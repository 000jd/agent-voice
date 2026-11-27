#!/usr/bin/env python3
"""Mock engine worker.

Purpose: prove the whole IPC contract, the lifecycle manager, streaming and
cancellation **without a GPU or torch**. It emits a shaped tone whose length is
derived from the input text and whose pitch is derived from
``mode``/``speaker``/``instruct``, so tests can assert on duration and tell two
renders apart deterministically.

This file is also the reference implementation for `engines/ADDING_AN_ENGINE.md`:
* a dedicated reader thread so `cancel` is honoured while "inference" runs
* a separate compute thread so the reader is never blocked
* parent-death watchdog so an orphaned worker cannot linger
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import signal
import struct
import sys
import threading
import time
import uuid
from array import array
from pathlib import Path

# --- IPC contract (duplicated deliberately: workers must not import the daemon) ---
FRAME_JSON = 0x01
FRAME_PCM = 0x02
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_SIZE = 4

PROTOCOL_VERSION = 1
BACKEND_NAME = "mock"
BACKEND_VERSION = "1"
SAMPLE_RATE = 24_000
MODES = ("clone", "custom_voice", "voice_design")

RESOLVED_DEVICE = (os.environ.get("AGENT_VOICE_RESOLVED_DEVICE") or "cpu").strip().lower()
"""The device the daemon resolved for this engine.

