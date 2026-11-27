#!/usr/bin/env python3
"""Mock ASR worker — a deterministic, timed transcript with no dependencies.

Purpose: prove the whole ``kind: asr`` path — transcribe request, word
timestamps, hotword plumbing, cancellation, segment shape — **without a model,
without numpy, without ffmpeg**. Everything here is stdlib, so this worker runs
in the core daemon's interpreter (``venv_python: null``) exactly like
``engines/mock``.

The transcript is derived from the WAV's real duration, not from its content:
segment ``i`` always carries the same sentence, so a test can assert on text and
timing at index ``i`` and stay stable across runs and across machines.

Audio: this worker only understands WAV. The parent always converts to 16 kHz
mono before calling, so no resampling lives here — but any sample rate is
accepted and read as-is, because rejecting a 44.1 kHz file would tell a caller
nothing useful.

``synthesize``/``stream_pcm`` are refused with ``BAD_REQUEST`` naming
``transcribe``: a TTS method sent to an ASR worker is a routing mistake, and
answering it with audio-shaped silence would hide the bug.

Skeleton (reader thread, compute thread, parent-death watchdog, Connection) is
copied from ``engines/mock/worker.py``, the reference implementation.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import signal
import struct
import sys
import threading
import time
import uuid
import wave

# --- IPC contract (duplicated deliberately: workers must not import the daemon) ---
FRAME_JSON = 0x01
FRAME_PCM = 0x02
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_SIZE = 4

PROTOCOL_VERSION = 1
BACKEND_NAME = "mock-asr"
BACKEND_VERSION = "1"

ENGINE_ID = "mock_asr"
KIND = "asr"

DEFAULT_ALIAS = "mock-asr-parity"
DEFAULT_MODELS: dict[str, str] = {DEFAULT_ALIAS: "local/mock-asr-parity"}

LANGUAGES: tuple[str, ...] = ("English",)
"""The only language this synthetic transcript speaks.

Mirrors ``engine.yaml`` ``capabilities.languages``: declaring more here would
make a caller believe a translation happened.
"""

CHUNK_SECONDS = 4.0
"""Target span of one segment. ~4 s is Kokoro-chunk-sized and keeps a 1 minute
clip at 8 segments after the cap below."""

MIN_SEGMENTS = 2
"""Never fewer than two: a one-segment transcript cannot prove the segment list,
the ``index`` field or a subtitle cue boundary is wired up at all."""

MAX_SEGMENTS = 8
"""Hard cap, so a one-hour file does not produce a 900-entry reply."""

SEGMENT_TEXTS: tuple[str, ...] = (
    "The first line of the mock transcript.",
    "The second line keeps the timing honest.",
    "A third line proves the segment list grows.",
    "The fourth line is still deterministic.",
    "A fifth line arrives without a decoder.",
    "The sixth line is the last fixed sentence.",
    "A seventh line only appears for long audio.",
    "The eighth line closes the capped transcript.",
)
"""Segment ``i`` is always ``SEGMENT_TEXTS[i % len(SEGMENT_TEXTS)]``.

Index-stable on purpose: a test asserts on a sentence at a given index, so the
tuple must never be reordered and the mapping must never depend on filesize,
language or hotwords.
"""

HOTWORD_TEMPLATE = "Heard the terms {terms} in this request."
"""Appended to the final segment when hotwords are supplied.

