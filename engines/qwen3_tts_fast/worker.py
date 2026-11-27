#!/usr/bin/env python3
"""Qwen3-TTS worker (CUDA-graph accelerated `faster-qwen3-tts` backend).

Runs inside ``engines/qwen3_tts_fast/.venv``. Speaks IPC protocol v1.

Three synthesis modes, each with a streaming and a non-streaming entry point:
``clone`` (``generate_voice_clone*``), ``custom_voice``
(``generate_custom_voice*`` with a preset speaker) and ``voice_design``
(``generate_voice_design*`` from a natural-language description).

This backend has real token-level streaming via ``generate_*_streaming(chunk_size=8)``,
so ``stream_pcm`` forwards audio while the model is still decoding and
``streaming_mode`` is ``"native"``. Cancellation is checked between yielded
chunks *and* the generator is closed so the CUDA graph loop stops promptly.

CUDA only, and the refusal is explicit rather than accidental: the installed
``faster-qwen3-tts 0.5.4`` metadata requires an NVIDIA GPU with CUDA and drives
``torch.cuda.CUDAGraph``, so there is no CPU or MPS code path to offer. When
``AGENT_VOICE_RESOLVED_DEVICE`` resolves to anything but cuda/rocm, ``load_model``
answers ``BAD_REQUEST`` naming engine ``qwen3_tts`` — the same model family with
real cpu/mps/rocm paths — instead of failing deep inside the loader.

CUDA-graph capture is an optimization, never a promise: ``warmup()`` may fail on
a driver or a model that does not support it, and the worker then keeps working
in eager mode. That is reported (``cuda_graphs`` in ``hello``/``health``) and
logged to stderr instead of being swallowed.
"""

from __future__ import annotations

import ctypes
import gc
import json
import os
import signal
import struct
import sys
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

FRAME_JSON = 0x01
FRAME_PCM = 0x02
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_SIZE = 4
PROTOCOL_VERSION = 1
BACKEND_NAME = "faster-qwen3-tts"
SAMPLE_RATE = 24_000
STREAM_CHUNK_STEPS = 8
# Bumped whenever the on-disk prompt layout changes; a mismatch invalidates the file.
PROMPT_SCHEMA_VERSION = 2
# Live prompt dicts kept in RAM (one per distinct voice/text/mode).
PROMPT_CACHE_SIZE = 8
# The upstream ICL prompt is built from the reference wav with 0.5 s of trailing
# silence appended (faster_qwen3_tts/model.py:_resolve_voice_clone_prompt_from_reference).
# Reproduce it exactly so a warm cache hit is bit-identical to a cold compute.
_ICL_APPEND_SILENCE = True

ACCELERATOR_DEVICES: frozenset[str] = frozenset({"cuda", "rocm"})
"""The only devices this backend has a code path for.

``rocm`` is admitted because HIP is exposed through ``torch.cuda``, so the same
``CUDAGraph`` code runs there. It is not *declared* in engine.yaml: the installed
metadata makes no ROCm claim, so the resolver never selects it on its own.
"""

UNSUPPORTED_DEVICE_MARKER = "faster-qwen3-tts needs CUDA"

# Request-level mistakes the worker can spot before touching the model. Marker ->
# fix, so _classify can answer BAD_REQUEST with a suggestion an agent can act on
# instead of a generic INTERNAL_ERROR.
CLIENT_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "custom_voice needs a preset speaker",
        "Pass speaker=<preset name>; list_models() reports preset_speakers per model.",
    ),
    (
        "voice_design needs a non-empty instruct",
        "Describe the target voice in instruct, e.g. 'A warm, low-pitched male narrator'.",
    ),
    (
        "unknown mode",
        "Use mode='clone', 'custom_voice' or 'voice_design'.",
    ),
    (
        UNSUPPORTED_DEVICE_MARKER,
        "Use engine qwen3_tts for cpu/mps/rocm (same checkpoints, no CUDA graphs), or "
        "run this engine on cuda with AGENT_VOICE_DEVICES=qwen3_tts_fast=cuda.",
    ),
)


