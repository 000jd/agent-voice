#!/usr/bin/env python3
"""Qwen3-TTS worker (official `qwen-tts` backend).

Runs inside ``engines/qwen3_tts/.venv``. Speaks IPC protocol v1.

Three synthesis modes, each routed to its own entry point: ``clone``
(``generate_voice_clone``), ``custom_voice`` (``generate_custom_voice`` with a
preset speaker) and ``voice_design`` (``generate_voice_design`` from a
natural-language description).

Cancellation is real: a ``transformers.StoppingCriteria`` consults a
``threading.Event`` between decode steps, so a cancel lands mid-utterance rather
than only between chunks. All three entry points forward ``**kwargs`` to
``model.generate()``, so the same criteria covers every mode. Streaming is
emulated because this backend has no token-level streaming API — the parent is
told ``streaming_mode: "emulated"`` so nobody claims otherwise.

The runtime device comes from ``AGENT_VOICE_RESOLVED_DEVICE`` (``cpu``, ``cuda``,
``mps`` or ``rocm``), which the daemon's worker manager resolves and injects. A
worker started without it keeps the pre-v0.6 behaviour and runs on ``cuda``.
The device decides three things and nothing else: the ``device_map``, the dtype
(``bfloat16`` only where the hardware has a fast path for it — CPU and Metal do
not), and whether VRAM is reported at all.
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
BACKEND_NAME = "qwen-tts"
SAMPLE_RATE = 24_000
STREAM_CHUNK_SECONDS = 0.1
# Bumped whenever the on-disk prompt layout changes; a mismatch invalidates the file.
PROMPT_SCHEMA_VERSION = 2
# Live prompt objects kept in RAM (one per distinct voice/text/mode).
PROMPT_CACHE_SIZE = 8

DEFAULT_DEVICE = "cuda"
"""Device for a worker spawned without AGENT_VOICE_RESOLVED_DEVICE.

Backward compatibility, deliberately: a v0.5 daemon, a direct IPC harness or a
GPU test starts this worker with no device variable, and that worker has always
run on CUDA. Anything else would silently change the audio those callers get.
"""

ACCELERATOR_DEVICES: frozenset[str] = frozenset({"cuda", "rocm"})
"""Devices whose torch build is a CUDA/HIP build.

