#!/usr/bin/env python3
"""Kokoro-82M worker — preset voices, IPC protocol v1.

Runs inside ``engines/kokoro/.venv`` (torch==2.15.0+cpu on the canonical install,
kokoro==0.9.4). Speaks the frozen v1 protocol exactly like
``engines/mock/worker.py``: a dedicated reader thread keeps ``cancel``/``health``
answerable while a compute thread renders, and a parent-death watchdog stops an
orphaned worker from lingering.

The runtime device comes from ``AGENT_VOICE_RESOLVED_DEVICE`` (``cpu`` by
default, so a worker started outside the daemon keeps the CPU behaviour it had).
Kokoro 0.9.4 is device-agnostic, so the device is passed through rather than
assumed:

1. ``CUDA_VISIBLE_DEVICES`` is emptied *before* torch is imported — but only on
   the cpu path. On cuda/mps the worker must see the device it was resolved onto.
2. ``KPipeline(..., device=<resolved>)`` is always explicit: its default is
   ``device=None``, which auto-selects CUDA whenever a GPU is present.
3. ``gpu_vram_mb`` is 0 on cpu and the real allocation on cuda/mps. The cpu path
   never touches a ``torch.cuda`` API.

Mode is ``custom_voice`` only. Kokoro-82M has 54 built-in voices (8 advertised
by ``engine.yaml``), no reference-audio encoder and no instruction channel, so
``voice_clone``/``voice_design``/``instruct`` requests are refused as
``BAD_REQUEST`` instead of being silently reinterpreted: speaking in the wrong
timbre is worse than an honest error.

Streaming is real but chunk-granular: ``KPipeline.__call__`` yields one
``Result`` per <=510-phoneme chunk and decodes them sequentially, so ``stream_pcm``
forwards each chunk as int16 PCM (native 24 kHz) and checks the cancel event
between chunks. A cancel cannot interrupt a decode already in flight.
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

# --- device + offline-safety, applied BEFORE torch/huggingface_hub import ----
RESOLVED_DEVICE = (os.environ.get("AGENT_VOICE_RESOLVED_DEVICE") or "cpu").strip().lower()
"""The runtime device the daemon resolved for this engine.

Default ``cpu``: the canonical install is the CPU wheel, and a worker started
outside the daemon (``engines test kokoro``, the CPU test fixtures) must keep the
behaviour it had before the variable existed.
"""

if RESOLVED_DEVICE == "cpu":
    # Overwritten, not setdefault: a CUDA device exported by the daemon must not
    # reach a cpu worker, and torch reads this at import time. Deliberately *not*
    # applied on cuda/mps: there the worker has to see the device it was resolved
    # onto, which is the whole point of the resolution.
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
# The Phase 0 spike saw the xet backend hang on a large download finalization;
# the plain HTTP path is slower to start but always finishes.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# stderr is captured by the daemon as the crash report; tqdm bars are noise.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# --- IPC contract (duplicated deliberately: workers must not import the daemon) ---
FRAME_JSON = 0x01
FRAME_PCM = 0x02
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_SIZE = 4
PROTOCOL_VERSION = 1

BACKEND_NAME = "kokoro"
BACKEND_VERSION = "0.9.4"
"""Fallback pin; the real version is read from installed metadata in __init__."""

SAMPLE_RATE = 24_000
"""Kokoro's native rate. Nothing is resampled to reach it."""

ENGINE_ID = "kokoro"
MODE = "custom_voice"
"""The only synthesis mode this engine implements."""

DEFAULT_ALIAS = "kokoro-82m"
DEFAULT_MODELS: dict[str, str] = {DEFAULT_ALIAS: "hexgrad/Kokoro-82M"}
CHECKPOINT_FILE = "kokoro-v1_0.pth"
"""The one weight file whose absence means "not downloaded"."""

DOWNLOAD_COMMAND = f"agent-voice models download {ENGINE_ID} {DEFAULT_ALIAS}"

