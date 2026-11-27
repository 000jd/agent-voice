#!/usr/bin/env python3
"""Phonon (Fermion Research) worker — CPU-only ASR, IPC protocol v1.

Runs inside ``engines/phonon/.venv`` (torch==2.15.0+cpu from the explicit
pytorch-cpu index, fermion-research==0.2.15). Speaks the frozen v1 protocol
exactly like ``engines/mock/worker.py``: a dedicated reader thread keeps
``cancel``/``health`` answerable while a decode runs, and a parent-death watchdog
stops an orphaned worker from lingering.

Zero-GPU guarantees, in order of importance:

1. ``CUDA_VISIBLE_DEVICES`` is emptied *before* torch is imported, so the process
   never sees a device even if the daemon exported one.
2. ``FERMION_DEVICE=cpu`` is defaulted for the same reason: the backend's own
   device selection must not decide to use a GPU the manifest says it does not.
3. ``gpu_vram_mb`` is always 0 and no ``torch.cuda`` API is ever called.

The library is imported **inside** ``load_model``, never at module scope: a venv
whose torch/fermion install is broken still reaches ``hello`` (so the manager
gets a clean handshake and the failure lands on the load RPC as
``MODEL_NOT_FOUND``) instead of dying before the IPC connection exists.

Verified API surface — do not invent beyond it:

* distribution ``fermion-research``, import name ``fermion``; CLI binaries
  ``phonon`` and ``fermion``.
* ``fermion.load_speech(alias)`` where the alias is exactly the manifest alias.
* ``speech.transcribe_detailed(path, hotwords=[...])`` → an object with
  documented ``.text`` and ``.words``. ``.segments`` may or may not exist, so
  both shapes are handled here. ``hotwords=`` was measured by the spike.
* ``hotword_lambda=`` is *not* confirmed by that measurement: it is sent
  optimistically and dropped with one retry if the backend rejects it. A missing
  kwarg degrades the bias, never the transcription.
* Audio must already be 16 kHz mono; the parent always writes that WAV, so no
  conversion happens in this process.
* There is no confidence field and no per-request language control on 2.5 (it
  auto-detects), so no ``language=`` kwarg is ever sent.
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
from typing import Any

# --- zero-GPU, applied BEFORE torch/fermion import ---------------------------
# Overwritten, not setdefault: a CUDA device exported by the daemon must not
# reach this process. torch reads this at import time, hence the module scope.
os.environ["CUDA_VISIBLE_DEVICES"] = ""
os.environ["FERMION_DEVICE"] = "cpu"
# stderr is captured by the daemon as the crash report; progress bars are noise.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

# --- IPC contract (duplicated deliberately: workers must not import the daemon) ---
FRAME_JSON = 0x01
FRAME_PCM = 0x02
LENGTH_PREFIX = ">I"
LENGTH_PREFIX_SIZE = 4

PROTOCOL_VERSION = 1
BACKEND_NAME = "fermion-research"
BACKEND_VERSION = "0.2.15"
"""Fallback pin; a loaded handle reports the real version when it can."""

ENGINE_ID = "phonon"
KIND = "asr"

# The pinned distribution and import name differ on purpose; both are spelled out
# so a reader never has to guess which one `pip` talks about and which one
# Python imports.
DISTRIBUTION = "fermion-research"
MODULE = "fermion"

DEFAULT_ALIAS = "phonon-2.5"
DEFAULT_MODELS: dict[str, str] = {
    "phonon-2.5": "FermionResearch/Phonon-2.5",
    "phonon-2.5-en": "FermionResearch/Phonon-2.5-English",
    "phonon-2": "FermionResearch/Phonon-2",
}

LANGUAGES: tuple[str, ...] = (
    "en", "es", "fr", "de", "it", "pt", "nl", "pl", "ru", "uk", "ro", "cs", "sk",
    "bg", "hr", "sv", "da", "fi", "et", "lv", "lt", "hu", "sl", "mt", "el",
)
"""Must stay identical to ``engine.yaml``'s ``capabilities.languages``."""

HOTWORD_LAMBDA = 2.0
"""Bias strength for a request's hotwords.

Kept below the vendor's own 2.5 default: this engine's bias is a nudge toward a
caller's vocabulary, and over-weighting it is how a decoder starts hallucinating
the word it was told to expect. The caller can raise the effect by sending a
better list, never by sending a magic number.
"""

