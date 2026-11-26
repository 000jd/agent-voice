"""Shared request resolution for REST and MCP.

Keeping routing here (rather than in each transport) is what stops REST and MCP
from drifting: both call :func:`resolve_target` and get identical precedence.

v0.3 adds two more ways to choose a timbre next to cloning a reference clip:
``speaker`` (a preset timbre on a CustomVoice checkpoint) and ``instruct`` (a
natural-language description, either steering a preset or designing a voice from
nothing). The three modes are exclusive per request, and an explicit model alias
always decides which one runs — silently swapping in a checkpoint that cannot do
what was asked would produce confident, wrong audio.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..bindings import BindingStore
from ..engine_registry import EngineRecord, EngineRegistry, hf_model_downloaded
from ..schemas import AgentVoiceError, ErrorCode, ModelMode, ModelSpec, QualityTier
from ..settings_types import SettingsLike
from ..voice_store import VoiceStore

QUALITY_TO_TIER: dict[QualityTier, str] = {"high": "high", "fast": "fast"}

OPENAI_MODEL_MAP: dict[str, str] = {
    "tts-1": "0.6b-base",
    "tts-1-hd": "1.7b-base",
}
"""Canonical OpenAI model names, mapped to model aliases."""

OPENAI_QUALITY_MAP: dict[str, str] = {
    "tts-1": "fast",
    "tts-1-hd": "high",
}
"""The same names expressed as a quality tier.

