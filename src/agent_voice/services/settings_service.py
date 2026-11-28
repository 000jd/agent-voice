"""Whitelisted, redacted settings read/write over REST."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import envfile
from ..config import Settings
from ..schemas import (
    AgentVoiceError,
    ErrorCode,
    SettingEntry,
    SettingsResponse,
)


@dataclass(frozen=True)
class Spec:
    field: str
    env: str
    label: str
    group: str
    kind: str = "str"
    editable: bool = True
    description: str = ""
    choices: tuple[str, ...] = ()


SPECS: tuple[Spec, ...] = (
    Spec("ui_enabled", "AGENT_VOICE_UI", "Studio UI", "Studio", "bool", True, "Serve /ui."),
    Spec(
        "default_engine",
        "AGENT_VOICE_DEFAULT_ENGINE",
        "Default TTS engine",
        "Defaults",
        "str",
        True,
    ),
    Spec(
        "default_asr_engine",
        "AGENT_VOICE_DEFAULT_ASR_ENGINE",
        "Default ASR engine",
        "Defaults",
        "str",
        True,
    ),
    Spec(
        "default_voice_id", "AGENT_VOICE_DEFAULT_VOICE_ID", "Default voice", "Defaults", "str", True
    ),
    Spec(
        "device",
        "AGENT_VOICE_DEVICE",
        "Global device",
        "Hardware",
        "select",
        True,
        choices=("auto", "cpu", "cuda", "mps", "rocm"),
    ),
    Spec("devices", "AGENT_VOICE_DEVICES", "Per-engine devices", "Hardware", "str", True),
    Spec("max_text_chars", "MAX_TEXT_CHARS", "Max text characters", "Limits", "int", True),
    Spec("max_script_beats", "MAX_SCRIPT_BEATS", "Max script beats", "Limits", "int", True),
    Spec(
        "max_instruct_chars",
        "MAX_INSTRUCT_CHARS",
        "Max instruction characters",
        "Limits",
        "int",
        True,
    ),
    Spec(
        "max_design_sample_chars",
        "MAX_DESIGN_SAMPLE_CHARS",
        "Max design sample characters",
        "Limits",
        "int",
        True,
    ),
    Spec(
        "max_ref_text_chars",
        "MAX_REF_TEXT_CHARS",
        "Max reference transcript characters",
        "Limits",
        "int",
        True,
    ),
    Spec(
        "max_transcribe_seconds",
        "MAX_TRANSCRIBE_SECONDS",
        "Max transcription seconds",
        "Limits",
        "int",
        True,
    ),
    Spec("max_dub_segments", "MAX_DUB_SEGMENTS", "Max dub segments", "Limits", "int", True),
    Spec("max_cache_gb", "MAX_CACHE_GB", "Synthesis cache GB", "Storage", "float", True),
    Spec(
        "model_idle_ttl_seconds",
        "MODEL_IDLE_TTL_SECONDS",
        "Model idle TTL seconds",
        "GPU",
        "float",
        True,
    ),
    Spec("max_queue_depth", "MAX_QUEUE_DEPTH", "Queue depth", "GPU", "int", True),
    Spec(
        "voice_draft_max_count", "VOICE_DRAFT_MAX_COUNT", "Max voice drafts", "Drafts", "int", True
    ),
    Spec(
        "voice_draft_ttl_seconds",
        "VOICE_DRAFT_TTL_SECONDS",
        "Draft TTL seconds",
        "Drafts",
        "float",
        True,
    ),
    Spec("history_max_items", "HISTORY_MAX_ITEMS", "History retention", "Storage", "int", True),
    Spec("capture_max_items", "CAPTURE_MAX_ITEMS", "Capture retention", "Storage", "int", True),
)

READ_ONLY: tuple[Spec, ...] = (
    Spec("api_key_configured", "AGENT_VOICE_API_KEY", "API key", "Security", "readonly", False),
    Spec("host", "AGENT_VOICE_HOST", "Bind host", "Security", "readonly", False),
    Spec("port", "AGENT_VOICE_PORT", "Bind port", "Security", "readonly", False),
    Spec(
        "allowed_hosts", "AGENT_VOICE_ALLOWED_HOSTS", "Allowed hosts", "Security", "readonly", False
    ),
    Spec(
        "allow_any_path",
        "AGENT_VOICE_ALLOW_ANY_PATH",
        "Allow arbitrary paths",
        "Security",
        "readonly",
        False,
    ),
    Spec(
        "mcp_base_path", "AGENT_VOICE_MCP_BASE_PATH", "MCP base path", "Security", "readonly", False
    ),
    Spec(
        "allow_agent_engine_mutation",
        "ALLOW_AGENT_ENGINE_MUTATION",
        "Agent engine mutation",
        "Security",
        "readonly",
        False,
    ),
    Spec(
        "require_engine_approval",
        "REQUIRE_ENGINE_APPROVAL",
        "Require engine approval",
        "Security",
        "readonly",
        False,
    ),
    Spec("toolset", "AGENT_VOICE_TOOLSET", "MCP toolset", "Security", "readonly", False),
    Spec("home", "AGENT_VOICE_HOME", "Home", "Paths", "readonly", False),
    Spec("data_dir", "DATA_DIR", "Data directory", "Paths", "readonly", False),
    Spec("engines_dir", "ENGINES_DIR", "Engines directory", "Paths", "readonly", False),
)

SPEC_BY_KEY = {spec.env: spec for spec in SPECS}
READ_ONLY_BY_KEY = {spec.env: spec for spec in READ_ONLY}


def _display_value(spec: Spec, ctx: Any) -> Any:
    if spec.field == "api_key_configured":
        return bool(ctx.settings.api_key)
    if spec.field == "allowed_hosts":
        return list(ctx.settings.allowed_hosts)
    if spec.field == "devices":
        value = ctx.settings.devices
        return ",".join(f"{k}={v}" for k, v in value.items())
    value = getattr(ctx.settings, spec.field, None)
    if isinstance(value, Path):
        return str(value)
    return value


def _source(ctx: Any, spec: Spec) -> tuple[str, bool]:
    if spec.env in os.environ:
        return "env", True
    try:
        if spec.env in envfile.read_env(ctx.settings.config_path):
            return "file", False
    except OSError:
        pass
    return "default", False


def response(ctx: Any, *, restart_required: bool = False) -> SettingsResponse:
    entries: list[SettingEntry] = []
    values: dict[str, Any] = {}
    for spec in SPECS:
        value = _display_value(spec, ctx)
        source, shadowed = _source(ctx, spec)
        values[spec.env] = value
        entries.append(
            SettingEntry(
                key=spec.env,
                value=value,
                kind=spec.kind,  # type: ignore[arg-type]
                group=spec.group,
                label=spec.label,
                description=spec.description,
                editable=spec.editable,
                restart_required=True,
                choices=list(spec.choices),
                source=source,  # type: ignore[arg-type]
                shadowed_by_env=shadowed,
            )
        )
    for spec in READ_ONLY:
        value = _display_value(spec, ctx)
        values[spec.env] = value
        entries.append(
            SettingEntry(
                key=spec.env,
                value=value,
                kind="readonly",
                group=spec.group,
                label=spec.label,
                editable=False,
                restart_required=False,
                source="readonly",
            )
        )
    return SettingsResponse(
        settings=entries,
        values=values,
        config_path=str(ctx.settings.config_path),
        restart_required=restart_required,
    )


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"on", "true", "1", "yes"}:
        return True
    if text in {"off", "false", "0", "no"}:
        return False
    raise ValueError("expected on/off or true/false")


def _coerce(spec: Spec, value: Any) -> Any:
    if spec.kind == "bool":
        return _parse_bool(value)
    if spec.kind == "int":
        if isinstance(value, bool):
            raise ValueError("expected an integer")
        number = int(value)
        return number
    if spec.kind == "float":
        if isinstance(value, bool):
            raise ValueError("expected a number")
        return float(value)
    if spec.field == "devices":
        if isinstance(value, dict):
            return ",".join(f"{key}={item}" for key, item in value.items())
        return str(value)
    if spec.kind == "select":
        text = str(value).strip()
        if spec.choices and text not in spec.choices:
            raise ValueError(f"expected one of {', '.join(spec.choices)}")
        return text
    return str(value)


def patch(ctx: Any, values: dict[str, Any]) -> SettingsResponse:
    if not values:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "no settings supplied",
            "Send a non-empty values object.",
        )
    updates: dict[str, str] = {}
    candidate_values = ctx.settings.model_dump()
    for key, raw in values.items():
        if key in READ_ONLY_BY_KEY:
            raise AgentVoiceError(
                ErrorCode.FORBIDDEN_SETTING,
                f"{key} is a security setting and is read-only over REST",
                "Change it in <home>/.env or with `agent-voice setup`, then restart.",
            )
        spec = SPEC_BY_KEY.get(key)
        if spec is None:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"unknown or read-only setting: {key}",
                "Use GET /v1/settings to see the editable keys.",
            )
        try:
            coerced = _coerce(spec, raw)
        except (TypeError, ValueError) as exc:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid value for {key}: {exc}",
                "Correct the value and retry.",
            ) from exc
        candidate_values[spec.field] = coerced
        updates[key] = "true" if coerced is True else "false" if coerced is False else str(coerced)

    try:
        candidate = Settings(**candidate_values)
    except Exception as exc:  # noqa: BLE001 - pydantic validation message is the fix
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"settings are not valid: {exc}",
            "Correct the value and retry.",
        ) from exc
    if candidate.default_engine not in ctx.registry.ids():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown default engine: {candidate.default_engine}",
            "Use an engine id from GET /v1/engines.",
        )
    try:
        asr = ctx.registry.get(candidate.default_asr_engine)
        if asr.manifest.kind != "asr":
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"{candidate.default_asr_engine} is not an ASR engine",
                "Pick a kind:asr engine.",
            )
    except AgentVoiceError:
        raise
    except Exception as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown default ASR engine: {candidate.default_asr_engine}",
            "Install or choose a kind:asr engine.",
        ) from exc
    if candidate.default_voice_id:
        try:
            ctx.voices.get(candidate.default_voice_id)
        except AgentVoiceError as exc:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"unknown default voice: {candidate.default_voice_id}",
                "Use an id from GET /v1/voices.",
            ) from exc
    try:
        from ..hardware import validate_device_preferences

        validate_device_preferences(candidate, ctx.registry)
    except AgentVoiceError:
        raise
    except Exception as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"invalid device preference: {exc}",
            "Use a device the engine declares and this host supports.",
        ) from exc

    try:
        envfile.write_env(ctx.settings.config_path, updates)
    except OSError as exc:
        import logging

        logging.getLogger(__name__).warning(
            "could not write settings file %s: %s", ctx.settings.config_path, exc
        )
        raise AgentVoiceError(
            ErrorCode.INTERNAL_ERROR,
            "could not write the settings file",
            "Check the home directory permissions and retry.",
        ) from exc
    return response(ctx, restart_required=True)
