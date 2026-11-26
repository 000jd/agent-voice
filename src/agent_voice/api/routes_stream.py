"""PCM streaming over HTTP chunked transfer, plus a WebSocket with barge-in.

Backpressure is enforced here, not in the worker: the worker's PCM queue is
bounded, and if the client stops reading we cancel the worker rather than buffer
audio without limit.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from ..pronunciations import prepare_text
from ..schemas import AgentVoiceError, ErrorCode
from .routing import resolve_target

router = APIRouter(tags=["stream"])


async def _open_stream(
    request: Request,
    *,
    text: str,
    voice,
    engine,
    model,
    quality,
    language,
    speed,
    speaker=None,
    instruct: str = "",
    pronunciations: dict[str, str] | None = None,
):
    ctx = request.app.state.ctx
    if len(text) > ctx.settings.max_text_chars:
        raise AgentVoiceError(
            ErrorCode.TEXT_TOO_LONG,
            f"text is {len(text)} characters, limit is {ctx.settings.max_text_chars}",
            "Split the input or use speak_script.",
        )
    # Rewritten after the length check, so the limit applies to what the caller
    # sent. The spoken form itself is the cache discriminator.
    text = prepare_text(ctx.pronunciations, text, pronunciations)
    target = resolve_target(
        settings=ctx.settings,
        registry=ctx.registry,
        voices=ctx.voices,
        bindings=ctx.bindings,
        agent_id=request.headers.get("x-agent-id"),
        voice=voice,
        speaker=speaker,
        instruct=instruct,
        engine=engine,
        model=model,
        quality=quality,
    )
    stream_id, queue, mode = await ctx.workers.stream(
        engine_id=target.engine_id,
        model_alias=target.model_alias,
        text=text,
        voice_id=target.voice_id,
        ref_text=target.ref_text,
        x_vector_only=target.x_vector_only,
        # Canonical values from the resolved target: the speaker spelling as the
        # manifest declares it, and no instruct when a clone checkpoint speaks.
        mode=target.mode,
        speaker=target.speaker,
        instruct=target.instruct,
        language=language,
        speed=speed,
    )
    return ctx, target, stream_id, queue, mode


@router.post("/v1/audio/speech/pcm-stream")
async def pcm_stream(request: Request):
    """Stream 24 kHz mono int16 LE PCM as it is generated."""
    ctx = request.app.state.ctx
    body = await request.json()
    text = body.get("input") or body.get("text") or ""
    timeout = ctx.settings.stream_queue_timeout_seconds

    ctx_, target, stream_id, queue, mode = await _open_stream(
        request,
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
    )

    async def generate():
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    # A stalled consumer must not pin VRAM or memory.
                    await ctx_.workers.cancel_stream(target.engine_id, stream_id)
                    raise AgentVoiceError(
                        ErrorCode.BACKPRESSURE,
                        "client stopped reading the audio stream",
                        "Read the response body promptly or request a shorter text.",
                    ) from None
                if not chunk:
                    break
                yield chunk
        finally:
            await ctx_.workers.cancel_stream(target.engine_id, stream_id)

    headers = {
        "x-agent-voice-engine": target.engine_id,
        "x-agent-voice-model": target.model_alias,
        "x-agent-voice-streaming-mode": mode,
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
    """
    await websocket.accept()
    ctx = websocket.app.state.ctx
    current: dict | None = None

    async def pump(payload: dict) -> None:
        nonlocal current
        raw_text = payload.get("text") or payload.get("input") or ""
        if len(raw_text) > ctx.settings.max_text_chars:
            # Same admission rule as the chunked HTTP route: the caller is told
            # off for what it sent, before any rewrite or worker exists.
            await websocket.send_json(
                {
                    "type": "error",
                    "error_code": "TEXT_TOO_LONG",
                    "message": (
                        f"text is {len(raw_text)} characters, limit is "
                        f"{ctx.settings.max_text_chars}"
                    ),
                    "suggestion": "Split the input or use speak_script.",
                }
            )
            return
        # Whichever timbre the request asked for, by preset name or instruction,
        # travels the same path as on the HTTP stream: routing decides the model.
        speaker = payload.get("speaker")
        instruct = payload.get("instruct") or ""
        # Rewritten exactly the way the chunked route does it, so a socket client
        # and an HTTP client hear the same spoken form for the same line.
        text = prepare_text(
            ctx.pronunciations,
            raw_text,
            payload.get("pronunciations"),
        )
        target = resolve_target(
            settings=ctx.settings,
            registry=ctx.registry,
            voices=ctx.voices,
            bindings=ctx.bindings,
            agent_id=websocket.headers.get("x-agent-id"),
            voice=payload.get("voice"),
            speaker=speaker,
            instruct=instruct,
            engine=payload.get("engine"),
            model=payload.get("model"),
            quality=payload.get("quality", "high"),
        )
        stream_id, queue, mode = await ctx.workers.stream(
            engine_id=target.engine_id,
            model_alias=target.model_alias,
            text=text,
            voice_id=target.voice_id,
            ref_text=target.ref_text,
            x_vector_only=target.x_vector_only,
            mode=target.mode,
            speaker=target.speaker,
            instruct=target.instruct,
            language=payload.get("language", "Auto"),
            speed=float(payload.get("speed", 1.0)),
        )
        current = {"engine": target.engine_id, "stream_id": stream_id}
        await websocket.send_json(
            {
                "type": "started",
                "stream_id": str(stream_id),
                "engine_id": target.engine_id,
                "model_alias": target.model_alias,
                "streaming_mode": mode,
            }
        )
        timeout = ctx.settings.stream_queue_timeout_seconds
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                await ctx.workers.cancel_stream(target.engine_id, stream_id)
                await websocket.send_json(
                    {"type": "error", "error_code": "BACKPRESSURE",
                     "message": "client stopped reading"}
                )
                return
            if not chunk:
                break
            await websocket.send_bytes(chunk)
        await websocket.send_json({"type": "done", "stream_id": str(stream_id)})

    try:
        while True:
            message = await websocket.receive_json()
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
                    await ctx.workers.cancel_stream(current["engine"], current["stream_id"])
            await pump(message)
    except WebSocketDisconnect:
        pass
    except AgentVoiceError as exc:
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "error", **exc.to_response().model_dump(mode="json")}
            )
    finally:
        if current:
            with contextlib.suppress(Exception):
                await ctx.workers.cancel_stream(current["engine"], current["stream_id"])