SENTENCE_ENDINGS: tuple[str, ...] = (".", "!", "?")
"""Phonon's documented segment closers."""

CLOSING_MARKS: tuple[str, ...] = ('"', "'", ")", "]", "}", "\u201d", "\u2019")
"""Punctuation that may follow a sentence closer inside the same word token.

A transcript word is often ``done."`` or ``really?)``: the sentence ends at the
period/question mark, not after the quote. Stripping these before the
endswith check keeps the documented rule ("a word ending in sentence
punctuation closes a segment") true for punctuated speech.
"""

DEFAULT_MAX_SECONDS = 7.0
DEFAULT_PAUSE_SECONDS = 0.8
"""Phonon's documented segment limits; see :func:`group_words_into_segments`."""


class _BadRequest(ValueError):
    """A caller mistake. Answered as BAD_REQUEST, never as an engine failure."""

    def __init__(self, message: str, suggestion: str) -> None:
        super().__init__(message)
        self.suggestion = suggestion


class _MissingModel(RuntimeError):
    """The weights for this alias are not on this machine / failed to load.

    Carries the underlying failure's text already formatted, so an error reply can
    print it verbatim instead of prefixing it twice ("could not load X: could not
    load X: ...").
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


MISSING_WEIGHT_MARKERS: tuple[str, ...] = (
    "no such file",
    "file not found",
    "model not found",
    "localentrynotfound",
    "offlinemodeisenabled",
    "connectionerror",
    "couldn't connect",
    "could not download",
    "client error",
    "repository not found",
    "entry not found",
)
"""Substrings that mean "the weights are not on this machine", not "it broke"."""


def _is_missing_weights(exc: BaseException) -> bool:
    """Whether this failure means "the weights are not here", not "it broke"."""
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in MISSING_WEIGHT_MARKERS)


BACKEND_FAILURES: tuple[type[BaseException], ...] = (Exception, SystemExit)
"""What a backend call may raise, including ``SystemExit``.