def resolved_device() -> str:
    """The runtime device the daemon resolved for this engine.

    Defaults to ``cuda``: that is the only build this engine installs, so a
    worker started outside the daemon (a conformance run, a GPU test) keeps the
    behaviour it had before the device variable existed.
    """
    value = (os.environ.get("AGENT_VOICE_RESOLVED_DEVICE") or "").strip().lower()
    return value or "cuda"


def _client_error_suggestion(message: str) -> str:
    """Actionable fix for a caller mistake, or ``""`` when it is not one."""
    for marker, suggestion in CLIENT_ERROR_HINTS:
        if marker in message:
            return suggestion
    return ""


def _model_device(model):
    """Best-effort torch device of a loaded model, used to place cached tensors."""
    import torch

    for owner in (model, getattr(model, "model", None)):
        candidate = getattr(owner, "device", None)
        if candidate is not None:
            try:
                return torch.device(candidate)
            except (TypeError, RuntimeError):
                continue
    try:
        return next(model.parameters()).device
    except Exception:
        return torch.device("cpu")


def _read_dist_version(distribution: str) -> str:
    import importlib.metadata as md

    try:
        return md.version(distribution)
    except md.PackageNotFoundError:
        return "unknown"


def _parent_death_watchdog() -> None:
    if sys.platform.startswith("linux"):
        try:
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG
        except (OSError, AttributeError):
            pass


def _parent_pid_poll(interval: float = 2.0) -> None:
    parent = os.getppid()

    def loop() -> None:
        while True:
            time.sleep(interval)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


class Connection:
    def __init__(self, sock) -> None:
        self._sock = sock
        self._lock = threading.Lock()

    def recv(self) -> tuple[int, bytes]:
        header = self._read_exact(LENGTH_PREFIX_SIZE + 1)
        (total,) = struct.unpack(LENGTH_PREFIX, header[:LENGTH_PREFIX_SIZE])
        payload = self._read_exact(total - 1) if total > 1 else b""
        return header[LENGTH_PREFIX_SIZE], payload

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