PRESET_SPEAKERS: tuple[str, ...] = (
    "af_heart",
    "af_bella",
    "af_sarah",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
    "bm_lewis",
)
"""Must stay identical to ``engine.yaml``'s ``preset_speakers``.

The leading letter is Kokoro's language code: ``a`` = American English,
``b`` = British English. Each distinct code needs its own ``KPipeline`` (it
carries a G2P object), which is why the pipeline cache is keyed by code.
"""

SUPPORTED_LANGUAGES_DOC = "Auto, English"
SPEED_MIN, SPEED_MAX = 0.5, 2.0
"""Same clamp the request schema enforces; a direct IPC caller gets no exemption."""

DEFAULT_PIPELINE_LANG = "a"
"""Warmed by load_model: the American English G2P/model is the common case."""

# Substrings that mean "the weights are not on this machine", across the HF hub's
# several error shapes (LocalEntryNotFoundError, OfflineModeIsEnabled, a requests
# ConnectionError from the download stack, or a bare OSError from a cache read).
MISSING_WEIGHT_MARKERS: tuple[str, ...] = (
    "localentrynotfound",
    "offlinemodeisenabled",
    "connectionerror",
    "couldn't connect",
    "could not download",
    "client error",
    "repository not found",
    "can't find",
    "cannot find",
    "no such file or directory",
    "entry not found",
)

ESPEAK_MARKERS: tuple[str, ...] = ("espeak", "phontab", "espeakng")


class _BadRequest(ValueError):
    """A caller mistake. Answered as BAD_REQUEST, never as an engine failure."""

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


