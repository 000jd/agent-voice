"""Canonical OpenAI-compatible speech endpoint plus the agent speech surface."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ..pronunciations import check_pronunciation_map
from ..schemas import AgentVoiceError, ErrorCode, SpeakRequest, SpeechRequest, SynthesisResult
from ..services.utterance_service import speak_utterance
from .routing import ResolvedTarget, parse_model_selector, resolve_target

router = APIRouter(tags=["speech"])

OPENAI_SPEAKER_MAP: dict[str, str] = {
    "alloy": "Ryan",
    "echo": "Aiden",
    "fable": "Dylan",
    "onyx": "Uncle_Fu",
    "nova": "Serena",
    "shimmer": "Vivian",
}
"""Builtin OpenAI name -> preset speaker on a CustomVoice checkpoint.

Used only as the last resort, when the caller has no voice of its own: a preset
timbre needs no reference clip, so an OpenAI-shaped client still hears speech on
a fresh install that has nothing cloned yet."""

OPENAI_BUILTIN_VOICES = set(OPENAI_SPEAKER_MAP)
"""Voice names hard-coded into real OpenAI SDK / LangChain / Pipecat / LiveKit
clients. None of them can be satisfied by an OpenAI TTS clone, so they are mapped
onto whatever the local install actually has instead of 404-ing the client.