class FastEngine:
    def __init__(self, conn: Connection, models: dict[str, str]) -> None:
        self.conn = conn
        self.models = models
        self.loaded: dict[str, object] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._lock = threading.Lock()
        self.backend_version = _read_dist_version("faster-qwen3-tts")
        self.device = resolved_device()
        self.cuda_graphs = False
        """True only after a ``warmup()`` actually captured. ``hello``/``health``
        report it so a silent fallback to eager mode is visible to the daemon."""
        # (alias, sha256, ref_text, xvec, backend_version) -> dict accepted as
        # voice_clone_prompt by faster_qwen3_tts.
        self.prompt_cache: OrderedDict[tuple, dict] = OrderedDict()

    # -- model ----------------------------------------------------------
    def _load(self, alias: str):
        if alias in self.loaded:
            return self.loaded[alias]

        if self.device not in ACCELERATOR_DEVICES:
            # Refused before the import: a CUDA-graph backend has nothing to offer
            # a cpu/mps runtime, and the sibling engine has a real path there.
            raise ValueError(
                f"{UNSUPPORTED_DEVICE_MARKER}: this backend requires an NVIDIA GPU "
                f"with CUDA and is not available on device '{self.device}'"
            )

        from faster_qwen3_tts import FasterQwen3TTS

        repo = self.models.get(alias)
        if repo is None:
            raise ValueError(f"unknown model alias: {alias}")

        model = FasterQwen3TTS.from_pretrained(repo)
        # Captures CUDA graphs so the first real request is not the slow one.
        # An optimization, not a requirement: on failure the model still generates
        # in eager mode, so the failure is reported and logged rather than hidden.
        # stderr is what the daemon reads as the crash report, hence the warning
        # goes there.
        try:
            model.warmup(prefill_len=100)
            self.cuda_graphs = True
        except Exception as exc:
            self.cuda_graphs = False
            print(
                "qwen3_tts_fast worker: CUDA graph capture failed "
                f"({type(exc).__name__}: {exc}); continuing without graphs",
                file=sys.stderr,
            )
        self.loaded[alias] = model
        return model

    def _vram_mb(self) -> int | None:
        """This worker's device allocation in MB, or ``None`` when it cannot say.

        ``None`` means "no allocator to ask" (the device is not cuda/rocm, so the
        worker will refuse at load anyway); a real number is the allocator's own.
        """
        if self.device not in ACCELERATOR_DEVICES:
            return None
        try:
            import torch

            if torch.cuda.is_available():
                return int(torch.cuda.memory_allocated() / 1024**2)
        except Exception:
            return None
        return None

    def _seed(self, seed) -> None:
        if seed is None:
            return
        try:
            import torch

            torch.manual_seed(int(seed))
        except Exception:
            pass

    # -- rpc ------------------------------------------------------------
    def hello(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "protocol_version": PROTOCOL_VERSION,
                "engine_id": "qwen3_tts_fast",
                "backend_name": BACKEND_NAME,
                "backend_version": self.backend_version,
                "capabilities": {
                    "streaming": True,
                    "pcm_native": True,
                    "custom_voice": True,
                    "voice_design": True,
                    "instruct_control": True,
                },
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
                "cuda_graphs": self.cuda_graphs,
                "pid": os.getpid(),
            }
        )

    def load_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        try:
            self._load(alias)
        except Exception as exc:
            self._error(
                msg["id"],
                self._classify(exc),
                f"could not load {alias}: {exc}",
                _client_error_suggestion(str(exc))
                or "Check the model is downloaded and the GPU has free VRAM.",
            )
            return
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
                "cuda_graphs": self.cuda_graphs,
            }
        )

    def unload_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        self.loaded.pop(alias, None)
        # Drop this model's cached prompts: they hold tensors on its device.
        for key in [k for k in self.prompt_cache if k[0] == alias]:
            self.prompt_cache.pop(key, None)
        try:
            import torch

            gc.collect()
            # Guarded: an empty device is exactly what a bad resolve produces, and
            # torch.cuda.empty_cache() on a build without CUDA is not free.
            if self.device in ACCELERATOR_DEVICES and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
                "cuda_graphs": self.cuda_graphs,
            }
        )

    def health(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "ok": True,
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
                "cuda_graphs": self.cuda_graphs,
                "in_flight": self.in_flight,
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
    def _entry_point(self, model, msg: dict, *, streaming: bool):
        """Resolve the backend entry point and kwargs for this request's mode.

        Returns ``(function, kwargs)`` so streaming and non-streaming share one
        routing decision: the caller just splats the kwargs.
        """
        mode = msg.get("mode") or "clone"
        # Validates the mode plus speaker/instruct before any backend work runs.
        kwargs = self._generation_kwargs(model, msg, mode)
        if streaming:
            # Every streaming entry point shares the chunk_size knob.
            kwargs["chunk_size"] = STREAM_CHUNK_STEPS
        non_streaming_name, streaming_name = {
            "clone": ("generate_voice_clone", "generate_voice_clone_streaming"),
            "custom_voice": ("generate_custom_voice", "generate_custom_voice_streaming"),
            "voice_design": ("generate_voice_design", "generate_voice_design_streaming"),
        }[mode]
        return getattr(model, streaming_name if streaming else non_streaming_name), kwargs

    def _generation_kwargs(self, model, msg: dict, mode: str) -> dict:
        """Mode-specific kwargs; also the single place caller mistakes are caught."""
        text = msg.get("text") or ""
        language = msg.get("language") or "Auto"

        if mode == "custom_voice":
            speaker = (msg.get("speaker") or "").strip()
            if not speaker:
                raise ValueError(
                    "custom_voice needs a preset speaker: pass speaker=<name> "
                    "(list_models() reports preset_speakers for this model)"
                )
            return {
                "text": text,
                "language": language,
                "speaker": speaker,
                # The 0.6B CustomVoice backend drops instruct internally; forward it
                # unchanged rather than second-guessing the model here.
                "instruct": (msg.get("instruct") or "").strip() or None,
            }

        if mode == "voice_design":
            instruct = (msg.get("instruct") or "").strip()
            if not instruct:
                raise ValueError(
                    "voice_design needs a non-empty instruct: describe the voice "
                    "you want (e.g. 'A warm, low-pitched male narrator')"
                )
            return {"text": text, "language": language, "instruct": instruct}

        if mode != "clone":
            raise ValueError(
                f"unknown mode: {mode!r}: expected clone, custom_voice or voice_design"
            )

        return self._clone_kwargs(model, msg)

    def _clone_kwargs(self, model, msg: dict) -> dict:
        """Clone kwargs, prompt cache included. Prompt caching is clone-only."""
        ref_path = msg.get("ref_wav_path") or ""
        if not ref_path:
            # The *-Base checkpoints have no built-in speakers; without a reference
            # the backend would fail deep inside the model loader.
            raise ValueError(
                "no reference voice supplied: the Qwen3-TTS Base model can only "
                "clone, so pass voice=<voice_id> (create one with clone_voice) "
                "or register a default voice"
            )
        kwargs = {
            "text": msg.get("text") or "",
            "language": msg.get("language") or "Auto",
            "ref_text": msg.get("ref_text") or "",
        }
        prompt = self._resolve_prompt(model, msg, ref_path, kwargs["ref_text"])
        if prompt is not None:
            # `xvec_only`/`ref_audio` are ignored once a prompt dict is supplied.
            kwargs["voice_clone_prompt"] = prompt
        else:
            kwargs["ref_audio"] = ref_path
            kwargs["xvec_only"] = bool(msg.get("x_vector_only_mode"))
        return kwargs

    # -- prompt cache ---------------------------------------------------
    def _resolve_prompt(self, model, msg: dict, ref_path: str, ref_text: str):
        """Return a voice-clone prompt dict, doing no encoder work when cached.

        Both modes are fully disk-cached here. Unlike a bare embedding cache, the
        tensor triple ``ref_code`` + ``ref_spk_embedding`` + mode flags is enough
        to rebuild a complete ``voice_clone_prompt`` dict, and
        ``faster_qwen3_tts.model._resolve_precomputed_voice_clone_prompt`` accepts
        ICL dicts (re-tokenizing ``ref_text`` itself), so ICL needs no re-encode.
        """
        prompt_path = msg.get("prompt_cache_path")
        if not prompt_path:
            return None

        import torch

        model_alias = msg["model_alias"]
        xvec_only = bool(msg.get("x_vector_only_mode"))
        ref_sha = self._sha256(ref_path)
        key = (model_alias, ref_sha, ref_text, xvec_only, self.backend_version)

        cached = self.prompt_cache.get(key)
        if cached is not None:
            self.prompt_cache.move_to_end(key)
            return cached

        target = Path(prompt_path)
        meta_path = target.with_name("prompt_meta.json")
        if target.is_file() and meta_path.is_file():
            prompt = self._read_prompt_file(target, meta_path, model, key)
            if prompt is not None:
                self._remember_prompt(key, prompt)
                return prompt
            # Stale/corrupt cache must never be fatal: drop both files, recompute.
            self._discard_prompt_files(target, meta_path)

        prompt = self._compute_prompt(model, ref_path, ref_text, xvec_only)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "ref_code": None if prompt["ref_code"][0] is None
                    else prompt["ref_code"][0].detach().cpu(),
                    "ref_spk_embedding": prompt["ref_spk_embedding"][0].detach().cpu(),
                    "x_vector_only_mode": bool(prompt["x_vector_only_mode"][0]),
                    "icl_mode": bool(prompt["icl_mode"][0]),
                    "ref_text": ref_text,
                },
                target,
            )
            meta_path.write_text(json.dumps(self._prompt_meta(key)), "utf-8")
        except Exception:
            pass
        self._remember_prompt(key, prompt)
        return prompt

    @staticmethod
    def _load_ref_with_silence(ref_path: str) -> tuple:
        """Mirror faster_qwen3_tts's ICL reference load: mono float32 + trailing silence.

        ``_resolve_voice_clone_prompt_from_reference`` appends 0.5 s of silence to the
        reference before encoding, so the last codec tokens encode silence instead of
        the reference's final phoneme. Recomputing without it would change the voice.
        """
        import numpy as np
        import soundfile as sf

        audio, sr = sf.read(str(ref_path), dtype="float32", always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if _ICL_APPEND_SILENCE:
            audio = np.concatenate(
                [audio, np.zeros(int(0.5 * sr), dtype=np.float32)]
            )
        return audio, sr

    def _compute_prompt(self, model, ref_path: str, ref_text: str, xvec_only: bool) -> dict:
        """Build the prompt dict the way the fast wrapper would, via the inner model."""
        inner = getattr(model, "model", None)
        if inner is None or not hasattr(inner, "create_voice_clone_prompt"):
            raise RuntimeError("backend exposes no create_voice_clone_prompt")
        if xvec_only:
            items = inner.create_voice_clone_prompt(
                ref_audio=str(ref_path), ref_text="", x_vector_only_mode=True
            )
        else:
            items = inner.create_voice_clone_prompt(
                ref_audio=self._load_ref_with_silence(ref_path), ref_text=ref_text
            )
        return inner._prompt_items_to_voice_clone_prompt(items)

    def _read_prompt_file(self, target: Path, meta_path: Path, model, key: tuple):
        """Rebuild a prompt dict from disk, or return None if the cache is unusable."""
        _model_alias, ref_sha, ref_text, xvec_only, backend_version = key
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            if meta.get("schema_version") != PROMPT_SCHEMA_VERSION:
                return None
            if (
                meta.get("ref_wav_sha256") != ref_sha
                or meta.get("ref_text") != ref_text
                or meta.get("backend_version") != backend_version
                or bool(meta.get("x_vector_only_mode")) != xvec_only
            ):
                return None

            import torch

            state = torch.load(target, map_location="cpu", weights_only=True)
            spk = state.get("ref_spk_embedding")
            if spk is None:
                return None
            saved_xvec = bool(state["x_vector_only_mode"])
            icl_mode = bool(state["icl_mode"])
            ref_code = state.get("ref_code")
            if saved_xvec and ref_code is not None:
                return None
            if icl_mode and ref_code is None:
                return None

            # faster_qwen3_tts re-moves these to the talker device; hoisting them here
            # just avoids a host->device copy on every call.
            device = _model_device(model)
            return {
                "ref_code": [None if ref_code is None else ref_code.to(device)],
                "ref_spk_embedding": [spk.to(device)],
                "x_vector_only_mode": [saved_xvec],
                "icl_mode": [icl_mode],
            }
        except Exception:
            return None

    def _remember_prompt(self, key: tuple, prompt: dict) -> None:
        self.prompt_cache[key] = prompt
        self.prompt_cache.move_to_end(key)
        while len(self.prompt_cache) > PROMPT_CACHE_SIZE:
            self.prompt_cache.popitem(last=False)

    @staticmethod
    def _prompt_meta(key: tuple) -> dict:
        model_alias, ref_sha, ref_text, xvec_only, backend_version = key
        return {
            "schema_version": PROMPT_SCHEMA_VERSION,
            "backend_version": backend_version,
            "ref_wav_sha256": ref_sha,
            "ref_text": ref_text,
            "x_vector_only_mode": xvec_only,
            "model_alias": model_alias,
            "fields": ["ref_code", "ref_spk_embedding", "x_vector_only_mode", "icl_mode",
                       "ref_text"],
        }

    @staticmethod
    def _discard_prompt_files(target: Path, meta_path: Path) -> None:
        for path in (target, meta_path):
            try:
                path.unlink()
            except OSError:
                pass

    @staticmethod
    def _sha256(path: str) -> str:
        import hashlib

        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def synthesize(self, msg: dict) -> None:
        request_id = msg["id"]
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._lock:
            self.in_flight += 1
        try:
            model = self._load(msg["model_alias"])
            self._seed(msg.get("seed"))
            generate, kwargs = self._entry_point(model, msg, streaming=False)
            audio_list, sr = generate(**kwargs)
            if event.is_set():
                self._error(request_id, "JOB_CANCELLED", "request was cancelled", "")
                return

            import numpy as np
            import soundfile as sf

            data = np.asarray(audio_list[0], dtype=np.float32)
            out = Path(msg.get("wav_path") or f"/tmp/fast-{request_id}.wav")
            out.parent.mkdir(parents=True, exist_ok=True)
            sf.write(out, data, int(sr), subtype="PCM_16")
            self.conn.send_json(
                {
                    "id": request_id,
                    "wav_path": str(out),
                    "sample_rate": int(sr),
                    "duration_sec": len(data) / float(sr),
                    "model_alias": msg["model_alias"],
                    "backend_name": BACKEND_NAME,
                    "backend_version": self.backend_version,
                    "gpu_vram_mb": self._vram_mb(),
                    "device": self.device,
                }
            )
        except Exception as exc:
            self._error(request_id, self._classify(exc), str(exc),
                        _client_error_suggestion(str(exc))
                        or "Check GPU memory and the reference audio.")
        finally:
            with self._lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def stream_pcm(self, msg: dict) -> None:
        """True token-level streaming: forward each decoded chunk immediately."""
        request_id = msg["id"]
        stream_id = uuid.UUID(msg["stream_id"])
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._lock:
            self.in_flight += 1
        generator = None
        try:
            import numpy as np

            model = self._load(msg["model_alias"])
            self._seed(msg.get("seed"))
            generate, kwargs = self._entry_point(model, msg, streaming=True)
            generator = generate(**kwargs)

            produced = 0
            for chunk, sr, _timing in generator:
                if event.is_set() or self.shutdown_event.is_set():
                    break
                data = np.asarray(chunk, dtype=np.float32).flatten()
                if int(sr) != SAMPLE_RATE:
                    target_len = int(len(data) * SAMPLE_RATE / int(sr))
                    if target_len > 0:
                        data = np.interp(
                            np.linspace(0, len(data) - 1, target_len, dtype=np.float64),
                            np.arange(len(data)),
                            data,
                        ).astype(np.float32)
                pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
                self.conn.send_pcm(stream_id, pcm)
                produced += len(pcm)

            if event.is_set():
                self._error(request_id, "JOB_CANCELLED", "request was cancelled", "")
                return

            self.conn.send_json(
                {
                    "id": request_id,
                    "stream_id": str(stream_id),
                    "done": True,
                    "duration_sec": produced / 2 / SAMPLE_RATE,
                    "streaming_mode": "native",
                    "gpu_vram_mb": self._vram_mb(),
                    "device": self.device,
                }
            )
        except Exception as exc:
            if not event.is_set():
                self._error(request_id, self._classify(exc), str(exc),
                            _client_error_suggestion(str(exc)))
        finally:
            if generator is not None:
                close = getattr(generator, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        pass
            with self._lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def _classify(self, exc: BaseException) -> str:
        """Map a failure onto the daemon's taxonomy.

        ``CUDA_OOM`` is only claimed on cuda/rocm; every other device is refused
        at load, so any other device's allocation failure is an INTERNAL_ERROR
        with the allocator's own message rather than a misleading CUDA verdict.
        """
        message = str(exc)
        lowered = message.lower()
        oom = (
            "out of memory" in lowered
            or "outofmemory" in type(exc).__name__.lower()
            or "defaultcpuallocator" in lowered
        )
        if oom:
            return "CUDA_OOM" if self.device in ACCELERATOR_DEVICES else "INTERNAL_ERROR"
        if _client_error_suggestion(message):
            return "BAD_REQUEST"
        return "INTERNAL_ERROR"

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
            self._error(msg.get("id", ""), "BAD_REQUEST", f"unknown method: {method}", "")
            return
        handler(msg)


def _models_from_env() -> dict[str, str]:
    raw = os.environ.get("AGENT_VOICE_ENGINE_MODELS")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {
        "1.7b-base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "0.6b-base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "1.7b-custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "0.6b-custom": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        "1.7b-design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    }


def _connect():
    import socket

    kind = os.environ.get("AGENT_VOICE_IPC_TRANSPORT", "unix")
    if kind == "unix":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.environ["AGENT_VOICE_IPC_PATH"])
        return sock
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.connect((os.environ["AGENT_VOICE_IPC_HOST"], int(os.environ["AGENT_VOICE_IPC_PORT"])))
    return sock


def main() -> int:
    _parent_death_watchdog()
    _parent_pid_poll()
    sock = _connect()
    conn = Connection(sock)
    engine = FastEngine(conn, _models_from_env())

    while not engine.shutdown_event.is_set():
        try:
            frame_type, payload = conn.recv()
        except (ConnectionError, OSError):
            break
        try:
            engine.dispatch(frame_type, payload)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            try:
                engine._error("", "BAD_REQUEST", f"malformed request: {exc}", "")
            except OSError:
                break

    try:
        sock.close()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