class _MissingWeights(RuntimeError):
    """Weights (or a voice pack) are not in the local HF cache."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def _read_dist_version(distribution: str) -> str:
    import importlib.metadata as md

    try:
        return md.version(distribution)
    except md.PackageNotFoundError:
        return BACKEND_VERSION


def _parent_death_watchdog() -> None:
    """Linux: ask the kernel to SIGTERM us when the parent dies."""
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
        except (OSError, AttributeError):
            pass


def _parent_pid_poll(interval: float = 2.0) -> None:
    """Portable fallback: exit when the original parent disappears."""
    parent = os.getppid()

    def loop() -> None:
        while True:
            time.sleep(interval)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


class Connection:
    """Blocking framed connection with a write lock."""

    def __init__(self, sock) -> None:
        self._sock = sock
        self._lock = threading.Lock()

    def recv(self) -> tuple[int, bytes]:
        header = self._read_exact(LENGTH_PREFIX_SIZE + 1)
        (total,) = struct.unpack(LENGTH_PREFIX, header[:LENGTH_PREFIX_SIZE])
        frame_type = header[LENGTH_PREFIX_SIZE]
        payload = self._read_exact(total - 1) if total > 1 else b""
        return frame_type, payload

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError("parent closed IPC")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        frame = struct.pack(LENGTH_PREFIX, len(payload) + 1) + bytes((FRAME_JSON,)) + payload
        with self._lock:
            self._sock.sendall(frame)

    def send_pcm(self, stream_id: uuid.UUID, pcm: bytes) -> None:
        body = stream_id.bytes + pcm
        frame = struct.pack(LENGTH_PREFIX, len(body) + 1) + bytes((FRAME_PCM,)) + body
        with self._lock:
            self._sock.sendall(frame)


class KokoroEngine:
    def __init__(self, conn: Connection, models: dict[str, str]) -> None:
        self.conn = conn
        self.models = models
        self.loaded_models: list[str] = []
        # (repo_id, lang_code) -> KPipeline. One pipeline per language: it owns
        # the G2P object, while the voice is a per-call argument (KPipeline keeps
        # a loaded voice pack per voice name). Two is the maximum here, because
        # engine.yaml advertises English only.
        self._pipelines: dict[tuple[str, str], object] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._state_lock = threading.Lock()
        self.backend_version = _read_dist_version(BACKEND_NAME)
        self.device = RESOLVED_DEVICE

    # -- model ----------------------------------------------------------
    def _repo(self, alias: str) -> str:
        repo = self.models.get(alias)
        if not repo:
            raise _BadRequest(
                f"unknown model alias: {alias!r}",
                f"Known aliases: {sorted(self.models)}. list_models() reports them.",
            )
        return repo

    @staticmethod
    def _weights_cached(repo: str) -> bool:
        """True when the 327 MB checkpoint is already in the local HF cache.

        A cache *lookup*, never a download: the check has to be free so that
        load_model stays instant on a warm machine and honest on a cold one.
        """
        try:
            from huggingface_hub import try_to_load_from_cache
        except ImportError:  # pragma: no cover - huggingface_hub is a hard dep
            return False
        try:
            cached = try_to_load_from_cache(repo_id=repo, filename=CHECKPOINT_FILE)
        except Exception:
            return False
        return isinstance(cached, str) and Path(cached).is_file()

    @staticmethod
    def _fetch_weights(repo: str) -> None:
        """Download the checkpoint if it is missing, or raise ``_MissingWeights``.

        Deliberately explicit instead of letting KPipeline discover it: a network
        failure then surfaces as MODEL_NOT_FOUND with the one command that fixes
        it, rather than as a traceback in the middle of the first utterance.
        """
        try:
            from huggingface_hub import hf_hub_download

            hf_hub_download(repo_id=repo, filename=CHECKPOINT_FILE)
        except Exception as exc:
            raise _MissingWeights(
                f"Kokoro weights not available for {repo}: {type(exc).__name__}: {exc}"
            ) from exc

    def _ensure_weights(self, repo: str) -> None:
        if self._weights_cached(repo):
            return
        self._fetch_weights(repo)

    def _pipeline(self, repo: str, lang_code: str):
        key = (repo, lang_code)
        pipeline = self._pipelines.get(key)
        if pipeline is not None:
            return pipeline
        # Imported here, not at module scope: torch takes ~1 s and ~200 MB, and
        # hello/health must stay answerable during a cold start.
        from kokoro import KPipeline

        try:
            pipeline = KPipeline(
                lang_code=lang_code, repo_id=repo, device=self.device
            )
        except Exception as exc:
            if _is_missing_weights(exc):
                raise _MissingWeights(
                    f"could not load {repo} (lang_code={lang_code}): {exc}"
                ) from exc
            raise
        self._pipelines[key] = pipeline
        return pipeline

    def _load(self, alias: str) -> str:
        repo = self._repo(alias)
        self._ensure_weights(repo)
        # Warm the common English pipeline so a broken install fails at
        # load_model, not inside the caller's first sentence.
        self._pipeline(repo, DEFAULT_PIPELINE_LANG)
        if alias not in self.loaded_models:
            self.loaded_models.append(alias)
        return repo

    def _vram_mb(self) -> int:
        """This worker's device allocation in MB; exactly 0 on the cpu path.

        Kokoro is 82M parameters, so the real number is small on cuda/mps too —
        but it must be measured there, because a hard-coded 0 would claim the
        model is not on the device it was just loaded onto. ``mps`` has no
        ``memory_allocated``: Metal reports unified memory through
        ``current_allocated_memory`` only.
        """
        if self.device == "cpu":
            return 0
        try:
            import torch

            if self.device == "mps":
                allocated = getattr(
                    getattr(torch, "mps", None), "current_allocated_memory", None
                )
                return int(allocated() / 1024**2) if allocated is not None else 0
            if torch.cuda.is_available():
                return int(torch.cuda.memory_allocated() / 1024**2)
        except Exception:
            return 0
        return 0

    def _empty_device_cache(self) -> None:
        """Release cached device blocks for the device in use (no-op on cpu)."""
        if self.device == "cpu":
            return
        try:
            import gc

            import torch

            gc.collect()
            if self.device == "mps":
                empty = getattr(getattr(torch, "mps", None), "empty_cache", None)
                if empty is not None:
                    empty()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # -- request validation ---------------------------------------------
    @staticmethod
    def _speech_request(msg: dict) -> tuple[str, str, float]:
        """Validate one request; returns ``(speaker, text, speed)``.

        Everything a Kokoro request cannot honour is rejected here, in one
        place, with the fix in the suggestion: the daemon should never have to
        parse an engine-specific explanation.
        """
        mode = (msg.get("mode") or MODE).strip() or MODE
        if mode != MODE:
            raise _BadRequest(
                f"kokoro does not support mode={mode!r}: it speaks preset voices only",
                "Use mode='custom_voice' with speaker=<preset name>, or pick a "
                "cloning/design engine (`agent-voice engines list`).",
            )
        if (msg.get("ref_wav_path") or "").strip() or (msg.get("ref_text") or "").strip():
            raise _BadRequest(
                "kokoro cannot clone a voice: it has no reference-audio encoder",
                "Pass speaker=<preset name> instead of a reference clip; see "
                "list_models() for preset_speakers.",
            )
        if msg.get("prompt_cache_path"):
            raise _BadRequest(
                "kokoro has no voice-clone prompt cache",
                "Drop prompt_cache_path: this engine speaks its built-in voices.",
            )
        if msg.get("x_vector_only_mode"):
            raise _BadRequest(
                "kokoro has no x-vector-only clone mode",
                "Drop x_vector_only_mode and pass speaker=<preset name>.",
            )
        instruct = (msg.get("instruct") or "").strip()
        if instruct:
            raise _BadRequest(
                "kokoro does not accept instruct: it has no instruction channel",
                "Remove instruct, or use a model whose manifest says "
                "supports_instruct=true.",
            )

        speaker = (msg.get("speaker") or "").strip()
        if not speaker:
            raise _BadRequest(
                "custom_voice requires a speaker",
                f"Pass speaker=<preset name>. Supported: {', '.join(PRESET_SPEAKERS)}.",
            )
        if speaker not in PRESET_SPEAKERS:
            raise _BadRequest(
                f"unknown kokoro speaker: {speaker!r}",
                f"Supported speakers: {', '.join(PRESET_SPEAKERS)}. "
                "list_models() reports preset_speakers per model.",
            )

        language = (msg.get("language") or "").strip()
        if language and not (
            language.lower() in {"auto", "english"} or language.lower().startswith("en")
        ):
            raise _BadRequest(
                f"kokoro does not support language={language!r} in this engine",
                f"Supported: {SUPPORTED_LANGUAGES_DOC} (engine.yaml "
                "capabilities.languages).",
            )

        text = msg.get("text") or ""
        if not text.strip():
            raise _BadRequest(
                "text is empty",
                "Send the words to speak as text; whitespace-only input is not audio.",
            )

        try:
            speed = float(msg.get("speed") or 1.0)
        except (TypeError, ValueError):
            raise _BadRequest(
                f"speed must be a number, got {msg.get('speed')!r}", "Pass speed=1.0."
            ) from None
        # Clamped, not rejected: the request schema already bounds this, so a
        # slightly out-of-range value from a direct IPC caller is a caller typo
        # not worth failing a render over.
        speed = max(SPEED_MIN, min(SPEED_MAX, speed))
        return speaker, text, speed

    @staticmethod
    def _lang_code(speaker: str) -> str:
        """Kokoro's language code for a preset voice: ``a`` American, ``b`` British."""
        return speaker[0].lower() if speaker[:1].lower() in {"a", "b"} else DEFAULT_PIPELINE_LANG

    # -- audio ----------------------------------------------------------
    def _chunks(self, alias: str, speaker: str, text: str, speed: float):
        """Yield one float32 mono array per KPipeline result chunk."""
        import numpy as np

        repo = self._repo(alias)
        pipeline = self._pipeline(repo, self._lang_code(speaker))
        for result in pipeline(text, voice=speaker, speed=speed):
            audio = getattr(result, "audio", None)
            if audio is None:
                # A quiet pipeline (model=False) yields no audio; never expected
                # here, but a None must not become a crash inside the manager.
                continue
            array = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            array = np.asarray(array, dtype=np.float32).reshape(-1)
            if array.size:
                yield array

    @staticmethod
    def _pcm16(array) -> bytes:
        import numpy as np

        return (np.clip(array, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    def _write_wav(self, path: Path, arrays: list) -> None:
        """Write 24 kHz mono PCM16, the shape the daemon's WAV fast path expects."""
        import numpy as np
        import soundfile as sf

        data = arrays[0] if len(arrays) == 1 else np.concatenate(arrays)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(path), data, SAMPLE_RATE, subtype="PCM_16")

    # -- rpc ------------------------------------------------------------
    def hello(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "protocol_version": PROTOCOL_VERSION,
                "engine_id": ENGINE_ID,
                "backend_name": BACKEND_NAME,
                "backend_version": self.backend_version,
                "capabilities": {
                    "streaming": True,
                    "pcm_native": True,
                    "voice_clone": False,
                    "custom_voice": True,
                    "voice_design": False,
                    "instruct_control": False,
                },
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": self._vram_mb(),
                "pid": os.getpid(),
                "device": self.device,
                "sample_rate": SAMPLE_RATE,
                "mode": MODE,
                "preset_speakers": list(PRESET_SPEAKERS),
            }
        )

    def load_model(self, msg: dict) -> None:
        alias = msg.get("model_alias") or DEFAULT_ALIAS
        try:
            self._load(alias)
        except _BadRequest as exc:
            self._bad_request(msg["id"], exc)
            return
        except Exception as exc:  # classified and reported to the parent
            self._error(msg["id"], self._classify(exc), f"could not load {alias}: {exc}",
                        self._suggestion(exc))
            return
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
            }
        )

    def unload_model(self, msg: dict) -> None:
        alias = msg.get("model_alias") or DEFAULT_ALIAS
        if alias in self.loaded_models:
            self.loaded_models.remove(alias)
        repo = self.models.get(alias)
        if repo is not None:
            for key in [k for k in self._pipelines if k[0] == repo]:
                self._pipelines.pop(key, None)
            import gc

            gc.collect()
        # Only meaningful on cuda/mps, and a null op on cpu: dropping the pipeline
        # releases the Python objects, while the device allocator keeps its cached
        # blocks until it is told to let them go.
        self._empty_device_cache()
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
            }
        )

    def health(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "ok": True,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": self._vram_mb(),
                "in_flight": self.in_flight,
                "device": self.device,
            }
        )

    def cancel(self, msg: dict) -> None:
        target = msg["target_request_id"]
        event = self.cancel_events.get(target)
        if event is not None:
            event.set()
        self.conn.send_json(
            {"id": msg["id"], "target_request_id": target, "cancelled": event is not None}
        )

    def shutdown(self, msg: dict) -> None:
        for event in self.cancel_events.values():
            event.set()
        self.shutdown_event.set()
        self.conn.send_json({"id": msg["id"], "ok": True})

    # -- synthesis ------------------------------------------------------
    def synthesize(self, msg: dict) -> None:
        request_id = msg["id"]
        try:
            speaker, text, speed = self._speech_request(msg)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return

        alias = msg.get("model_alias") or DEFAULT_ALIAS
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            arrays: list = []
            produced = 0
            cancelled = False
            for array in self._chunks(alias, speaker, text, speed):
                if event.is_set() or self.shutdown_event.is_set():
                    cancelled = True
                    break
                arrays.append(array)
                produced += int(array.size)
            if cancelled:
                self._cancelled(request_id)
                return
            if produced == 0:
                self._error(
                    request_id,
                    "INTERNAL_ERROR",
                    "kokoro produced no audio for this text",
                    "Send plain English words or punctuation; a line of only "
                    "symbols can phonemise to nothing.",
                )
                return

            output = Path(msg.get("wav_path") or f"/tmp/kokoro-{request_id}.wav")
            self._write_wav(output, arrays)
            self.conn.send_json(
                {
                    "id": request_id,
                    "wav_path": str(output),
                    "sample_rate": SAMPLE_RATE,
                    "duration_sec": produced / SAMPLE_RATE,
                    "chunks_used": len(arrays),
                    "model_alias": alias,
                    "mode": MODE,
                    "speaker": speaker,
                    "backend_name": BACKEND_NAME,
                    "backend_version": self.backend_version,
                    "gpu_vram_mb": self._vram_mb(),
                    "device": self.device,
                }
            )
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
        except Exception as exc:  # classified and reported to the parent
            self._error(request_id, self._classify(exc), f"{type(exc).__name__}: {exc}",
                        self._suggestion(exc))
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def stream_pcm(self, msg: dict) -> None:
        request_id = msg["id"]
        stream_id = uuid.UUID(msg["stream_id"])
        try:
            speaker, text, speed = self._speech_request(msg)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return

        alias = msg.get("model_alias") or DEFAULT_ALIAS
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            produced = 0
            chunks_used = 0
            cancelled = False
            for array in self._chunks(alias, speaker, text, speed):
                # Cooperative cancellation: checked between chunks, because a
                # decoder step already running cannot be interrupted.
                if event.is_set() or self.shutdown_event.is_set():
                    cancelled = True
                    break
                pcm = self._pcm16(array)
                self.conn.send_pcm(stream_id, pcm)
                produced += len(pcm)
                chunks_used += 1
            if cancelled:
                self._cancelled(request_id)
                return
            if produced == 0:
                self._error(
                    request_id,
                    "INTERNAL_ERROR",
                    "kokoro produced no audio for this text",
                    "Send plain English words or punctuation; a line of only "
                    "symbols can phonemise to nothing.",
                )
                return
            self.conn.send_json(
                {
                    "id": request_id,
                    "stream_id": str(stream_id),
                    "done": True,
                    "duration_sec": produced / 2 / SAMPLE_RATE,
                    "chunks_used": chunks_used,
                    "sample_rate": SAMPLE_RATE,
                    "streaming_mode": "native",
                    "model_alias": alias,
                    "mode": MODE,
                    "speaker": speaker,
                    "gpu_vram_mb": self._vram_mb(),
                    "device": self.device,
                }
            )
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
        except Exception as exc:  # classified and reported to the parent
            if not event.is_set():
                self._error(request_id, self._classify(exc), f"{type(exc).__name__}: {exc}",
                            self._suggestion(exc))
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    # -- errors ---------------------------------------------------------
    def _classify(self, exc: BaseException) -> str:
        """Map a failure onto the daemon's error taxonomy, aware of the device.

        ``CUDA_OOM`` only on the cuda path. On cpu and mps an allocation failure
        is real but not a CUDA one, so it stays INTERNAL_ERROR with the
        allocator's own message rather than a verdict that names the wrong knob.
        """
        if isinstance(exc, _BadRequest):
            return "BAD_REQUEST"
        if isinstance(exc, _MissingWeights) or _is_missing_weights(exc):
            return "MODEL_NOT_FOUND"
        message = str(exc).lower()
        oom = (
            "out of memory" in message
            or "outofmemory" in type(exc).__name__.lower()
            or "defaultcpuallocator" in message
        )
        if oom and self.device == "cuda":
            return "CUDA_OOM"
        return "INTERNAL_ERROR"

    @staticmethod
    def _suggestion(exc: BaseException) -> str:
        if isinstance(exc, _BadRequest):
            return exc.suggestion
        if isinstance(exc, _MissingWeights) or _is_missing_weights(exc):
            return (
                f"Run `{DOWNLOAD_COMMAND}` (or check network access to "
                "huggingface.co) and retry."
            )
        message = str(exc).lower()
        if any(marker in message for marker in ESPEAK_MARKERS):
            return (
                "Install espeak-ng (e.g. `sudo apt install espeak-ng`): Kokoro's "
                "English G2P uses it for out-of-dictionary words."
            )
        return "Check the worker's stderr (`agent-voice doctor`) and retry."

    def _cancelled(self, request_id: str) -> None:
        self.conn.send_json(
            {
                "id": request_id,
                "error_code": "JOB_CANCELLED",
                "message": "request was cancelled",
                "suggestion": "Retry without cancelling, or reduce the text length.",
            }
        )

    def _bad_request(self, request_id: str, exc: _BadRequest) -> None:
        self.conn.send_json(
            {
                "id": request_id,
                "error_code": "BAD_REQUEST",
                "message": str(exc),
                "suggestion": exc.suggestion,
            }
        )

    def _error(self, request_id: str, code: str, message: str, suggestion: str) -> None:
        self.conn.send_json(
            {"id": request_id, "error_code": code, "message": message, "suggestion": suggestion}
        )

    # -- dispatch -------------------------------------------------------
    def dispatch(self, frame_type: int, payload: bytes) -> None:
        if frame_type != FRAME_JSON:
            return
        msg = json.loads(payload.decode("utf-8"))
        method = msg.get("method")

        if method in {"synthesize", "stream_pcm"}:
            # Compute runs off the reader thread so cancel/health stay responsive
            # while a chunk renders (a 510-phoneme chunk is ~1 s of CPU work).
            target = self.synthesize if method == "synthesize" else self.stream_pcm
            threading.Thread(target=target, args=(msg,), daemon=True).start()
            return

        handler = {
            "hello": self.hello,
            "load_model": self.load_model,
            "unload_model": self.unload_model,
            "health": self.health,
            "cancel": self.cancel,
            "shutdown": self.shutdown,
        }.get(method)
        if handler is None:
            self._error(
                msg.get("id", ""),
                "BAD_REQUEST",
                f"unknown method: {method}",
                "Check src/agent_voice/ipc/protocol.py for the supported method list.",
            )
            return
        handler(msg)