Derived from the map above so the two can never drift apart. Every engine's
builtin map uses the same six names, so this set stays correct for all of them."""

KOKORO_ENGINE_ID = "kokoro"
"""The one engine whose preset timbres are not the Qwen CustomVoice speaker names."""


def _openai_speaker_map(engine_id: str) -> dict[str, str]:
    """The builtin name -> preset speaker map for the engine that will speak.

    Kokoro's preset voices (``af_heart``, ``am_adam``...) are not the Qwen
    CustomVoice speaker names, so mapping ``alloy`` to ``Ryan`` on a Kokoro-only
    install would ask for a timbre that does not exist. The map is imported
    lazily so the daemon still starts on an install without Kokoro's extra
    module, and every other engine keeps the Qwen map it has always used.
    """
    if engine_id == KOKORO_ENGINE_ID:
        try:
            from ..kokoro_support import KOKORO_OPENAI_VOICES
        except ImportError:  # pragma: no cover - kokoro support is optional
            return OPENAI_SPEAKER_MAP
        return KOKORO_OPENAI_VOICES
    return OPENAI_SPEAKER_MAP


def _predicted_engine(request: Request, engine: str | None, model: str | None) -> str:
    """The engine a request will resolve to, before ``resolve_target`` runs.

    The builtin-voice substitution happens first (it decides *what* to ask for),
    so it has to guess the engine the same way routing will: explicit argument,
    then an ``engine:model`` selector, then the server default.
    """
    engine_from_model, _alias = parse_model_selector(model)
    return engine or engine_from_model or request.app.state.ctx.settings.default_engine


def _preset_speaker_for(
    request: Request, speaker: str, engine_id: str
) -> str | None:
    """The mapped speaker if the engine can voice it without a clone.

    A CustomVoice checkpoint speaks fixed timbres, so this is the one path to
    audio that needs neither a registered voice nor a download of anything but
    the checkpoint itself. ``local/`` repos count as present too: they are models
    that ship with the install rather than being fetched from HuggingFace.
    """
    ctx = request.app.state.ctx
    for model in ctx.registry.list_models(engine_id):
        if model.mode != "custom_voice" or speaker not in model.preset_speakers:
            continue
        if model.downloaded_locally or model.hf_repo.startswith("local/"):
            return speaker
    return None


def _substitute_openai_voice(
    request: Request, voice: str | None, *, engine: str | None, model: str | None
) -> tuple[str | None, str | None]:
    """Map a builtin OpenAI voice name onto a locally resolvable voice or speaker.

    Returns ``(voice_id, speaker)``: at most one of the two is set, and both are
    None when nothing local can satisfy the name.

    Strictly scoped to the builtin set: any other unknown name still fails with
    VOICE_NOT_FOUND, because silently speaking in the wrong voice — or worse,
    papering over a typo — is not a favour to an agent.
    """
    if voice not in OPENAI_BUILTIN_VOICES:
        return voice, None

    ctx = request.app.state.ctx
    if ctx.voices.resolve(voice) is not None:
        return voice, None

    configured = ctx.settings.default_voice_id or None
    if configured and ctx.voices.resolve(configured) is not None:
        return configured, None

    saved = ctx.voices.list()
    if len(saved) == 1:
        return saved[0].voice_id, None

    # No local voice to clone with, so try the preset timbre the name maps to.
    engine_id = _predicted_engine(request, engine, model)
    mapped = _openai_speaker_map(engine_id).get(voice or "")
    if mapped is None:
        return None, None
    return None, _preset_speaker_for(request, mapped, engine_id)


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
    speaker: str | None = None,
    instruct: str = "",
    openai_voice_compat: bool = False,
    pronunciations: dict[str, str] | None = None,
    markup: bool = True,
    spell_identifiers: bool = False,
    paragraph_gap_ms: int = 0,
    project: str | None = None,
) -> SynthesisResult:
    """Synthesise one utterance through the shared pipeline.

    The admission caps come first (``_validate_text`` reads the raw characters;
    ``check_pronunciation_map`` bounds the lexicon), then :func:`speak_utterance`
    parses markup on the raw text, resolves the target, and applies the scoped
    lexicon and code normalization in that order.
    """
    ctx = request.app.state.ctx
    _validate_text(request, text)

    # A raw map is checked here as well as in the pydantic model: this function is
    # also the entry point for OpenAI-shaped bodies and future callers, and an
    # over-cap map must be a BAD_REQUEST before it reaches the regex cache.
    check_pronunciation_map(pronunciations)

    if openai_voice_compat:
        # Either a local voice or a preset speaker comes back; routing turns that
        # pair into one concrete model, then the worker is handed the resolution.
        voice, speaker = _substitute_openai_voice(
            request, voice, engine=engine, model=model
        )

    def resolve() -> ResolvedTarget:
        return resolve_target(
            settings=ctx.settings,
            registry=ctx.registry,
            voices=ctx.voices,
            bindings=ctx.bindings,
            agent_id=_agent_id(request),
            voice=voice,
            speaker=speaker,
            instruct=instruct,
            engine=engine,
            model=model,
            quality=quality,  # type: ignore[arg-type]
        )

    outcome = await speak_utterance(
        ctx,
        text=text,
        resolve=resolve,
        markup=markup,
        spell_identifiers=spell_identifiers,
        paragraph_gap_ms=paragraph_gap_ms,
        project=project,
        overrides=pronunciations,
        language=language,
        seed=seed,
        speed=speed,
        response_format=response_format,
        take=take,
    )
    return outcome.result


@router.post("/v1/audio/speech")
async def openai_speech(payload: SpeechRequest, request: Request):
    """OpenAI-compatible endpoint.

    Accepts ``tts-1`` / ``tts-1-hd`` or an explicit ``engine:model`` selector.

    Deliberately strict: an OpenAI client cannot opt into v0.4 markup or a scoped
    lexicon project, so brackets are spoken as written and only the global plus
    resolved-voice lexicon layers apply. Anything else would change what an
    existing client hears without it asking.
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
        markup=False,
        project=None,
    )
    media = {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "flac": "audio/flac",
        "pcm": "application/octet-stream",
    }[payload.response_format]
    headers = {
        "x-agent-voice-engine": result.engine_id,
        "x-agent-voice-model": result.model_alias,
    }
    if result.speaker:
        # The body is audio, so a preset-timbre fallback is only observable here.
        headers["x-agent-voice-speaker"] = result.speaker
    return FileResponse(
        result.file_path,
        media_type=media,
        filename=f"{result.artifact_id}.{payload.response_format}",
        headers=headers,
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
        speaker=payload.speaker,
        instruct=payload.instruct,
        # Per-call overrides; the persistent lexicon applies underneath them.
        pronunciations=payload.pronunciations,
        # v0.4 selectors: markup is on by default for the agent surface, the
        # scoped project participates in the lexicon, and code normalization is
        # opt-in because it changes how identifiers are pronounced.
        markup=payload.markup,
        spell_identifiers=payload.spell_identifiers,
        paragraph_gap_ms=payload.paragraph_gap_ms,
        project=payload.project,
    )
    return result.model_dump(mode="json")


@router.post("/v1/clone")
async def clone_voice_shortcut(payload: dict, request: Request):
    """Alias kept for clients that POST /v1/clone rather than /v1/voices."""
    from .routes_voices import create_voice_from_payload

    return await create_voice_from_payload(payload, request)
