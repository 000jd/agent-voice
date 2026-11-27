"""Frozen IPC wire protocol, version 1.

IMMUTABLE CONTRACT. Parent daemon and every engine worker code against this.

Frame layout::

    frame := len_u32_be || type_u8 || payload

``len_u32_be`` counts the bytes AFTER the length field, i.e. ``1 + len(payload)``
(the type byte is included). Payloads:

* ``JSON_MSG`` (0x01) — UTF-8 JSON object. Every request and response carries a
  correlation ``id`` (UUID4 string) so ``health``, ``cancel`` and ``load_model``
  multiplex cleanly alongside an active ``stream_pcm``.
* ``PCM_CHUNK`` (0x02) — 16-byte raw UUID ``stream_id`` followed by raw
  24 kHz mono int16 little-endian PCM.

Import-safe without torch.
"""

from __future__ import annotations

import struct
import uuid
from typing import Final, Literal, Protocol, runtime_checkable

FRAME_JSON: Final[int] = 0x01
FRAME_PCM: Final[int] = 0x02

PROTOCOL_VERSION: Final[int] = 1
"""Wire protocol version exchanged in the ``hello`` handshake."""

LENGTH_PREFIX: Final[str] = ">I"
LENGTH_PREFIX_SIZE: Final[int] = 4
MAX_PAYLOAD_BYTES: Final[int] = 16 * 1024 * 1024

STREAM_ID_BYTES: Final[int] = 16

Method = Literal[
    "hello",
    "load_model",
    "unload_model",
    "synthesize",
    "stream_pcm",
    "transcribe",
    "cancel",
    "health",
    "shutdown",
]


def encode_frame(frame_type: int, payload: bytes) -> bytes:
    """Prefix a payload with its length and frame type."""
    if frame_type not in (FRAME_JSON, FRAME_PCM):
        raise ValueError(f"unknown frame type: {frame_type!r}")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload too large: {len(payload)} > {MAX_PAYLOAD_BYTES}")
    total = len(payload) + 1
    return struct.pack(LENGTH_PREFIX, total) + bytes((frame_type,)) + payload


def encode_json(obj: bytes) -> bytes:
    """Wrap already-serialised JSON bytes in a JSON_MSG frame."""
    return encode_frame(FRAME_JSON, obj)


def encode_pcm(stream_id: uuid.UUID, pcm: bytes) -> bytes:
    """Wrap raw PCM in a PCM_CHUNK frame tagged with its stream id."""
    return encode_frame(FRAME_PCM, stream_id.bytes + pcm)


def decode_header(header: bytes) -> tuple[int, int]:
    """Parse the 5-byte frame header. Returns (frame_type, payload_len)."""
    if len(header) != LENGTH_PREFIX_SIZE + 1:
        raise ValueError("header must be exactly 5 bytes")
    (total,) = struct.unpack(LENGTH_PREFIX, header[:LENGTH_PREFIX_SIZE])
    if total < 1:
        raise ValueError("frame length must be at least 1 (the type byte)")
    if total - 1 > MAX_PAYLOAD_BYTES:
        raise ValueError(f"frame too large: {total - 1} > {MAX_PAYLOAD_BYTES}")
    return header[LENGTH_PREFIX_SIZE], total - 1


def split_pcm_payload(payload: bytes) -> tuple[uuid.UUID, bytes]:
    """Split a PCM_CHUNK payload into (stream_id, pcm_bytes)."""
    if len(payload) < STREAM_ID_BYTES:
        raise ValueError("PCM payload shorter than a stream id")
    return uuid.UUID(bytes=payload[:STREAM_ID_BYTES]), payload[STREAM_ID_BYTES:]


def new_request_id() -> str:
    return str(uuid.uuid4())


def new_stream_id() -> uuid.UUID:
    return uuid.uuid4()


@runtime_checkable
class FrameWriter(Protocol):
    """Minimal sink a worker needs. Implemented by the transport."""

    def write_frame(self, frame: bytes) -> None: ...