OpenAI clients pick a *tier*, not a model. If the resolved engine does not carry
the mapped alias (the mock engine, or a future Kokoro), the tier is what matters,
so we fall back to it rather than erroring on a name the client could not know.
"""


@dataclass(frozen=True)
class ResolvedTarget:
    voice_id: str | None
    engine_id: str
    model_alias: str
    ref_text: str
    x_vector_only: bool
    mode: ModelMode = "clone"
    """Which capability answers this request: clone a reference, speak a preset
    timbre, or design a voice from a description."""
    speaker: str | None = None
    """Canonical manifest spelling of the preset timbre, custom_voice only."""
    instruct: str = ""
    """Natural-language direction. Empty for clone: a Base checkpoint has no
    instruct channel, so carrying one would only widen the cache key."""


def parse_model_selector(model: str | None) -> tuple[str | None, str | None]:
    """Interpret a model selector in three forms.

    * ``tts-1`` / ``tts-1-hd``  -> (None, None); the caller applies the tier
    * ``engine:model``           -> (engine, alias)
    * ``0.6b-base``              -> (None, alias), engine discovered
    """
    if not model:
        return None, None

    if model in OPENAI_MODEL_MAP:
        # Tier, not alias: the alias may not exist on the resolved engine.
        return None, None

    if ":" in model:
        engine_part, _, alias_part = model.partition(":")
        if engine_part and alias_part:
            return engine_part, alias_part
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"malformed model selector: {model}",
            "Use `engine:model`, e.g. `qwen3_tts:1.7b-base`.",
        )

    return None, model


def quality_for_openai_model(model: str | None) -> str | None:
    """Map an OpenAI model name to a quality tier, if it is one."""
    return OPENAI_QUALITY_MAP.get(model or "")


def _auto_voice(
    *,
    record: EngineRecord,
    voices: VoiceStore,
) -> tuple[str | None, str, bool]:
    """Pick a voice when the caller, binding and server default all left it unset.

    Runs inside :func:`resolve_target`, which every caller invokes *before*
    ``ensure_worker``, so a missing voice costs an agent a fast, actionable error
    instead of a GPU wake followed by an engine-level crash.
    """
    available = voices.list()
    if len(available) == 1:
        # One registered voice is unambiguous: use it rather than making the
        # agent discover and pass its id.
        sole = available[0]
        return sole.voice_id, sole.ref_text, sole.x_vector_only_mode

    if not record.manifest.capabilities.voice_clone:
        return None, "", False

    if available:
        raise AgentVoiceError(
            ErrorCode.VOICE_REQUIRED,
            f"no voice selected and engine '{record.engine_id}' requires one; "
            f"{len(available)} voices are registered",
            "Pass voice=<id> (call list_voices() to see them), bind one with "
            "PUT /v1/agents/bindings, or set AGENT_VOICE_DEFAULT_VOICE_ID.",
        )
    raise AgentVoiceError(
        ErrorCode.VOICE_REQUIRED,
        f"no voice selected and engine '{record.engine_id}' can only clone",
        "No voices registered. Call clone_voice(name='narrator', "
        "ref_audio_path='<3-10s wav/mp4>', ref_text='...') first.",
    )


def _instruct_model(record: EngineRecord, mode: ModelMode) -> str | None:
    """An alias that could honour an instruct, for the error message.

    Prefers a model of the same mode (``0.6b-custom`` -> ``1.7b-custom``),
    falling back to any instruct-capable non-clone model on the engine.
    """
    for model in record.manifest.models:
        if model.mode == mode and model.supports_instruct:
            return model.alias
    for model in record.manifest.models:
        if model.mode != "clone" and model.supports_instruct:
            return model.alias
    return None


def _pick_model(
    record: EngineRecord,
    mode: ModelMode,
    quality: QualityTier,
    require_instruct: bool,
) -> str:
    """Choose an alias for ``mode``: mode -> instruct -> downloaded -> tier.

    The mode filter is not optional. With CustomVoice and VoiceDesign sharing a
    quality tier, an unfiltered pick could hand back a checkpoint that cannot do
    what was asked at all.

    Downloaded models win over the requested tier: pulling a 4 GB checkpoint
    because the caller said ``quality="high"`` is a worse answer than speaking a
    tier lower with weights that are already on disk.
    """
    models = record.manifest.models
    candidates = [model for model in models if model.mode == mode]
    if not candidates:
        known = sorted(model.alias for model in models)
        modes = sorted({model.mode for model in models})
        raise AgentVoiceError(
            ErrorCode.MODEL_NOT_FOUND,
            f"engine '{record.engine_id}' declares no {mode} model",
            f"Known aliases: {known} (modes: {modes}). Pass engine=<id> or "
            f"model=<alias> to use an engine that can do {mode}.",
        )

    if require_instruct:
        capable = [model for model in candidates if model.supports_instruct]
        if not capable:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"no {mode} model on engine '{record.engine_id}' supports instruct",
                f"Known aliases: {[model.alias for model in candidates]}. Drop "
                "instruct or use an engine whose models declare supports_instruct.",
            )
        candidates = capable

    installed = [
        model
        for model in candidates
        if model.hf_repo.startswith("local/") or hf_model_downloaded(model.hf_repo)
    ]
    if installed:
        candidates = installed

    for model in candidates:
        if model.quality_tier == quality:
            return model.alias
    for model in candidates:
        if model.default:
            return model.alias
    return candidates[0].alias


def _canonical_speaker(spec: ModelSpec, speaker: str) -> str:
    """Match a preset speaker case-insensitively, returning manifest spelling.

    Canonicalising matters beyond tidiness: the speaker is part of the cache key,
    so ``ryan`` and ``Ryan`` must not produce two entries for the same audio. An
    empty preset list means the engine accepts any timbre name (a custom plugin,
    or the mock engine used in tests).
    """
    requested = speaker.strip()
    if not spec.preset_speakers:
        return requested
    for preset in spec.preset_speakers:
        if preset.lower() == requested.lower():
            return preset
    raise AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"model '{spec.alias}' has no preset speaker '{requested}'",
        f"Known speakers on '{spec.alias}': {spec.preset_speakers}.",
    )


def _check_instruct_supported(
    record: EngineRecord,
    spec: ModelSpec,
) -> None:
    """Reject instruct on a model that cannot honour it.

    Only custom-voice/design models reach here; clone is exempt because the clone
    path deliberately ignores instruct (see :func:`resolve_target`). Dropping an
    instruction silently on a model that advertises instruct control is not an
    option: the caller would get confident audio in the wrong delivery.
    """
    if spec.supports_instruct:
        return
    alternative = _instruct_model(record, spec.mode)
    if alternative is not None:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"model '{spec.alias}' does not support instruct; use '{alternative}' instead",
            f"Pass model='{alternative}' for instruct control, or drop instruct "
            f"to speak with '{spec.alias}'.",
        )
    raise AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"model '{spec.alias}' does not support instruct",
        "Drop instruct, or use a model that declares supports_instruct.",
    )


def resolve_target(
    *,
    settings: SettingsLike,
    registry: EngineRegistry,
    voices: VoiceStore,
    bindings: BindingStore,
    agent_id: str | None = None,
    voice: str | None = None,
    speaker: str | None = None,
    instruct: str = "",
    engine: str | None = None,
    model: str | None = None,
    quality: QualityTier = "high",
    force_mode: ModelMode | None = None,
) -> ResolvedTarget:
    """Apply precedence: explicit arg -> X-Agent-Id binding -> server default.

    ``speaker`` asks for a preset timbre, ``instruct`` for a natural-language
    direction. An explicit (or bound) model alias decides the mode from its own
    manifest entry; without one the intent decides: a preset speaker means
    custom-voice, an instruction with no explicit voice means voice design, and
    everything else keeps the clone behaviour callers already rely on.
    """
    if voice and speaker:
        # Two different timbres in one request: whichever won would be a guess.
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "voice and speaker cannot be combined",
            "Pass voice=<id> to reuse a reference clip, or speaker=<preset> with "
            "a custom-voice model; a designed voice takes instruct only.",
        )

    if force_mode is not None and (voice or speaker):
        # A forced mode owns how the timbre is chosen; an explicit voice/speaker
        # is a contradiction, not a hint to silently ignore.
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"force_mode={force_mode} cannot be combined with voice or speaker",
            "Drop voice/speaker: the forced mode chooses its own timbre source.",
        )

    engine_from_model, alias_from_model = parse_model_selector(model)

    # An OpenAI model name expresses a tier. Apply it, but never let it override
    # an explicit quality argument from an agent-facing caller.
    tier_from_openai = quality_for_openai_model(model)
    if tier_from_openai is not None:
        quality = tier_from_openai  # type: ignore[assignment]

    if force_mode is not None:
        # A forced mode (voice design) ignores bindings: the render *is* its own
        # reference, so inheriting a bound or default voice would defeat the point
        # and make a design request silently depend on someone else's binding.
        voice_id: str | None = None
        engine_id = engine or engine_from_model or settings.default_engine
        alias: str | None = alias_from_model
    else:
        voice_id, engine_id, alias = bindings.resolve(
            agent_id=agent_id,
            explicit_voice=voice,
            explicit_engine=engine or engine_from_model,
            explicit_model=alias_from_model,
            default_voice=settings.default_voice_id or None,
            default_engine=settings.default_engine,
        )

    record = registry.require_installed(engine_id)
    known_aliases = {spec.alias for spec in record.manifest.models}

    if alias is not None and alias not in known_aliases:
        # A mismatched engine/alias pair is a user error, not something to
        # silently substitute with a different voice.
        other_engine = registry.resolve_engine_for_model(alias)
        if other_engine and other_engine != engine_id:
            raise AgentVoiceError(
                ErrorCode.MODEL_NOT_FOUND,
                f"model '{alias}' belongs to engine '{other_engine}', not '{engine_id}'",
                f"Use model='{other_engine}:{alias}' or engine='{other_engine}'.",
            )
        raise AgentVoiceError(
            ErrorCode.MODEL_NOT_FOUND,
            f"engine '{engine_id}' has no model '{alias}'",
            f"Known aliases: {sorted(known_aliases)}",
        )

    if alias is None:
        mode: ModelMode
        if force_mode is not None:
            # A forced mode picks a model that can do exactly that, so the
            # validation below can report a missing instruct instead of tripping
            # over a checkpoint it should never have chosen.
            mode = force_mode
        elif speaker:
            mode = "custom_voice"
        elif instruct and voice is None:
            # An instruction with no reference voice is a request for a voice
            # that does not exist yet: design one.
            mode = "voice_design"
        else:
            mode = "clone"
        alias = _pick_model(
            record,
            mode,
            quality,
            # Design always needs instruct; a preset only needs it when the caller
            # actually asked for one. Clone never does.
            require_instruct=mode == "voice_design" or (mode == "custom_voice" and bool(instruct)),
        )

    spec = registry.model_spec(engine_id, alias)

    if force_mode is not None and spec.mode != force_mode:
        forced_aliases = sorted(
            model.alias for model in record.manifest.models if model.mode == force_mode
        )
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"model '{alias}' is a {spec.mode} model and cannot be used in {force_mode} mode",
            f"Use {forced_aliases or '(no model declared)'} on engine "
            f"'{engine_id}', or omit model to have one picked.",
        )

    mode = spec.mode

    if mode == "clone" and speaker:
        # An explicit (or bound) Base checkpoint decides the mode, so a preset
        # speaker would otherwise be dropped and the caller would hear a clone.
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"model '{alias}' is a clone model and cannot speak preset speaker '{speaker}'",
            "Use a custom_voice model (for example 1.7b-custom) or drop speaker.",
        )
    if mode != "clone" and voice:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"model '{alias}' is a {mode} model and cannot reuse a cloned voice",
            "Drop voice, or use a clone model (1.7b-base/0.6b-base) to speak it.",
        )

    resolved_speaker: str | None = None
    resolved_instruct = ""

    if mode == "clone":
        # Clone ignores instruct: a Base checkpoint has no instruct channel, and
        # forcing a caller that passes it blindly to fail would break the legacy
        # path for a parameter the engine cannot use anyway.
        resolved_instruct = ""
    elif mode == "custom_voice":
        if not speaker or not speaker.strip():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"model '{alias}' speaks preset timbres and needs a speaker",
                f"Pass speaker=<one of {spec.preset_speakers}> with "
                f"engine='{engine_id}', model='{alias}'.",
            )
        resolved_speaker = _canonical_speaker(spec, speaker)
        if instruct:
            _check_instruct_supported(record, spec)
            resolved_instruct = instruct
    else:  # voice_design
        if not instruct or not instruct.strip():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"model '{alias}' designs a voice from a description and needs instruct",
                "Describe the voice you want, e.g. instruct='a warm, low British "
                "narrator in his forties, unhurried'.",
            )
        resolved_instruct = instruct

    resolved_voice_id: str | None = None
    ref_text = ""
    x_vector_only = False
    if mode == "clone":
        if voice_id:
            metadata = voices.resolve(voice_id)
            if metadata is not None:
                resolved_voice_id = metadata.voice_id
                ref_text = metadata.ref_text
                x_vector_only = metadata.x_vector_only_mode
            else:
                # A voice name that does not exist is a hard error: synthesising in
                # the wrong voice silently is worse than failing.
                raise AgentVoiceError(
                    ErrorCode.VOICE_NOT_FOUND,
                    f"no such voice: {voice_id}",
                    "Call list_voices() to see available voices, or clone_voice() first.",
                )

        if resolved_voice_id is None:
            resolved_voice_id, ref_text, x_vector_only = _auto_voice(record=record, voices=voices)

    return ResolvedTarget(
        voice_id=resolved_voice_id,
        engine_id=engine_id,
        model_alias=alias,
        ref_text=ref_text,
        x_vector_only=x_vector_only,
        mode=mode,
        speaker=resolved_speaker,
        instruct=resolved_instruct,
    )


def describe_error(exc: AgentVoiceError) -> dict[str, Any]:
    return exc.to_response().model_dump(mode="json")