``rocm`` is HIP, which torch exposes through the same ``torch.cuda`` namespace, so
both take ``cuda:0`` as the device string, ``bfloat16``, and a real
``memory_allocated()``. ``mps`` is Metal: no ``torch.cuda`` API exists there.
"""


def resolved_device() -> str:
    """The runtime device the daemon resolved for this engine."""
    value = (os.environ.get("AGENT_VOICE_RESOLVED_DEVICE") or "").strip().lower()
    return value or DEFAULT_DEVICE


def device_map_for(device: str) -> str:
    """``device_map`` for ``from_pretrained``: one concrete device, never "auto"."""
    if device in ACCELERATOR_DEVICES:
        return "cuda:0"
    return device


def dtype_for(torch_module, device: str):
    """Half precision only where it is a speedup, full precision elsewhere.

    bfloat16 needs a fast bf16 matmul path, which a CUDA/HIP card has and a CPU
    does not; Metal does not implement bfloat16 at all, so it would either fail
    or be silently upcast. float32 is the honest choice on cpu and mps.
    """
    if device in ACCELERATOR_DEVICES:
        return torch_module.bfloat16
    return torch_module.float32


def is_oom(message: str, type_name: str = "") -> bool:
    """Whether a failure is an allocation failure, across all three backends."""
    lowered = message.lower()
    return (
        "out of memory" in lowered
        or "outofmemory" in type_name.lower()
        or "defaultcpuallocator" in lowered
    )

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
)


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


class CancelCriteria:
    """StoppingCriteria that aborts decoding when the cancel event fires."""

    def __init__(self, event: threading.Event) -> None:
        self.event = event

    def __call__(self, input_ids, scores, **kwargs) -> bool:
        return self.event.is_set()


class QwenEngine:
    def __init__(self, conn: Connection, models: dict[str, str]) -> None:
        self.conn = conn
        self.models = models
        self.loaded: dict[str, object] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._lock = threading.Lock()
        self._torch = None
        self.backend_version = _read_dist_version("qwen-tts")
        self.device = resolved_device()
        # alias -> OrderedDict[(sha256, ref_text, xvec, backend_version)] -> [VoiceClonePromptItem]
        self.prompt_cache: OrderedDict[tuple, list] = OrderedDict()

    # -- model loading --------------------------------------------------
    def _load(self, alias: str):
        if alias in self.loaded:
            return self.loaded[alias]

        import torch
        from qwen_tts import Qwen3TTSModel

        self._torch = torch
        repo = self.models.get(alias)
        if repo is None:
            raise ValueError(f"unknown model alias: {alias}")

        model = Qwen3TTSModel.from_pretrained(
            repo,
            device_map=device_map_for(self.device),
            dtype=dtype_for(torch, self.device),
            # sdpa, never flash-attn: building flash-attn on a 32 GB box is a trap
            # and the docs only ever call it a recommendation.
            attn_implementation="sdpa",
        )
        self.loaded[alias] = model
        return model

    def _vram_mb(self) -> int | None:
        """This worker's device allocation in MB, or ``None`` when there is none.

        ``None`` rather than 0 on cpu/mps: there is no separate pool to measure
        there, and a 0 would read as "this worker holds nothing" instead of "this
        worker cannot say". On cuda/rocm the number is the allocator's own, which
        is what ``gpu_status()`` reports when NVML cannot answer.
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

    def _empty_device_cache(self) -> None:
        """Release cached device blocks for the device this worker actually uses."""
        try:
            import torch

            gc.collect()
            if self.device in ACCELERATOR_DEVICES:
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            elif self.device == "mps":
                mps = getattr(getattr(torch, "mps", None), "empty_cache", None)
                if mps is not None:
                    mps()
        except Exception:
            pass

    # -- rpc ------------------------------------------------------------
    def hello(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "protocol_version": PROTOCOL_VERSION,
                "engine_id": "qwen3_tts",
                "backend_name": BACKEND_NAME,
                "backend_version": self.backend_version,
                "capabilities": {
                    "streaming": False,
                    "pcm_native": False,
                    "custom_voice": True,
                    "voice_design": True,
                    "instruct_control": True,
                },
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
                "pid": os.getpid(),
            }
        )

    def load_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        try:
            self._load(alias)
        except Exception as exc:
            self._error(msg["id"], self._classify(exc), f"could not load {alias}: {exc}",
                        self._suggestion(exc))
            return
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
            }
        )

    def unload_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        self.loaded.pop(alias, None)
        # Drop this model's cached prompts: they hold tensors on its device.
        for key in [k for k in self.prompt_cache if k[0] == alias]:
            self.prompt_cache.pop(key, None)
        self._empty_device_cache()
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded),
                "gpu_vram_mb": self._vram_mb(),
                "device": self.device,
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
    def _generate(self, msg: dict, cancel: threading.Event):
        """Run the entry point for this request's mode with cancellation wired in."""
        model = self._load(msg["model_alias"])
        mode = msg.get("mode") or "clone"
        text = msg.get("text") or ""
        language = msg.get("language") or "Auto"
        seed = msg.get("seed")

        if self._torch is not None and seed is not None:
            self._torch.manual_seed(int(seed))

        # The custom-voice and voice-design entry points forward **kwargs to
        # model.generate(), so one CancelCriteria covers all three modes.
        stopping_criteria = [CancelCriteria(cancel)]

        if mode == "custom_voice":
            speaker = (msg.get("speaker") or "").strip()
            if not speaker:
                raise ValueError(
                    "custom_voice needs a preset speaker: pass speaker=<name> "
                    "(list_models() reports preset_speakers for this model)"
                )
            wavs, sr = model.generate_custom_voice(
                text=text,
                speaker=speaker,
                language=language,
                # None, not "", so the checkpoint treats it as "no instruction".
                instruct=(msg.get("instruct") or "").strip() or None,
                stopping_criteria=stopping_criteria,
            )
            return wavs[0], sr

        if mode == "voice_design":
            instruct = (msg.get("instruct") or "").strip()
            if not instruct:
                raise ValueError(
                    "voice_design needs a non-empty instruct: describe the voice "
                    "you want (e.g. 'A warm, low-pitched male narrator')"
                )
            wavs, sr = model.generate_voice_design(
                text=text,
                instruct=instruct,
                language=language,
                stopping_criteria=stopping_criteria,
            )
            return wavs[0], sr

        if mode != "clone":
            raise ValueError(
                f"unknown mode: {mode!r}: expected clone, custom_voice or voice_design"
            )

        ref_path = msg.get("ref_wav_path") or ""
        ref_text = msg.get("ref_text") or ""
        x_vector_only = bool(msg.get("x_vector_only_mode"))

        if not ref_path:
            # The *-Base checkpoints only do voice cloning; they ship no built-in
            # speakers. Passing an empty ref_audio raises an opaque
            # `Error opening ''`, so fail with something an agent can act on.
            raise ValueError(
                "no reference voice supplied: the Qwen3-TTS Base model can only "
                "clone, so pass voice=<voice_id> (create one with clone_voice) "
                "or register a default voice"
            )

        prompt = None
        prompt_path = msg.get("prompt_cache_path")
        if prompt_path:
            prompt = self._load_prompt(
                model, msg["model_alias"], ref_path, ref_text, x_vector_only, prompt_path
            )
        else:
            prompt = model.create_voice_clone_prompt(
                ref_audio=ref_path, ref_text=ref_text, x_vector_only_mode=x_vector_only
            )

        kwargs = {
            "text": text,
            "language": language,
            "stopping_criteria": stopping_criteria,
        }
        if prompt is not None:
            kwargs["voice_clone_prompt"] = prompt
        else:
            kwargs["ref_audio"] = ref_path
            kwargs["ref_text"] = ref_text

        wavs, sr = model.generate_voice_clone(**kwargs)
        return wavs[0], sr

    def _load_prompt(self, model, model_alias: str, ref_path: str, ref_text: str,
                     x_vector_only: bool, prompt_path: str):
        """Return a prompt for this reference voice, doing zero encoder work on a hit.

        Three tiers, cheapest first:
          1. in-memory LRU of live ``VoiceClonePromptItem`` lists -> pure dict lookup;
          2. ``prompt.pt`` + ``prompt_meta.json`` written by schema v2 -> dataclass
             reconstructed directly, no ``create_voice_clone_prompt`` at all;
          3. compute via the model and persist both tiers.
        """
        import torch
        from qwen_tts import VoiceClonePromptItem

        ref_sha = self._sha256(ref_path)
        key = (model_alias, ref_sha, ref_text, bool(x_vector_only), self.backend_version)

        cached = self.prompt_cache.get(key)
        if cached is not None:
            self.prompt_cache.move_to_end(key)
            return cached

        target = Path(prompt_path)
        meta_path = target.with_name("prompt_meta.json")

        if target.is_file() and meta_path.is_file():
            items = self._read_prompt_file(target, meta_path, model, key, VoiceClonePromptItem)
            if items is not None:
                self._remember_prompt(key, items)
                return items
            # Stale/corrupt cache must never be fatal: both files are gone, recompute.
            self._discard_prompt_files(target, meta_path)

        items = model.create_voice_clone_prompt(
            ref_audio=ref_path, ref_text=ref_text, x_vector_only_mode=x_vector_only
        )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            torch.save(self._prompt_to_state(items), target)
            meta_path.write_text(json.dumps(self._prompt_meta(key)), "utf-8")
        except Exception:
            pass
        self._remember_prompt(key, items)
        return items

    # -- prompt cache helpers -------------------------------------------
    def _remember_prompt(self, key: tuple, items: list) -> None:
        self.prompt_cache[key] = items
        self.prompt_cache.move_to_end(key)
        while len(self.prompt_cache) > PROMPT_CACHE_SIZE:
            self.prompt_cache.popitem(last=False)

    @staticmethod
    def _prompt_meta(key: tuple) -> dict:
        model_alias, ref_sha, ref_text, x_vector_only, backend_version = key
        return {
            "schema_version": PROMPT_SCHEMA_VERSION,
            "backend_version": backend_version,
            "ref_wav_sha256": ref_sha,
            "ref_text": ref_text,
            "x_vector_only_mode": x_vector_only,
            "model_alias": model_alias,
            "fields": ["ref_code", "ref_spk_embedding", "x_vector_only_mode", "icl_mode",
                       "ref_text"],
        }

    @staticmethod
    def _prompt_to_state(items: list) -> dict:
        """Flatten the (single-item) prompt into CPU tensors + plain Python scalars."""
        item = items[0]
        return {
            "ref_code": None if item.ref_code is None else item.ref_code.detach().cpu(),
            "ref_spk_embedding": item.ref_spk_embedding.detach().cpu(),
            "x_vector_only_mode": bool(item.x_vector_only_mode),
            "icl_mode": bool(item.icl_mode),
            "ref_text": "" if item.ref_text is None else str(item.ref_text),
        }

    def _read_prompt_file(self, target: Path, meta_path: Path, model, key: tuple,
                          dataclass_cls):
        """Rebuild prompt items from disk, or return None if the cache is unusable."""
        _model_alias, ref_sha, ref_text, x_vector_only, backend_version = key
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
            if meta.get("schema_version") != PROMPT_SCHEMA_VERSION:
                return None
            if (
                meta.get("ref_wav_sha256") != ref_sha
                or meta.get("ref_text") != ref_text
                or meta.get("backend_version") != backend_version
                or bool(meta.get("x_vector_only_mode")) != bool(x_vector_only)
            ):
                return None

            import torch

            state = torch.load(target, map_location="cpu", weights_only=True)
            if state.get("ref_spk_embedding") is None:
                return None
            xvec_only = bool(state["x_vector_only_mode"])
            icl_mode = bool(state["icl_mode"])
            ref_code = state.get("ref_code")
            if xvec_only and ref_code is not None:
                return None
            if icl_mode and ref_code is None:
                return None

            device = _model_device(model)
            # The consumer moves tensors itself (modeling_qwen3_tts.py:1963/2192), but
            # placing them up front avoids a per-call host->device copy.
            items = [
                dataclass_cls(
                    ref_code=None if ref_code is None else ref_code.to(device),
                    ref_spk_embedding=state["ref_spk_embedding"].to(device),
                    x_vector_only_mode=xvec_only,
                    icl_mode=icl_mode,
                    ref_text=state.get("ref_text") or ref_text or None,
                )
            ]
            return items
        except Exception:
            return None

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
            audio, sr = self._generate(msg, event)
            if event.is_set():
                self._error(request_id, "JOB_CANCELLED", "request was cancelled", "")
                return

            import numpy as np
            import soundfile as sf

            data = np.asarray(audio, dtype=np.float32)
            out = Path(msg.get("wav_path") or f"/tmp/qwen-{request_id}.wav")
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
            self._error(request_id, self._classify(exc), str(exc), self._suggestion(exc))
        finally:
            with self._lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def stream_pcm(self, msg: dict) -> None:
        """Emulated streaming: generate fully, then emit PCM in bounded chunks."""
        request_id = msg["id"]
        stream_id = uuid.UUID(msg["stream_id"])
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._lock:
            self.in_flight += 1
        try:
            audio, sr = self._generate(msg, event)
            if event.is_set():
                self._error(request_id, "JOB_CANCELLED", "request was cancelled", "")
                return

            import numpy as np

            data = np.asarray(audio, dtype=np.float32)
            if int(sr) != SAMPLE_RATE:
                # Bounded linear resample to the contract's 24 kHz.
                target_len = int(len(data) * SAMPLE_RATE / int(sr))
                data = np.interp(
                    np.linspace(0, len(data) - 1, target_len, dtype=np.float64),
                    np.arange(len(data)),
                    data,
                ).astype(np.float32)

            pcm = (np.clip(data, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            chunk_bytes = int(SAMPLE_RATE * STREAM_CHUNK_SECONDS) * 2
            produced = 0
            for offset in range(0, len(pcm), chunk_bytes):
                if event.is_set() or self.shutdown_event.is_set():
                    self._error(request_id, "JOB_CANCELLED", "request was cancelled", "")
                    return
                block = pcm[offset : offset + chunk_bytes]
                self.conn.send_pcm(stream_id, block)
                produced += len(block)

            self.conn.send_json(
                {
                    "id": request_id,
                    "stream_id": str(stream_id),
                    "done": True,
                    "duration_sec": produced / 2 / SAMPLE_RATE,
                    "streaming_mode": "emulated",
                    "gpu_vram_mb": self._vram_mb(),
                    "device": self.device,
                }
            )
        except Exception as exc:
            self._error(request_id, self._classify(exc), str(exc), self._suggestion(exc))
        finally:
            with self._lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def _classify(self, exc: BaseException) -> str:
        """Map a failure onto the daemon's taxonomy, aware of the device in use.

        ``CUDA_OOM`` is only truthful on a cuda/rocm worker. An allocation failure
        on cpu or mps is still an allocation failure, but reporting it as CUDA
        sends the caller to a knob that does not exist on that machine, so it is
        an INTERNAL_ERROR whose message keeps the allocator's own text.
        """
        message = str(exc)
        if is_oom(message, type(exc).__name__):
            return "CUDA_OOM" if self.device in ACCELERATOR_DEVICES else "INTERNAL_ERROR"
        if "no reference voice supplied" in message:
            return "VOICE_NOT_FOUND"
        if _client_error_suggestion(message):
            return "BAD_REQUEST"
        return "INTERNAL_ERROR"

    def _suggestion(self, exc: BaseException) -> str:
        """The one next step for this failure, phrased for the device in use."""
        client = _client_error_suggestion(str(exc))
        if client:
            return client
        if is_oom(str(exc), type(exc).__name__):
            if self.device in ACCELERATOR_DEVICES:
                return (
                    "Free device memory: close other GPU processes, or use the 0.6b "
                    "alias. `agent-voice doctor` reports free VRAM."
                )
            if self.device == "cpu":
                return (
                    "Free system RAM, or use the 0.6b alias: this model runs in "
                    "float32 on the CPU, so the 1.7B checkpoints need several GB."
                )
            return (
                "Free unified memory (quit other Apple apps), use the 0.6b alias, "
                "or run this engine on cpu with AGENT_VOICE_DEVICES=qwen3_tts=cpu."
            )
        return "Check the model is downloaded and the reference audio is valid."

    def _error(self, request_id: str, code: str, message: str, suggestion: str) -> None:
        self.conn.send_json(
            {
                "id": request_id,
                "error_code": code,
                "message": message,
                "suggestion": suggestion,
            }
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
    engine = QwenEngine(conn, _models_from_env())

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