Read from the environment, default ``cpu``: the tone generator has no torch
and no accelerator path (``engine.yaml`` declares cpu only), so cpu is the
only truthful answer and the one a worker started outside the daemon gets.
"""


class _BadRequest(ValueError):
    """A caller mistake. Answered as BAD_REQUEST, never as an engine failure."""

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


def _voice_params(msg: dict) -> tuple[str, str | None, str]:
    """Validate and normalise ``(mode, speaker, instruct)`` from a request.

    Absent keys mean v1 behaviour: clone with no preset speaker and no
    instruction.
    """
    mode = msg.get("mode") or "clone"
    if mode not in MODES:
        raise _BadRequest(
            f"unknown mode: {mode!r}",
            "Use mode='clone' (reference voice), 'custom_voice' (preset speaker) "
            "or 'voice_design' (describe the voice in instruct).",
        )
    speaker = (msg.get("speaker") or "").strip() or None
    instruct = (msg.get("instruct") or "").strip()
    if mode == "custom_voice" and speaker is None:
        raise _BadRequest(
            "custom_voice requires a speaker",
            "Pass speaker=<preset name>; list_models() reports preset_speakers "
            "for each model.",
        )
    if mode == "voice_design" and not instruct:
        raise _BadRequest(
            "voice_design requires a non-empty instruct",
            "Describe the target voice in instruct, e.g. 'A warm, low-pitched "
            "male narrator'.",
        )
    return mode, speaker, instruct


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


class MockEngine:
    def __init__(self, connection: Connection) -> None:
        self.conn = connection
        self.loaded_models: list[str] = []
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._state_lock = threading.Lock()
        self.device = RESOLVED_DEVICE

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _text_duration(text: str, speed: float) -> float:
        """Deterministic pseudo-duration from the text, respecting speed."""
        words = max(1, len(text.split()))
        base = max(0.4, min(20.0, words / 2.6))
        return max(0.1, base / max(0.25, min(4.0, speed)))

    def _render_pcm(self, seconds: float, freq: float) -> bytes:
        frames = int(seconds * SAMPLE_RATE)
        samples = array("h")
        amplitude = 6000
        for i in range(frames):
            # Gentle fade in/out so tests can measure silence at the edges.
            envelope = min(1.0, i / 240.0, (frames - i) / 240.0)
            value = amplitude * envelope * math.sin(2 * math.pi * freq * i / SAMPLE_RATE)
            samples.append(int(value))
        return samples.tobytes()

    @staticmethod
    def _tone_hz(mode: str, speaker: str | None, instruct: str) -> float:
        """Deterministic pitch for this ``clone``/``custom_voice``/``voice_design`` request.

        Every distinct (mode, speaker, instruct) must render different bytes —
        that is what makes a cache-isolation test fail loudly when the cache key
        forgets one of the three.
        """
        digest = hashlib.sha256(f"{mode}:{speaker or ''}:{instruct}".encode()).hexdigest()
        # 180-420 Hz in 0.1 Hz steps: always audible, ~2400 distinct pitches, so
        # two real requests colliding on a pitch is negligible.
        return 180.0 + (int(digest[:8], 16) % 2400) / 10.0

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        data_size = len(pcm)
        header = b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVEfmt "
        header += struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16)
        header += b"data" + struct.pack("<I", data_size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header + pcm)

    # -- rpc ------------------------------------------------------------
    def hello(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "protocol_version": PROTOCOL_VERSION,
                "engine_id": "mock",
                "backend_name": BACKEND_NAME,
                "backend_version": BACKEND_VERSION,
                "capabilities": {
                    "streaming": True,
                    "pcm_native": True,
                    "custom_voice": True,
                    "voice_design": True,
                    "instruct_control": True,
                },
                "loaded_models": self.loaded_models,
                "gpu_vram_mb": 0,
                "device": self.device,
                "pid": os.getpid(),
            }
        )

    def load_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        if alias not in self.loaded_models:
            self.loaded_models.append(alias)
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": self.loaded_models,
                "gpu_vram_mb": 0,
                "device": self.device,
            }
        )

    def unload_model(self, msg: dict) -> None:
        alias = msg["model_alias"]
        if alias in self.loaded_models:
            self.loaded_models.remove(alias)
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": self.loaded_models,
                "gpu_vram_mb": 0,
                "device": self.device,
            }
        )

    def health(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "ok": True,
                "loaded_models": self.loaded_models,
                "gpu_vram_mb": 0,
                "device": self.device,
                "in_flight": self.in_flight,
            }
        )

    def cancel(self, msg: dict) -> None:
        target = msg["target_request_id"]
        event = self.cancel_events.get(target)
        cancelled = False
        if event is not None:
            event.set()
            cancelled = True
        self.conn.send_json(
            {"id": msg["id"], "target_request_id": target, "cancelled": cancelled}
        )

    def shutdown(self, msg: dict) -> None:
        for event in self.cancel_events.values():
            event.set()
        self.shutdown_event.set()
        self.conn.send_json({"id": msg["id"], "ok": True})

    def synthesize(self, msg: dict) -> None:
        request_id = msg["id"]
        try:
            mode, speaker, instruct = _voice_params(msg)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            text = msg.get("text") or ""
            speed = float(msg.get("speed") or 1.0)
            seconds = self._text_duration(text, speed)

            if event.wait(min(seconds, 0.25)):
                self._cancelled(request_id)
                return

            pcm = self._render_pcm(seconds, self._tone_hz(mode, speaker, instruct))
            output = Path(msg.get("wav_path") or f"/tmp/mock-{request_id}.wav")
            self._write_wav(output, pcm)
            self.conn.send_json(
                {
                    "id": request_id,
                    "wav_path": str(output),
                    "sample_rate": SAMPLE_RATE,
                    "duration_sec": seconds,
                    "model_alias": msg.get("model_alias", "mock-tone"),
                    "backend_name": BACKEND_NAME,
                    "backend_version": BACKEND_VERSION,
                    "gpu_vram_mb": 0,
                    "device": self.device,
                }
            )
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    def stream_pcm(self, msg: dict) -> None:
        request_id = msg["id"]
        stream_id = uuid.UUID(msg["stream_id"])
        try:
            mode, speaker, instruct = _voice_params(msg)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return
        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            text = msg.get("text") or ""
            speed = float(msg.get("speed") or 1.0)
            seconds = self._text_duration(text, speed)
            pcm = self._render_pcm(seconds, self._tone_hz(mode, speaker, instruct))

            chunk_frames = SAMPLE_RATE // 10  # 100 ms
            chunk_bytes = chunk_frames * 2
            produced = 0
            for offset in range(0, len(pcm), chunk_bytes):
                if event.is_set() or self.shutdown_event.is_set():
                    self._cancelled(request_id)
                    return
                self.conn.send_pcm(stream_id, pcm[offset : offset + chunk_bytes])
                produced += len(pcm[offset : offset + chunk_bytes])
                # Simulate decode latency so backpressure tests are meaningful.
                time.sleep(0.005)

            self.conn.send_json(
                {
                    "id": request_id,
                    "stream_id": str(stream_id),
                    "done": True,
                    "duration_sec": produced / 2 / SAMPLE_RATE,
                    "streaming_mode": "native",
                    "gpu_vram_mb": 0,
                    "device": self.device,
                }
            )
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

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

    # -- dispatch -------------------------------------------------------
    def dispatch(self, frame_type: int, payload: bytes) -> threading.Thread | None:
        if frame_type != FRAME_JSON:
            return None
        msg = json.loads(payload.decode("utf-8"))
        method = msg.get("method")

        if method in {"synthesize", "stream_pcm"}:
            # Run compute off the reader thread so cancel/health stay responsive.
            target = self.synthesize if method == "synthesize" else self.stream_pcm
            thread = threading.Thread(target=target, args=(msg,), daemon=True)
            thread.start()
            return thread

        handler = {
            "hello": self.hello,
            "load_model": self.load_model,
            "unload_model": self.unload_model,
            "health": self.health,
            "cancel": self.cancel,
            "shutdown": self.shutdown,
        }.get(method)
        if handler is None:
            self.conn.send_json(
                {
                    "id": msg.get("id", ""),
                    "error_code": "BAD_REQUEST",
                    "message": f"unknown method: {method}",
                    "suggestion": "Check ipc/protocol.py for the supported method list.",
                }
            )
            return None
        handler(msg)
        return None


def _connect():
    kind = os.environ.get("AGENT_VOICE_IPC_TRANSPORT", "unix")
    import socket

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
    engine = MockEngine(conn)

    while not engine.shutdown_event.is_set():
        try:
            frame_type, payload = conn.recv()
        except (ConnectionError, OSError):
            break
        try:
            engine.dispatch(frame_type, payload)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            try:
                conn.send_json(
                    {
                        "id": "",
                        "error_code": "BAD_REQUEST",
                        "message": f"malformed request: {exc}",
                        "suggestion": "Send a JSON object with an id and method.",
                    }
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