def _is_missing_weights(exc: BaseException) -> bool:
    """Whether this failure means "the weights are not here", not "it broke"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in MISSING_WEIGHT_MARKERS)


def _models_from_env() -> dict[str, str]:
    raw = os.environ.get("AGENT_VOICE_ENGINE_MODELS")
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return {str(alias): str(repo) for alias, repo in parsed.items()}
    return dict(DEFAULT_MODELS)


def _connect():
    import socket

    kind = os.environ.get("AGENT_VOICE_IPC_TRANSPORT", "unix")
    if kind == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.environ["AGENT_VOICE_IPC_PATH"])
        return sock

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect(
        (os.environ["AGENT_VOICE_IPC_HOST"], int(os.environ["AGENT_VOICE_IPC_PORT"]))
    )
    return sock


def main() -> int:
    _parent_death_watchdog()
    _parent_pid_poll()

    sock = _connect()
    conn = Connection(sock)
    engine = KokoroEngine(conn, _models_from_env())

    while not engine.shutdown_event.is_set():
        try:
            frame_type, payload = conn.recv()
        except (ConnectionError, OSError):
            break
        try:
            engine.dispatch(frame_type, payload)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            try:
                engine._error(
                    "",
                    "BAD_REQUEST",
                    f"malformed request: {exc}",
                    "Send a JSON object with an id and method.",
                )
            except OSError:
                break

    try:
        sock.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