Appended to the existing text rather than emitted as an extra span so the
segment tiles keep covering the audio exactly once — a synthetic extra span
would either overlap the previous one or be zero-length.
"""

CANCEL_POLL_SECONDS = 0.02
"""How long each synthetic segment takes, and therefore the cancel granularity."""


class _BadRequest(ValueError):
    """A caller mistake. Answered as BAD_REQUEST, never as an engine failure."""

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


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


def _wav_duration(path: str) -> float:
    """Duration of a WAV in seconds, via the stdlib ``wave`` reader.

    Raises ``_BadRequest`` with an actionable suggestion for anything that is not
    a readable PCM WAV — the one thing this engine cannot compensate for, since
    decoding is deliberately out of scope.
    """
    if not path:
        raise _BadRequest(
            "audio_path is empty",
            "Pass the absolute path of a 16 kHz mono WAV. The daemon converts "
            "media to that shape before calling an ASR engine.",
        )
    try:
        with wave.open(path, "rb") as handle:
            frame_rate = handle.getframerate()
            frame_count = handle.getnframes()
    except FileNotFoundError:
        raise _BadRequest(
            f"audio file not found: {path}",
            "Check the path exists and is readable by the daemon, then retry.",
        ) from None
    except (wave.Error, EOFError) as exc:
        raise _BadRequest(
            f"not a readable PCM WAV: {path} ({exc})",
            "This engine only understands WAV. Convert first: "
            f"`ffmpeg -i input -ar 16000 -ac 1 {path}`.",
        ) from None
    except OSError as exc:
        raise _BadRequest(
            f"could not read {path}: {exc}",
            "Check the file permissions and that the path is not a directory.",
        ) from None

    if frame_rate <= 0:
        raise _BadRequest(
            f"{path} declares a sample rate of {frame_rate}",
            "Re-encode the file: a zero sample rate cannot be turned into timings.",
        )
    return frame_count / frame_rate


def _segment_count(duration_sec: float) -> int:
    """How many ~4 s tiles this audio becomes, clamped to [2, 8]."""
    if duration_sec <= 0:
        return MIN_SEGMENTS
    wanted = math.ceil(duration_sec / CHUNK_SECONDS)
    return max(MIN_SEGMENTS, min(MAX_SEGMENTS, wanted))


def _spans(duration_sec: float, count: int) -> list[tuple[float, float]]:
    """Evenly tile ``[0, duration_sec]`` into ``count`` spans, rounded to 4 dp."""
    step = duration_sec / count if count else 0.0
    return [
        (round(index * step, 4), round((index + 1) * step, 4)) for index in range(count)
    ]


def _word_timings(text: str, start_sec: float, end_sec: float) -> list[dict]:
    """Spread this segment's words evenly across its span.

    Deterministic by construction: the same text and span always produce the same
    list, so a subtitle test can assert exact numbers.
    """
    words = text.split()
    if not words:
        return []
    step = (end_sec - start_sec) / len(words)
    return [
        {
            "word": word,
            "start_sec": round(start_sec + index * step, 4),
            "end_sec": round(start_sec + (index + 1) * step, 4),
        }
        for index, word in enumerate(words)
    ]


def _segments(
    duration_sec: float, *, word_timestamps: bool, hotwords: list[str]
) -> list[dict]:
    """Build the deterministic segment list for ``duration_sec``.

    Segment ``index`` always carries ``SEGMENT_TEXTS[index]``; hotwords are folded
    into the final segment so the timed tiling still covers the audio once.
    """
    count = _segment_count(duration_sec)
    spans = _spans(duration_sec, count)
    texts = [SEGMENT_TEXTS[index % len(SEGMENT_TEXTS)] for index in range(count)]
    if hotwords:
        texts[-1] = f"{texts[-1]} {HOTWORD_TEMPLATE.format(terms=', '.join(hotwords))}"

    segments: list[dict] = []
    for index, (start_sec, end_sec) in enumerate(spans):
        segment: dict = {
            "index": index,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "text": texts[index],
        }
        if word_timestamps:
            segment["words"] = _word_timings(texts[index], start_sec, end_sec)
        segments.append(segment)
    return segments


class MockAsrEngine:
    def __init__(self, connection: Connection, models: dict[str, str]) -> None:
        self.conn = connection
        self.models = models
        self.loaded_models: list[str] = []
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._state_lock = threading.Lock()

    # -- helpers --------------------------------------------------------
    def _alias(self, msg: dict) -> str:
        alias = msg.get("model_alias") or DEFAULT_ALIAS
        if alias not in self.models:
            raise _BadRequest(
                f"unknown model alias: {alias!r}",
                f"Known aliases: {sorted(self.models)}. list_models() reports them.",
            )
        return alias

    @staticmethod
    def _language(msg: dict) -> str:
        """The reply language: always this engine's one fixed language.

        The manifest declares ``multilingual: false`` and a single language, so
        echoing a requested ``fr`` would describe a capability the engine does
        not have. A fixed-language checkpoint reports what it is, not what it was
        asked for; ``msg`` is accepted for signature parity with real engines.
        """
        return LANGUAGES[0]

    @staticmethod
    def _hotwords(msg: dict) -> list[str]:
        raw = msg.get("hotwords") or []
        if not isinstance(raw, list):
            raise _BadRequest(
                f"hotwords must be a list of strings, got {type(raw).__name__}",
                "Send hotwords as a JSON array, e.g. [\"Phonon\", \"agent-voice\"].",
            )
        return [str(term).strip() for term in raw if str(term).strip()]

    # -- rpc ------------------------------------------------------------
    def hello(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "protocol_version": PROTOCOL_VERSION,
                "engine_id": ENGINE_ID,
                "kind": KIND,
                "backend_name": BACKEND_NAME,
                "backend_version": BACKEND_VERSION,
                "capabilities": {
                    "streaming": False,
                    "pcm_native": False,
                    "voice_clone": False,
                    "custom_voice": False,
                    "voice_design": False,
                    "instruct_control": False,
                    "word_timestamps": True,
                    "hotwords": True,
                    "multilingual": False,
                },
                "languages": list(LANGUAGES),
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": 0,
                "pid": os.getpid(),
                "device": "cpu",
            }
        )

    def load_model(self, msg: dict) -> None:
        request_id = msg["id"]
        try:
            alias = self._alias(msg)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return
        if alias not in self.loaded_models:
            self.loaded_models.append(alias)
        self.conn.send_json(
            {
                "id": request_id,
                "model_alias": alias,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": 0,
                "device": "cpu",
            }
        )

    def unload_model(self, msg: dict) -> None:
        alias = msg.get("model_alias") or DEFAULT_ALIAS
        if alias in self.loaded_models:
            self.loaded_models.remove(alias)
        self.conn.send_json(
            {
                "id": msg["id"],
                "model_alias": alias,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": 0,
            }
        )

    def health(self, msg: dict) -> None:
        self.conn.send_json(
            {
                "id": msg["id"],
                "ok": True,
                "kind": KIND,
                "loaded_models": list(self.loaded_models),
                "gpu_vram_mb": 0,
                "in_flight": self.in_flight,
                "device": "cpu",
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

    # -- transcription --------------------------------------------------
    def transcribe(self, msg: dict) -> None:
        request_id = msg["id"]
        try:
            alias = self._alias(msg)
            hotwords = self._hotwords(msg)
            audio_path = str(msg.get("audio_path") or "")
            duration_sec = round(_wav_duration(audio_path), 4)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return

        language = self._language(msg)
        word_timestamps = bool(msg.get("word_timestamps"))

        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            segments = _segments(
                duration_sec, word_timestamps=word_timestamps, hotwords=hotwords
            )
            emitted: list[dict] = []
            for segment in segments:
                # Cooperative cancellation between segments: a synthetic decode
                # step cannot be interrupted mid-way, and 20 ms is the granularity
                # a barge-in test can actually observe.
                if event.wait(CANCEL_POLL_SECONDS) or self.shutdown_event.is_set():
                    self._cancelled(request_id)
                    return
                emitted.append(segment)

            text = " ".join(segment["text"] for segment in emitted)
            reply: dict = {
                "id": request_id,
                "model_alias": alias,
                "backend_name": BACKEND_NAME,
                "backend_version": BACKEND_VERSION,
                "language": language,
                "duration_sec": duration_sec,
                "text": text,
                "truncated": False,
                "segments": emitted,
            }
            if hotwords:
                reply["hotwords_applied"] = hotwords
            self.conn.send_json(reply)
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    # -- errors ---------------------------------------------------------
    def _cancelled(self, request_id: str) -> None:
        self.conn.send_json(
            {
                "id": request_id,
                "error_code": "JOB_CANCELLED",
                "message": "request was cancelled",
                "suggestion": "Retry without cancelling, or transcribe a shorter clip.",
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
    def dispatch(self, frame_type: int, payload: bytes) -> threading.Thread | None:
        if frame_type != FRAME_JSON:
            return None
        msg = json.loads(payload.decode("utf-8"))
        method = msg.get("method")
        request_id = msg.get("id", "")

        if method == "transcribe":
            # Compute runs off the reader thread so cancel/health stay responsive
            # while the segments are being emitted.
            thread = threading.Thread(target=self.transcribe, args=(msg,), daemon=True)
            thread.start()
            return thread

        if method in {"synthesize", "stream_pcm"}:
            # A TTS method on an ASR worker is a routing mistake: say which method
            # this engine does answer instead of pretending with empty audio.
            self._error(
                request_id,
                "BAD_REQUEST",
                f"unknown method: {method}",
                f"{ENGINE_ID} is a kind: asr engine and answers 'transcribe' "
                "(plus hello/load_model/unload_model/health/cancel/shutdown).",
            )
            return None

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
                request_id,
                "BAD_REQUEST",
                f"unknown method: {method}",
                f"{ENGINE_ID} answers: hello, load_model, unload_model, transcribe, "
                "cancel, health, shutdown (see ipc/protocol.py).",
            )
            return None
        handler(msg)
        return None


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
    engine = MockAsrEngine(conn, _models_from_env())

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
