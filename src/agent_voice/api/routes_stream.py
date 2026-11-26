"""PCM streaming over HTTP chunked transfer, plus a WebSocket with barge-in.

Backpressure is enforced here, not in the worker: the worker's PCM queue is
bounded, and if the client stops reading we cancel the worker rather than buffer
audio without limit.

Streaming shares the one text pipeline with the file routes
(:mod:`agent_voice.services.utterance_service`), which means markup, the scoped
lexicon and code normalization behave identically here. The difference is that a
stream has no stitch step: an utterance that expands into several segments is
produced by opening one worker stream per speech segment, in order, and writing
int16 silence between them. A pause in a stream is therefore literal silence at
the exact requested length — there is no crossfade seam to compensate for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..audio_utils import scale_pcm16, silence_pcm16
from ..markup import parse_markup
from ..pronunciations import check_pronunciation_map
from ..schemas import AgentVoiceError, ErrorCode
from ..services.utterance_service import (
    PlannedSegment,
    UtterancePlan,
    effective_speed,
    plan_utterance,
)
from .routing import ResolvedTarget, resolve_target

logger = logging.getLogger(__name__)

router = APIRouter(tags=["stream"])


class StreamFailure(Exception):
    """A worker died mid-stream, so the client must not be told "done".

    A dead worker closes its PCM queue exactly like a finished one, so the
    consumer has to ask the manager afterwards. Raising keeps that decision here
    instead of duplicating it in the HTTP and WebSocket responses.
    """

    def __init__(self, failure: dict[str, Any]) -> None:
        super().__init__(failure.get("message", "the worker died mid-stream"))
        self.failure = failure


@dataclass
class _OpenedStream:
    """A live stream: the plan for the whole utterance plus its first segment."""

    ctx: Any
    target: ResolvedTarget
    plan: UtterancePlan
    language: str
    speed: float
    first_index: int = 0
    stream_id: Any = None
    queue: asyncio.Queue | None = field(default=None)
    mode: str = ""

    @property
    def engine_id(self) -> str:
        return self.target.engine_id


def _as_bool(value: Any, default: bool) -> bool:
    """Read a JSON boolean from a free-form body, tolerating string spellings.

    The stream bodies are not pydantic models (they are raw dicts, so an OpenAI
    client can post its own shape), which is exactly where a ``"false"`` string
    would otherwise be truthy and silently enable markup.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


async def _open_segment(
    opened: _OpenedStream, segment: PlannedSegment
) -> tuple[Any, asyncio.Queue, str]:
    """Open one worker stream for one speech segment."""
    target = opened.target
    return await opened.ctx.workers.stream(
        engine_id=target.engine_id,
        model_alias=target.model_alias,
        text=segment.text,
        voice_id=target.voice_id,
        ref_text=target.ref_text,
        x_vector_only=target.x_vector_only,
        # Canonical values from the resolved target: the speaker spelling as the
        # manifest declares it, and no instruct when a clone checkpoint speaks.
        mode=target.mode,
        speaker=target.speaker,
        instruct=target.instruct,
        language=opened.language,
        # The base request speed times the markup multiplier, clamped exactly as
        # the file pipeline clamps it, so a stream and a file sound the same.
        speed=effective_speed(opened.speed, segment.speed),
    )


async def _prepare_stream(
    ctx: Any,
    *,
    agent_id: str | None,
    text: str,
    voice: Any,
    engine: Any,
    model: Any,
    quality: Any,
    language: str,
    speed: float,
    speaker: Any = None,
    instruct: str = "",
    pronunciations: dict[str, str] | None = None,
    markup: bool = True,
    spell_identifiers: bool = False,
    paragraph_gap_ms: int = 0,
    project: str | None = None,
) -> _OpenedStream:
    """Admission, markup parse, routing and normalization — then the first stream.

    The first speech segment's worker stream is opened *here*, before the response
    starts, so every routing failure (unknown engine, missing voice, bad speaker)
    is still an ordinary HTTP/WS error instead of a stream that dies after the
    status line has gone out.
    """
    if len(text) > ctx.settings.max_text_chars:
        raise AgentVoiceError(
            ErrorCode.TEXT_TOO_LONG,
            f"text is {len(text)} characters, limit is {ctx.settings.max_text_chars}",
            "Split the input or use speak_script.",
        )
    # A raw body map is validated here: this route never sees a pydantic model.
    check_pronunciation_map(pronunciations)

    script = parse_markup(text, markup=markup, paragraph_gap_ms=paragraph_gap_ms)
    target = resolve_target(
        settings=ctx.settings,
        registry=ctx.registry,
        voices=ctx.voices,
        bindings=ctx.bindings,
        agent_id=agent_id,
        voice=voice,
        speaker=speaker,
        instruct=instruct,
        engine=engine,
        model=model,
        quality=quality,
    )
    plan = plan_utterance(
        ctx,
        script,
        target=target,
        project=project,
        spell_identifiers=spell_identifiers,
        overrides=pronunciations,
    )

    opened = _OpenedStream(
        ctx=ctx,
        target=target,
        plan=plan,
        language=language,
        speed=float(speed),
    )
    for index, segment in enumerate(plan.segments):
        if segment.is_pause:
            continue
        opened.first_index = index
        opened.stream_id, opened.queue, opened.mode = await _open_segment(
            opened, segment
        )
        break
    return opened


