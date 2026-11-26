"""Canonical OpenAI-compatible speech endpoint plus the agent speech surface."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..schemas import AgentVoiceError, ErrorCode, SpeakRequest, SpeechRequest
from .routing import resolve_target

router = APIRouter(tags=["speech"])

OPENAI_BUILTIN_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer"}
"""Voice names hard-coded into real OpenAI SDK / LangChain / Pipecat / LiveKit
clients. None of them can be satisfied by an OpenAI TTS clone, so they are mapped
onto whatever the local install actually has instead of 404-ing the client."""


def _substitute_openai_voice(request: Request, voice: str | None) -> str | None:
    """Map a builtin OpenAI voice name onto a locally resolvable voice.

    Strictly scoped to the builtin set: any other unknown name still fails with
    VOICE_NOT_FOUND, because silently speaking in the wrong voice — or worse,
    papering over a typo — is not a favour to an agent.
    """
    if voice not in OPENAI_BUILTIN_VOICES:
        return voice

    ctx = request.app.state.ctx
    if ctx.voices.resolve(voice) is not None:
        return voice

    configured = ctx.settings.default_voice_id or None
    if configured and ctx.voices.resolve(configured) is not None:
        return configured

    saved = ctx.voices.list()
    if len(saved) == 1:
        return saved[0].voice_id

    return None


def _agent_id(request: Request) -> str | None:
    return request.headers.get("x-agent-id")


def _validate_text(request: Request, text: str) -> None:
    ctx = request.app.state.ctx
    limit = ctx.settings.max_text_chars
    if len(text) > limit:
        raise AgentVoiceError(
            ErrorCode.TEXT_TOO_LONG,
            f"text is {len(text)} characters, limit is {limit}",
            "Split into several calls or use speak_script for long content.",
        )
    if not text.strip():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST, "text is empty", "Provide non-empty input."
        )


async def run_synthesis(
    request: Request,
    *,
    text: str,
    voice: str | None,
    engine: str | None,
    model: str | None,
    quality: str,
    language: str,
    response_format: str,
    seed: int | None,
    speed: float,
    take: int | None = None,
    openai_voice_compat: bool = False,
):
    ctx = request.app.state.ctx
    _validate_text(request, text)

    if openai_voice_compat:
        voice = _substitute_openai_voice(request, voice)

    target = resolve_target(
        settings=ctx.settings,
        registry=ctx.registry,
        voices=ctx.voices,
        bindings=ctx.bindings,
        agent_id=_agent_id(request),
        voice=voice,
        engine=engine,
        model=model,
        quality=quality,  # type: ignore[arg-type]
    )

    return await ctx.workers.synthesize(
        engine_id=target.engine_id,
        model_alias=target.model_alias,
        text=text,
        voice_id=target.voice_id,
        ref_text=target.ref_text,
        x_vector_only=target.x_vector_only,
        language=language,
        seed=seed,
        speed=speed,
        response_format=response_format,
        take=take,
    )


@router.post("/v1/audio/speech")
async def openai_speech(payload: SpeechRequest, request: Request):
    """OpenAI-compatible endpoint.

    Accepts ``tts-1`` / ``tts-1-hd`` or an explicit ``engine:model`` selector.
    """
    result = await run_synthesis(
        request,
        text=payload.input,
        voice=payload.voice,
        engine=None,
        model=payload.model,
        quality="high",
        language="Auto",
        response_format=payload.response_format,
        seed=None,
        speed=payload.speed,
        openai_voice_compat=True,
    )
    media = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
        "pcm": "application/octet-stream",
    }[payload.response_format]
    return FileResponse(
        result.file_path,
        media_type=media,
        filename=f"{result.artifact_id}.{payload.response_format}",
        headers={
            "x-agent-voice-engine": result.engine_id,
            "x-agent-voice-model": result.model_alias,
        },
    )


@router.post("/v1/agent/speak")
async def agent_speak(payload: SpeakRequest, request: Request):
    """Agent-facing synthesis returning compact JSON with a file path."""
    result = await run_synthesis(
        request,
        text=payload.text,
        voice=payload.voice,
        engine=payload.engine,
        model=payload.model,
        quality=payload.quality,
        language=payload.language,
        response_format=payload.format,
        seed=payload.seed,
        speed=payload.speed,
        # A new take is an alternate delivery of the same line: same text, other
        # audio. It feeds the cache key, so take 1 stays cached.
        take=payload.take,
    )
    return result.model_dump(mode="json")


@router.post("/v1/clone")
async def clone_voice_shortcut(payload: dict, request: Request):
    """Alias kept for clients that POST /v1/clone rather than /v1/voices."""
    from .routes_voices import create_voice_from_payload

    return await create_voice_from_payload(payload, request)