# ---------------------------------------------------------------------------
# Message shapes (documentation + validation helpers; plain dicts on the wire)
# ---------------------------------------------------------------------------
#
# hello        req  {"id": str, "method": "hello"}
#              resp {"id": str, "protocol_version": int, "engine_id": str,
#                    "backend_name": str, "backend_version": str,
#                    "capabilities": {"streaming": bool, "pcm_native": bool},
#                    "loaded_models": [str], "gpu_vram_mb": int, "pid": int}
#                   ASR workers additionally report "kind": "asr". Optional:
#                   the parent reads the manifest, never the handshake, so an
#                   older worker that omits it is still conformant.
#
# load_model   req  {"id": str, "method": "load_model", "model_alias": str}
#              resp {"id": str, "model_alias": str, "loaded_models": [str]}
#
# unload_model req  {"id": str, "method": "unload_model", "model_alias": str}
#              resp {"id": str, "model_alias": str, "loaded_models": [str]}
#
# synthesize   req  {"id": str, "method": "synthesize", "model_alias": str,
#                    "text": str, "ref_wav_path": str, "ref_text": str,
#                    "x_vector_only_mode": bool, "prompt_cache_path": str|None,
#                    "language": str, "seed": int|None, "speed": float,
#                    "mode": str, "speaker": str|None, "instruct": str}
#                    mode/speaker/instruct are optional (default clone/None/"");
#                    omitting them keeps v1 behaviour exactly.
#              resp {"id": str, "wav_path": str, "sample_rate": int,
#                    "duration_sec": float, "model_alias": str,
#                    "backend_name": str, "backend_version": str}
#
# stream_pcm   req  same as synthesize plus "stream_id": str (uuid)
#              resp PCM_CHUNK* then
#                   {"id": str, "stream_id": str, "done": true,
#                    "duration_sec": float, "streaming_mode": "native"|"emulated"}
#
# transcribe   req  {"id": str, "method": "transcribe", "model_alias": str,
#                    "audio_path": str, "language": str|None,
#                    "word_timestamps": bool, "hotwords": [str]}
#                    Added in v0.5 for kind: asr engines. Additive and optional:
#                    a TTS worker answers BAD_REQUEST "unknown method", and an ASR
#                    worker answers that to synthesize. Framing is unchanged.
#              resp {"id": str, "model_alias": str, "backend_name": str,
#                    "backend_version": str, "language": str,
#                    "duration_sec": float, "text": str, "truncated": bool,
#                    "segments": [{"index": int, "start_sec": float,
#                                  "end_sec": float, "text": str,
#                                  "words": [{"word": str, "start_sec": float,
#                                             "end_sec": float}]}]}
#                    "words" is present only when word_timestamps was true;
#                    "segments" may be [] for silence-only audio.
#
# cancel       req  {"id": str, "method": "cancel", "target_request_id": str}
#              resp {"id": str, "target_request_id": str, "cancelled": bool}
#
# health       req  {"id": str, "method": "health"}
#              resp {"id": str, "ok": bool, "loaded_models": [str],
#                    "gpu_vram_mb": int, "in_flight": int}
#
# shutdown     req  {"id": str, "method": "shutdown"}
#              resp {"id": str, "ok": bool}
#
# error        resp {"id": str, "error_code": str, "message": str,
#                    "suggestion": str}
# ---------------------------------------------------------------------------

ERROR_CODES_DOC: Final[str] = (
    "WORKER_BUSY WORKER_CRASHED CUDA_OOM JOB_CANCELLED MODEL_NOT_FOUND "
    "ENGINE_NOT_FOUND ENGINE_NOT_INSTALLED VOICE_NOT_FOUND INVALID_REF_AUDIO "
    "TEXT_TOO_LONG PATH_OUTSIDE_BASE AGENT_MUTATION_DISABLED INVALID_SLUG "
    "MANIFEST_INVALID TIMEOUT BACKPRESSURE BAD_REQUEST INTERNAL_ERROR"
)