async def _iter_pcm(
    opened: _OpenedStream,
    *,
    timeout: float,
    on_stream: Callable[[str, Any], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield 24 kHz mono int16 LE PCM for the whole utterance, pauses included."""
    current: tuple[str, Any] | None = None
    try:
        for index, segment in enumerate(opened.plan.segments):
            if segment.is_pause:
                if segment.silence_ms > 0:
                    # No crossfade exists in a stream, so the requested gap is
                    # emitted at exactly its requested length.
                    yield silence_pcm16(segment.silence_ms / 1000.0)
                continue

            if index == opened.first_index:
                stream_id, queue = opened.stream_id, opened.queue
            else:
                stream_id, queue, _mode = await _open_segment(opened, segment)

            current = (opened.engine_id, stream_id)
            if on_stream is not None:
                on_stream(opened.engine_id, stream_id)

            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    # A stalled consumer must not pin VRAM or memory.
                    await opened.ctx.workers.cancel_stream(opened.engine_id, stream_id)
                    raise AgentVoiceError(
                        ErrorCode.BACKPRESSURE,
                        "client stopped reading the audio stream",
                        "Read the response body promptly or request a shorter text.",
                    ) from None
                if not chunk:
                    break
                yield scale_pcm16(chunk, segment.gain_db) if segment.gain_db else chunk

            failure = opened.ctx.workers.stream_error(opened.engine_id, stream_id)
            current = None
            if failure is not None:
                raise StreamFailure(failure)
    finally:
        if current is not None:
            with contextlib.suppress(Exception):
                await opened.ctx.workers.cancel_stream(*current)


@router.post("/v1/audio/speech/pcm-stream")
async def pcm_stream(request: Request):
    """Stream 24 kHz mono int16 LE PCM as it is generated.

    Body fields mirror ``/v1/agent/speak`` where they can: ``markup`` (default
    true), ``spell_identifiers``, ``paragraph_gap_ms`` and ``project`` all apply.
    """
    ctx = request.app.state.ctx
    body = await request.json()
    text = body.get("input") or body.get("text") or ""
    timeout = ctx.settings.stream_queue_timeout_seconds

    opened = await _prepare_stream(
        ctx,
        agent_id=request.headers.get("x-agent-id"),
        text=text,
        voice=body.get("voice"),
        engine=body.get("engine"),
        model=body.get("model"),
        quality=body.get("quality", "high"),
        language=body.get("language", "Auto"),
        speed=float(body.get("speed", 1.0)),
        speaker=body.get("speaker"),
        instruct=body.get("instruct") or "",
        pronunciations=body.get("pronunciations"),
        markup=_as_bool(body.get("markup"), True),
        spell_identifiers=_as_bool(body.get("spell_identifiers"), False),
        paragraph_gap_ms=body.get("paragraph_gap_ms", 0),
        project=body.get("project"),
    )
    target = opened.target

    async def generate():
        try:
            async for chunk in _iter_pcm(opened, timeout=timeout):
                yield chunk
        except AgentVoiceError:
            # Headers and possibly audio are already on the wire, so the only
            # honest signal left is stopping the body; the log says why.
            logger.warning("PCM stream aborted", exc_info=True)
        except StreamFailure as failure:
            # Same reasoning as v0.3.3's mid-stream check: the status line cannot
            # become an error, but the log records the truncation.
            logger.warning(
                "PCM stream ended early (%s): %s",
                failure.failure.get("error_code", "WORKER_CRASHED"),
                failure.failure.get("message", "the worker died mid-stream"),
            )

    headers = {
        "x-agent-voice-engine": target.engine_id,
        "x-agent-voice-model": target.model_alias,
        "x-agent-voice-streaming-mode": opened.mode,
        "x-agent-voice-sample-rate": "24000",
    }
    if target.speaker:
        # The body is raw PCM, so a preset timbre is only observable here.
        headers["x-agent-voice-speaker"] = target.speaker
    return StreamingResponse(
        generate(), media_type="application/octet-stream", headers=headers
    )


@router.websocket("/v1/audio/ws")
async def audio_ws(websocket: WebSocket):
    """Bidirectional stream: send a JSON request, receive binary PCM frames.

    Sending a new ``{"type": "speak"}`` message while audio is flowing is barge-in:
    the in-flight generation is cancelled before the next one starts.

    A bad *frame* is answered with an error frame, not a closed socket: malformed
    JSON and recoverable request errors (no voice, unknown speaker/engine) leave
    the connection usable so a client can fix the message and carry on. Only a
    disconnect ends the loop.
    """
    await websocket.accept()
    ctx = websocket.app.state.ctx
    current: dict | None = None

    async def pump(payload: dict) -> None:
        nonlocal current
        raw_text = payload.get("text") or payload.get("input") or ""
        opened = await _prepare_stream(
            ctx,
            agent_id=websocket.headers.get("x-agent-id"),
            text=raw_text,
            voice=payload.get("voice"),
            engine=payload.get("engine"),
            model=payload.get("model"),
            quality=payload.get("quality", "high"),
            language=payload.get("language", "Auto"),
            speed=float(payload.get("speed", 1.0)),
            # Whichever timbre the request asked for, by preset name or
            # instruction, travels the same path as on the HTTP stream: routing
            # decides the model.
            speaker=payload.get("speaker"),
            instruct=payload.get("instruct") or "",
            pronunciations=payload.get("pronunciations"),
            markup=_as_bool(payload.get("markup"), True),
            spell_identifiers=_as_bool(payload.get("spell_identifiers"), False),
            paragraph_gap_ms=payload.get("paragraph_gap_ms", 0),
            project=payload.get("project"),
        )
        target = opened.target
        current = {"engine": target.engine_id, "stream_id": opened.stream_id}
        await websocket.send_json(
            {
                "type": "started",
                "stream_id": str(opened.stream_id),
                "engine_id": target.engine_id,
                "model_alias": target.model_alias,
                "streaming_mode": opened.mode,
            }
        )

        def track(engine_id: str, stream_id: Any) -> None:
            # Barge-in cancels "whatever is playing", which after a pause or a
            # segment boundary is a later stream than the one we started with.
            nonlocal current
            current = {"engine": engine_id, "stream_id": stream_id}

        timeout = ctx.settings.stream_queue_timeout_seconds
        try:
            async for chunk in _iter_pcm(opened, timeout=timeout, on_stream=track):
                await websocket.send_bytes(chunk)
        except StreamFailure as failure:
            await websocket.send_json(
                {
                    "type": "error",
                    "error_code": failure.failure.get("error_code", "WORKER_CRASHED"),
                    "message": failure.failure.get(
                        "message", "the worker died mid-stream"
                    ),
                    "suggestion": failure.failure.get("suggestion", ""),
                }
            )
            return

        await websocket.send_json({"type": "done", "stream_id": str(opened.stream_id)})

    try:
        while True:
            try:
                message = await websocket.receive_json()
            except ValueError:
                # Malformed JSON is a bad frame, not a broken socket.
                await websocket.send_json(
                    {
                        "type": "error",
                        "error_code": "BAD_REQUEST",
                        "message": "malformed JSON frame; expected a JSON object",
                        "suggestion": (
                            'Send {"type": "speak", "text": "..."}, '
                            '{"type": "cancel"} or {"type": "ping"}.'
                        ),
                    }
                )
                continue
            if not isinstance(message, dict):
                await websocket.send_json(
                    {
                        "type": "error",
                        "error_code": "BAD_REQUEST",
                        "message": (
                            f"expected a JSON object, got {type(message).__name__}"
                        ),
                        "suggestion": 'Send {"type": "speak", "text": "..."}.',
                    }
                )
                continue

            try:
                kind = message.get("type", "speak")
                if kind == "cancel":
                    if current:
                        await ctx.workers.cancel_stream(
                            current["engine"], current["stream_id"]
                        )
                    await websocket.send_json({"type": "cancelled"})
                    continue
                if kind == "ping":
                    await websocket.send_json({"type": "pong"})
                    continue
                if kind != "speak":
                    await websocket.send_json(
                        {"type": "error", "error_code": "BAD_REQUEST",
                         "message": f"unknown message type: {kind}"}
                    )
                    continue

                if current:
                    # Barge-in: stop whatever is playing before starting the new one.
                    with contextlib.suppress(Exception):
                        await ctx.workers.cancel_stream(
                            current["engine"], current["stream_id"]
                        )
                await pump(message)
            except AgentVoiceError as exc:
                # Inside the loop on purpose: a recoverable request error must
                # tell the caller what to fix without closing the socket.
                with contextlib.suppress(Exception):
                    await websocket.send_json(
                        {"type": "error", **exc.to_response().model_dump(mode="json")}
                    )
    except WebSocketDisconnect:
        pass
    finally:
        if current:
            with contextlib.suppress(Exception):
                await ctx.workers.cancel_stream(current["engine"], current["stream_id"])