``fermion``'s own ``_resolve`` raises ``SystemExit`` for an unknown speech model
— a CLI-shaped error, not an ``Exception``. Letting that escape a compute thread
would kill the thread silently and leave the daemon waiting out its full IPC
timeout, so it is caught here and answered as ``MODEL_NOT_FOUND`` with the
message the library produced. ``KeyboardInterrupt`` is deliberately NOT in this
tuple: a real interrupt must still unwind the process.
"""


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


# ---------------------------------------------------------------------------
# Word -> segment grouping (Phonon's own documented rules)
# ---------------------------------------------------------------------------


def _ends_sentence(word: str) -> bool:
    """True when ``word`` closes a sentence, quote or bracket after it included."""
    return word.rstrip("".join(CLOSING_MARKS)).endswith(SENTENCE_ENDINGS)


def _transcribe_detailed(
    speech: Any, audio_path: str, hotwords: list[str] | None
) -> Any:
    """Call the backend with the hotword bias, tolerating an older signature.

    The spike proved ``hotwords=`` and not ``hotword_lambda=``. If this backend
    rejects the latter, retry once without it: a weaker bias is a degraded
    feature, while an exception here would fail every transcription.
    """
    try:
        return speech.transcribe_detailed(
            audio_path,
            hotwords=hotwords or None,
            hotword_lambda=HOTWORD_LAMBDA,
        )
    except TypeError as exc:
        if "hotword_lambda" not in str(exc):
            raise
        print(
            "phonon worker: backend does not accept hotword_lambda; "
            "retrying with hotwords only",
            file=sys.stderr,
        )
        return speech.transcribe_detailed(audio_path, hotwords=hotwords or None)


def group_words_into_segments(
    words: list[dict],
    *,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    pause_seconds: float = DEFAULT_PAUSE_SECONDS,
) -> list[dict]:
    """Group timed words into segments using Phonon's documented rules.

    A segment closes at whichever of these comes first:

    * a word ending in sentence punctuation (``.``/``!``/``?``),
    * a silence gap ``>= pause_seconds`` before the next word,
    * the segment would exceed ``max_seconds`` of audio.

    These are Phonon's own documented segmentation rules, not a heuristic of our
    invention, which is why this lives as a module-level function with no engine
    state: a future ``faster_whisper`` worker should reuse it verbatim rather
    than re-deriving the same three thresholds.

    ``words`` are already normalised to ``{"word", "start_sec", "end_sec"}`` (the
    worker normalises before calling). Times in the result are copied from the
    first and last word of each group, never re-estimated, so a subtitle cue is
    exactly as long as the words it contains.
    """
    segments: list[dict] = []
    current: list[dict] = []

    def close() -> None:
        if not current:
            return
        segments.append(
            {
                "index": len(segments),
                "start_sec": current[0]["start_sec"],
                "end_sec": current[-1]["end_sec"],
                "text": " ".join(entry["word"] for entry in current).strip(),
                "words": list(current),
            }
        )
        current.clear()

    for entry in words:
        if current:
            previous_end = current[-1]["end_sec"]
            gap = entry["start_sec"] - previous_end
            would_span = entry["end_sec"] - current[0]["start_sec"]
            if gap >= pause_seconds or would_span > max_seconds:
                close()
        current.append(entry)
        if _ends_sentence(entry["word"]):
            close()

    close()
    return segments


def _normalise_words(raw: Any) -> list[dict]:
    """Normalise ``result.words`` to ``{"word", "start_sec", "end_sec"}``.

    Two shapes exist in the wild: a dict with ``text`` and a dict with ``word``.
    Both carry ``start``/``end``. Anything without usable timing is dropped
    rather than given a fabricated timestamp.
    """
    words: list[dict] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            word = entry.get("word") or entry.get("text") or ""
            start = entry.get("start", entry.get("start_sec"))
            end = entry.get("end", entry.get("end_sec"))
        else:
            # An object with attributes is equally plausible; read defensively.
            word = getattr(entry, "word", None) or getattr(entry, "text", None) or ""
            start = getattr(entry, "start", None)
            end = getattr(entry, "end", None)
        label = str(word).strip()
        if not label or start is None or end is None:
            continue
        try:
            words.append(
                {"word": label, "start_sec": float(start), "end_sec": float(end)}
            )
        except (TypeError, ValueError):
            continue
    return words


def _normalise_segments(raw: Any, words: list[dict]) -> list[dict]:
    """Normalise ``result.segments`` when the backend provides them.

    ``.segments`` may or may not exist on this backend's detailed result, so
    every entry is inspected rather than assumed: a segment without ``start``/
    ``end``/``text`` is skipped, and the caller falls back to
    :func:`group_words_into_segments` when nothing usable survives.

    A segment that carries no words of its own inherits the flat word list
    entries that fall inside its span. Without that, asking for word timestamps
    on a backend that segments would silently return segments with no words —
    the request said it wanted them.
    """
    segments: list[dict] = []
    for entry in raw or []:
        if isinstance(entry, dict):
            text = entry.get("text") or ""
            start = entry.get("start", entry.get("start_sec"))
            end = entry.get("end", entry.get("end_sec"))
            entry_words = entry.get("words")
        else:
            text = getattr(entry, "text", "") or ""
            start = getattr(entry, "start", getattr(entry, "start_sec", None))
            end = getattr(entry, "end", getattr(entry, "end_sec", None))
            entry_words = getattr(entry, "words", None)
        if start is None or end is None or not str(text).strip():
            continue
        try:
            start_sec, end_sec = float(start), float(end)
        except (TypeError, ValueError):
            continue
        segment = {
            "index": len(segments),
            "start_sec": start_sec,
            "end_sec": end_sec,
            "text": str(text).strip(),
        }
        if entry_words is not None:
            segment["words"] = _normalise_words(entry_words)
        elif words:
            # Half-open on the left, closed on the right, so a word sitting
            # exactly on a boundary lands in the later segment only.
            segment["words"] = [
                word
                for word in words
                if word["start_sec"] >= start_sec and word["end_sec"] <= end_sec
            ]
        segments.append(segment)
    if segments:
        return segments
    return group_words_into_segments(words)


def _duration_sec(audio_path: str, words: list[dict]) -> float:
    """Best-effort audio length: the file first, the last word second, else 0.

    Never raises: a transcript is still useful when the container is unreadable,
    and a wrong duration in a reply is far cheaper than a failed transcription.
    """
    try:
        import soundfile as sf

        info = sf.info(audio_path)
        frames, rate = float(info.frames), float(info.samplerate)
        if rate > 0:
            return round(frames / rate, 4)
    except Exception:
        pass
    if words:
        try:
            return round(float(words[-1]["end_sec"]), 4)
        except (KeyError, TypeError, ValueError):
            pass
    return 0.0


class PhononEngine:
    def __init__(self, connection: Connection, models: dict[str, str]) -> None:
        self.conn = connection
        self.models = models
        self.loaded_models: list[str] = []
        self.speech: dict[str, Any] = {}
        """alias -> a loaded ``fermion`` speech handle (opaque to this worker)."""
        self.cancel_events: dict[str, threading.Event] = {}
        self.shutdown_event = threading.Event()
        self.in_flight = 0
        self._state_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.backend_version = BACKEND_VERSION

    # -- model ----------------------------------------------------------
    def _repo(self, alias: str) -> str:
        repo = self.models.get(alias)
        if not repo:
            raise _BadRequest(
                f"unknown model alias: {alias!r}",
                f"Known aliases: {sorted(self.models)}. list_models() reports them.",
            )
        return repo

    def _load(self, alias: str) -> Any:
        """Load (or return) the backend handle for ``alias``.

        Serialised: two concurrent transcribes must not unpack the same model
        twice and double the resident memory. The import happens here, inside the
        handler, so an unusable venv is a clean MODEL_NOT_FOUND reply rather than
        a process that dies before ``hello``.
        """
        handle = self.speech.get(alias)
        if handle is not None:
            return handle
        self._repo(alias)

        with self._load_lock:
            handle = self.speech.get(alias)
            if handle is not None:
                return handle
            try:
                import fermion
            except Exception as exc:
                raise _MissingModel(
                    f"could not import {MODULE} (distribution {DISTRIBUTION}): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            # First load can take ~20 s (unpack + compile). That is expected, not
            # a hang: the daemon's load timeout is 600 s and transcribe's is 3600 s.
            try:
                handle = fermion.load_speech(alias)
            except BACKEND_FAILURES as exc:
                # Every load failure is MODEL_NOT_FOUND at the IPC layer: the
                # overwhelmingly common cause is "the weights are not on this
                # machine", and the suggestion names the download command either
                # way. Re-raising a raw exception would instead look like a crash.
                raise _MissingModel(
                    f"could not load {alias}: {type(exc).__name__}: {exc}"
                ) from exc

            self.speech[alias] = handle
            if alias not in self.loaded_models:
                self.loaded_models.append(alias)
            return handle

    # -- request validation ---------------------------------------------
    @staticmethod
    def _audio_path(msg: dict) -> str:
        path = str(msg.get("audio_path") or "").strip()
        if not path:
            raise _BadRequest(
                "audio_path is empty",
                "Pass the absolute path of a 16 kHz mono WAV; the daemon writes "
                "exactly that before calling an ASR engine.",
            )
        if not Path(path).is_file():
            raise _BadRequest(
                f"audio file not found: {path}",
                "Check the path exists and is readable, then retry.",
            )
        return path

    @staticmethod
    def _hotwords(msg: dict) -> list[str]:
        raw = msg.get("hotwords") or []
        if not isinstance(raw, list):
            raise _BadRequest(
                f"hotwords must be a list of strings, got {type(raw).__name__}",
                "Send hotwords as a JSON array, e.g. [\"agent-voice\", \"Fermion\"].",
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
                "backend_version": self.backend_version,
                "capabilities": {
                    "streaming": False,
                    "pcm_native": False,
                    "voice_clone": False,
                    "custom_voice": False,
                    "voice_design": False,
                    "instruct_control": False,
                    "word_timestamps": True,
                    "hotwords": True,
                    "multilingual": True,
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
        alias = msg.get("model_alias") or DEFAULT_ALIAS
        try:
            self._load(alias)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return
        except (Exception, SystemExit) as exc:
            self._error(
                request_id,
                "MODEL_NOT_FOUND",
                self._load_failure(alias, exc),
                self._suggestion(alias, exc),
            )
            return
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
        self.speech.pop(alias, None)
        if alias in self.loaded_models:
            self.loaded_models.remove(alias)
        # The handle owns the weights; dropping the reference is only half of it.
        import gc

        gc.collect()
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
            alias = msg.get("model_alias") or DEFAULT_ALIAS
            audio_path = self._audio_path(msg)
            hotwords = self._hotwords(msg)
            self._repo(alias)
        except _BadRequest as exc:
            self._bad_request(request_id, exc)
            return

        event = threading.Event()
        self.cancel_events[request_id] = event
        with self._state_lock:
            self.in_flight += 1
        try:
            # A transcribe must not require a prior load_model: the manager asks
            # for residency, but a direct IPC caller may not have.
            try:
                speech = self._load(alias)
            except _BadRequest as exc:
                self._bad_request(request_id, exc)
                return
            except (Exception, SystemExit) as exc:
                self._error(
                    request_id,
                    "MODEL_NOT_FOUND",
                    self._load_failure(alias, exc),
                    self._suggestion(alias, exc),
                )
                return

            try:
                result = _transcribe_detailed(speech, audio_path, hotwords)
            except BACKEND_FAILURES as exc:
                # SystemExit included: the library reports "no such file" and
                # similar CLI-shaped failures that way, and an escaping BaseException
                # would kill this thread and leave the parent waiting out its timeout.
                self._error(
                    request_id,
                    "INTERNAL_ERROR",
                    f"transcribe_detailed failed: {type(exc).__name__}: {exc}",
                    "Check the worker's stderr (`agent-voice doctor`) and retry.",
                )
                return

            if event.is_set() or self.shutdown_event.is_set():
                self._cancelled(request_id)
                return

            words = _normalise_words(getattr(result, "words", None) or [])
            segments = _normalise_segments(getattr(result, "segments", None), words)
            text = (getattr(result, "text", "") or "").strip()
            if not text:
                text = " ".join(segment["text"] for segment in segments).strip()

            want_words = bool(msg.get("word_timestamps"))
            reply_segments: list[dict] = []
            for segment in segments:
                if event.is_set() or self.shutdown_event.is_set():
                    self._cancelled(request_id)
                    return
                emitted: dict = {
                    "index": segment["index"],
                    "start_sec": round(float(segment["start_sec"]), 4),
                    "end_sec": round(float(segment["end_sec"]), 4),
                    "text": segment["text"],
                }
                # Per-word timings only when asked: they are most of the payload.
                if want_words and segment.get("words"):
                    emitted["words"] = [
                        {
                            "word": entry["word"],
                            "start_sec": round(float(entry["start_sec"]), 4),
                            "end_sec": round(float(entry["end_sec"]), 4),
                        }
                        for entry in segment["words"]
                    ]
                reply_segments.append(emitted)

            detected = getattr(result, "language", "") or ""
            self.conn.send_json(
                {
                    "id": request_id,
                    "model_alias": alias,
                    "backend_name": BACKEND_NAME,
                    "backend_version": self.backend_version,
                    # "auto" when the backend cannot say. Never an invented code:
                    # 2.5 auto-detects and may not expose what it decided.
                    "language": str(detected) or "auto",
                    "duration_sec": _duration_sec(audio_path, words),
                    "text": text,
                    "truncated": bool(getattr(result, "truncated", False)),
                    "segments": reply_segments,
                }
            )
        finally:
            with self._state_lock:
                self.in_flight -= 1
            self.cancel_events.pop(request_id, None)

    # -- errors ---------------------------------------------------------
    @staticmethod
    def _load_failure(alias: str, exc: BaseException) -> str:
        """One-line explanation of a failed load, without repeating itself.

        ``_MissingModel`` already describes the alias and the cause, so its text is
        used verbatim; any other exception (including a ``SystemExit`` from the
        library's model resolver) is wrapped once, here.
        """
        if isinstance(exc, _MissingModel):
            return str(exc)
        return f"could not load {alias}: {type(exc).__name__}: {exc}"

    @staticmethod
    def _suggestion(alias: str, exc: BaseException) -> str:
        """The one next step a caller can take when a model will not load.

        Always names the download command: a load failure is answered as
        MODEL_NOT_FOUND, and "the weights are not here" is by far the most common
        cause of one on a fresh machine. When the failure is clearly something
        else, the stderr hint is appended rather than replacing the fix.
        """
        suggestion = (
            f"Run `agent-voice models download {ENGINE_ID} {alias}` and retry. "
            f"If {DISTRIBUTION} is not installed: "
            f"`agent-voice engines install {ENGINE_ID}`."
        )
        if not (isinstance(exc, _MissingModel) or _is_missing_weights(exc)):
            suggestion += " The worker's stderr (`agent-voice doctor`) has the details."
        return suggestion

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
            # Decode runs off the reader thread so cancel/health stay answerable.
            thread = threading.Thread(target=self.transcribe, args=(msg,), daemon=True)
            thread.start()
            return thread

        if method in {"synthesize", "stream_pcm"}:
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
    engine = PhononEngine(conn, _models_from_env())

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
