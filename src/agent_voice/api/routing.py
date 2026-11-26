"""Shared request resolution for REST and MCP.

Keeping routing here (rather than in each transport) is what stops REST and MCP
from drifting: both call :func:`resolve_target` and get identical precedence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..bindings import BindingStore
from ..engine_registry import EngineRecord, EngineRegistry
from ..schemas import AgentVoiceError, ErrorCode, QualityTier
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


def resolve_target(
    *,
    settings: SettingsLike,
    registry: EngineRegistry,
    voices: VoiceStore,
    bindings: BindingStore,
    agent_id: str | None = None,
    voice: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    quality: QualityTier = "high",
) -> ResolvedTarget:
    """Apply precedence: explicit arg -> X-Agent-Id binding -> server default."""
    engine_from_model, alias_from_model = parse_model_selector(model)

    # An OpenAI model name expresses a tier. Apply it, but never let it override
    # an explicit quality argument from an agent-facing caller.
    tier_from_openai = quality_for_openai_model(model)
    if tier_from_openai is not None:
        quality = tier_from_openai  # type: ignore[assignment]

    voice_id, engine_id, bound_alias = bindings.resolve(
        agent_id=agent_id,
        explicit_voice=voice,
        explicit_engine=engine or engine_from_model,
        explicit_model=alias_from_model,
        default_voice=settings.default_voice_id or None,
        default_engine=settings.default_engine,
    )

    record = registry.require_installed(engine_id)
    known_aliases = {spec.alias for spec in record.manifest.models}

    alias = bound_alias
    if alias is None:
        alias = registry.default_model_alias(engine_id, quality)
    elif alias not in known_aliases:
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

    resolved_voice_id: str | None = None
    ref_text = ""
    x_vector_only = False
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
        resolved_voice_id, ref_text, x_vector_only = _auto_voice(
            record=record, voices=voices
        )

    return ResolvedTarget(
        voice_id=resolved_voice_id,
        engine_id=engine_id,
        model_alias=alias,
        ref_text=ref_text,
        x_vector_only=x_vector_only,
    )


def describe_error(exc: AgentVoiceError) -> dict[str, Any]:
    return exc.to_response().model_dump(mode="json")
