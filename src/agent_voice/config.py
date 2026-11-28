"""Configuration for the agent-voice core daemon.

The core daemon must never import torch. Engine-specific dependencies live in
isolated per-engine virtualenvs under ``engines/<engine_id>/.venv``.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .ids import validate_resource_id
from .pronunciations import (
    MAX_LEXICON_ENTRIES,
    MAX_MERGED_LEXICON_ENTRIES,
    PROJECT_LEXICON_DIR,
    PRONUNCIATION_FILE,
)
from .schemas import MAX_HOTWORDS, AgentVoiceError, ComputeDevice, DevicePreference


def default_home() -> Path:
    """The one resolved install home.

    ``AGENT_VOICE_HOME`` wins. Otherwise the checkout root is the first parent
    of this module that holds both ``pyproject.toml`` and ``engines/``. That is
    what makes the CLI work from any directory without creating ``data/`` or
    ``engines/`` in the caller's cwd. A non-editable install with no checkout
    falls back to ``~/.agent-voice``; the doctor row says so.
    """
    override = os.environ.get("AGENT_VOICE_HOME", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "engines").is_dir():
            return parent
    return (Path.home() / ".agent-voice").resolve()


def settings_env_file(home: Path | None = None) -> Path:
    """The one ``.env`` the daemon reads, anchored at the home."""
    return (home or default_home()) / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The file is supplied by get_settings() from default_home(); a bare
        # Settings(...) in a test must not read the developer's checkout .env.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    def __init__(self, **values: object) -> None:
        """Accept constructor field names, without letting env field names in.

        ``populate_by_name=True`` would also make the standard ``HOME``,
        ``PORT``, ``HOST`` and ``DEVICE`` environment variables match fields,
        silently re-homing the install or changing its bind address. Map only
        *explicit constructor kwargs* from the field name to its alias; the
        environment source still sees aliases alone.
        """
        mapped = dict(values)
        for name, field in type(self).model_fields.items():
            if name not in mapped:
                continue
            alias = field.alias
            if alias is None:
                choices = getattr(field.validation_alias, "choices", None)
                if choices:
                    alias = next((choice for choice in choices if isinstance(choice, str)), None)
            if not alias:
                continue
            # An explicit alias always wins over the field-name spelling.
            if alias in mapped:
                mapped.pop(name)
            else:
                mapped[alias] = mapped.pop(name)
        super().__init__(**mapped)

    # --- Server ---------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="AGENT_VOICE_HOST")
    port: int = Field(default=8000, alias="AGENT_VOICE_PORT")
    api_key: str = Field(default="", alias="AGENT_VOICE_API_KEY")
    ui_enabled: bool = Field(default=True, alias="AGENT_VOICE_UI")
    """Serve the built Studio SPA at ``/ui``. ``serve --no-ui`` overrides this."""

    # --- Defaults -------------------------------------------------------
    default_engine: str = Field(default="mock", alias="AGENT_VOICE_DEFAULT_ENGINE")
    default_asr_engine: str = Field(default="phonon", alias="AGENT_VOICE_DEFAULT_ASR_ENGINE")
    """Which ``kind: asr`` engine a transcription uses when none is named.

    Deliberately independent of ``default_engine``: speech in and speech out are
    different plugin families, and a CPU listener next to a GPU speaker is the
    normal deployment.
    """
    default_voice_id: str = Field(default="", alias="AGENT_VOICE_DEFAULT_VOICE_ID")

    # --- Hardware -------------------------------------------------------
    device: DevicePreference = Field(default="auto", alias="AGENT_VOICE_DEVICE")
    """Global device preference. ``auto`` resolves per engine from its manifest
    and the host; an explicit value applies to every engine that supports it."""
    devices: Annotated[dict[str, ComputeDevice], NoDecode] = Field(
        default_factory=dict, alias="AGENT_VOICE_DEVICES"
    )
    """Per-engine overrides, the one env spelling ``kokoro=cpu,qwen3_tts=cuda``.

    NoDecode keeps pydantic-settings from JSON-decoding the text before the
    validator sees it; the validator accepts the documented ``a=b,c=d`` form.
    Unknown engine ids are checked against the registry at AppContext startup.
    """

    # --- GPU lifecycle --------------------------------------------------
    model_idle_ttl_seconds: float = Field(default=900.0, alias="MODEL_IDLE_TTL_SECONDS")
    max_active_engines: int = Field(default=1, ge=1, le=2, alias="MAX_ACTIVE_ENGINES")
    max_resident_models: int = Field(default=1, ge=1, alias="MAX_RESIDENT_MODELS")
    max_gpu_vram_gb: float = Field(default=12.5, alias="MAX_GPU_VRAM_GB")
    min_engine_residency_seconds: float = Field(
        default=60.0, alias="MIN_ENGINE_RESIDENCY_SECONDS"
    )
    min_free_vram_gb: float = Field(default=2.0, alias="MIN_FREE_VRAM_GB")
    enable_vram_pressure_yield: bool = Field(default=True, alias="ENABLE_VRAM_PRESSURE_YIELD")
    max_queue_depth: int = Field(default=64, ge=1, alias="MAX_QUEUE_DEPTH")
    """Uncached GPU jobs waiting behind an engine lock before admission returns 429.

    Cache hits and read-only calls never enter this queue: they are answered before
    the engine slot is requested.
    """

    # --- Cache / audio --------------------------------------------------
    max_cache_gb: float = Field(default=5.0, alias="MAX_CACHE_GB")
    chunk_chars: int = Field(default=400, ge=1, alias="CHUNK_CHARS")
    max_text_chars: int = Field(default=20000, ge=1, alias="MAX_TEXT_CHARS")
    max_script_beats: int = Field(default=200, ge=1, alias="MAX_SCRIPT_BEATS")
    max_instruct_chars: int = Field(default=1000, ge=1, alias="MAX_INSTRUCT_CHARS")
    max_design_sample_chars: int = Field(
        default=500, ge=1, alias="MAX_DESIGN_SAMPLE_CHARS"
    )
    max_ref_text_chars: int = Field(default=2000, ge=1, alias="MAX_REF_TEXT_CHARS")
    normalize_lufs: float = Field(default=-16.0, alias="NORMALIZE_LUFS")
    max_transcribe_seconds: float = Field(
        default=3600.0, gt=0, alias="MAX_TRANSCRIBE_SECONDS"
    )
    """Refuse media longer than this at admission, before ffmpeg or a worker.

    An hour is a generous default for a podcast; it is a cap on *work*, not on
    file size, so an oversized upload costs a probe and nothing else.
    """
    max_dub_segments: int = Field(default=500, ge=1, alias="MAX_DUB_SEGMENTS")
    """Cap on translated lines in one dub_render, checked before synthesis."""

    # --- Ingest / drafts / durable studio ---------------------------------
    ingest_max_bytes: int = Field(default=512 * 1024**2, ge=1024, alias="INGEST_MAX_BYTES")
    ingest_max_channels: int = Field(default=8, ge=1, le=32, alias="INGEST_MAX_CHANNELS")
    ingest_max_sample_rate: int = Field(
        default=192_000, ge=8_000, le=768_000, alias="INGEST_MAX_SAMPLE_RATE"
    )
    ingest_max_streams: int = Field(default=8, ge=1, le=64, alias="INGEST_MAX_STREAMS")
    ingest_max_decoded_bytes: int = Field(
        default=256 * 1024**2, ge=1024, alias="INGEST_MAX_DECODED_BYTES"
    )
    ingest_probe_timeout_seconds: float = Field(
        default=5.0, gt=0, le=120, alias="INGEST_PROBE_TIMEOUT_SECONDS"
    )
    ingest_concurrency: int = Field(default=2, ge=1, le=16, alias="INGEST_CONCURRENCY")
    voice_draft_ttl_seconds: float = Field(
        default=3600.0, ge=60, le=86400, alias="VOICE_DRAFT_TTL_SECONDS"
    )
    voice_draft_max_count: int = Field(
        default=8, ge=1, le=1000, alias="VOICE_DRAFT_MAX_COUNT"
    )
    voice_draft_max_bytes: int = Field(
        default=512 * 1024**2, ge=1024, alias="VOICE_DRAFT_MAX_BYTES"
    )
    voice_draft_quota_bytes: int = Field(
        default=2 * 1024**3, ge=1024, alias="VOICE_DRAFT_QUOTA_BYTES"
    )
    voice_draft_max_duration_seconds: float = Field(
        default=60.0, gt=0, le=600, alias="VOICE_DRAFT_MAX_DURATION_SECONDS"
    )
    history_max_items: int = Field(default=2000, ge=1, alias="HISTORY_MAX_ITEMS")
    capture_max_items: int = Field(default=500, ge=1, alias="CAPTURE_MAX_ITEMS")
    capture_max_total_bytes: int = Field(
        default=5 * 1024**3, ge=1024, alias="CAPTURE_MAX_TOTAL_BYTES"
    )
    capture_max_transcript_chars: int = Field(
        default=200_000, ge=1000, alias="CAPTURE_MAX_TRANSCRIPT_CHARS"
    )
    artifact_ttl_seconds: float = Field(
        default=7 * 24 * 3600, ge=60, alias="ARTIFACT_TTL_SECONDS"
    )

    # --- Streaming ------------------------------------------------------
    stream_queue_maxsize: int = Field(default=32, ge=1, alias="STREAM_QUEUE_MAXSIZE")
    stream_queue_timeout_seconds: float = Field(
        default=5.0, alias="STREAM_QUEUE_TIMEOUT_SECONDS"
    )

    # --- Agent tooling --------------------------------------------------
    allow_agent_engine_mutation: bool = Field(
        default=False, alias="ALLOW_AGENT_ENGINE_MUTATION"
    )
    require_engine_approval: bool = Field(
        default=True, alias="REQUIRE_ENGINE_APPROVAL"
    )
    """When true, register_engine stages a plugin under engines/.pending/<id>/
    instead of activating it. An operator promotes it with `engines approve`.

    On by default: an agent-authored plugin is Python the daemon will execute, so a
    human approval gate is the safe default even when mutation is enabled.
    """
    toolset: Literal["auto", "core", "full"] = Field(default="auto", alias="AGENT_VOICE_TOOLSET")
    """Which MCP tools to expose.

    ``auto`` exposes the read/synthesis surface when agent mutation is disabled,
    and the full surface when it is enabled. ``core`` and ``full`` force it.
    """
    mcp_base_path: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_VOICE_MCP_BASE_PATH", "MCP_BASE_PATH"),
    )

    # --- HTTP security --------------------------------------------------
    # NoDecode stops pydantic-settings from JSON-decoding this list from the
    # environment *before* the validator runs, which would make a plain
    # `testserver` or `a,b` value raise SettingsError. `_split_hosts` accepts
    # both comma text and a JSON array.
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="AGENT_VOICE_ALLOWED_HOSTS"
    )
    """Extra Host header values accepted by the DNS-rebinding guard, beyond the
    loopback defaults. Accepts `a,b` from the environment."""

    allow_any_path: bool = Field(default=False, alias="AGENT_VOICE_ALLOW_ANY_PATH")
    """When false (the default), agent-supplied clone_voice paths are confined to
    ``<DATA_DIR>/inbox/``. The CLI `voices add` is an operator command and stays
    unrestricted; set this true only for a trusted local agent that needs other
    paths."""

    # --- Paths ----------------------------------------------------------
    # Declared first: the path validators below read it from ValidationInfo.
    # *Not* named `home`: populate_by_name=True would then match the standard
    # $HOME environment variable and point the whole install at the user's
    # home directory. `home` is a read-only property over this field.
    agent_home: Path = Field(default_factory=default_home, alias="AGENT_VOICE_HOME")
    data_dir: Path = Field(
        default=Path("data"),
        validation_alias=AliasChoices("AGENT_VOICE_DATA_DIR", "DATA_DIR"),
    )
    engines_dir: Path = Field(
        default=Path("engines"),
        validation_alias=AliasChoices("AGENT_VOICE_ENGINES_DIR", "ENGINES_DIR"),
    )

    @field_validator("normalize_lufs")
    @classmethod
    def _validate_lufs(cls, value: float) -> float:
        if not -40.0 <= value <= -5.0:
            raise ValueError("NORMALIZE_LUFS must be between -40 and -5")
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_api_key(cls, value: object) -> object:
        """Whitespace around a key is never part of it — and only whitespace is unset.

        ``AGENT_VOICE_API_KEY=" "`` used to count as "a key is set", so the daemon
        would happily bind a LAN address with effectively no authentication while
        the guard compared incoming headers against a single space. Stripping here
        is what makes the bind refusal and the guard agree on "no key".
        """
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("default_voice_id")
    @classmethod
    def _validate_default_voice_id(cls, value: str) -> str:
        if not value:
            return value
        try:
            return validate_resource_id(
                value,
                noun="default voice id",
                hint="Use a voice id/name of at most 64 safe characters.",
            )
        except AgentVoiceError as exc:
            raise ValueError(exc.message) from exc

    @field_validator("ui_enabled", mode="before")
    @classmethod
    def _parse_ui_enabled(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"on", "true", "yes", "1"}:
                return True
            if text in {"off", "false", "no", "0"}:
                return False
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        """Accept `a,b`, `["a","b"]` or a real list.

        Both text forms show up in practice: a human writes comma text in .env,
        while a script or the older documented example writes a JSON array.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("devices", mode="before")
    @classmethod
    def _parse_devices(cls, value: object) -> object:
        """Accept ``kokoro=cpu,qwen3_tts=cuda`` from the environment.

        The resulting dict is validated against ``ComputeDevice`` by pydantic;
        this only splits the documented text form.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return {}
        parsed: dict[str, str] = {}
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "=" not in part:
                raise ValueError(
                    "AGENT_VOICE_DEVICES entries must look like engine=device; "
                    f"got {part!r}"
                )
            engine, device = (piece.strip() for piece in part.split("=", 1))
            if not engine or not device:
                raise ValueError(
                    "AGENT_VOICE_DEVICES entries need both an engine and a device; "
                    f"got {part!r}"
                )
            parsed[engine] = device
        return parsed

    @field_validator("agent_home", mode="after")
    @classmethod
    def _home_absolute(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("data_dir", "engines_dir", mode="after")
    @classmethod
    def _under_home(cls, value: Path, info: ValidationInfo) -> Path:
        """Resolve a relative path against the home, never against the cwd.

        Running `agent-voice` from any directory must use the checkout's
        ``data/`` and ``engines/``; a relative path in ``.env`` must not create
        directories in the caller's cwd. Absolute paths are unchanged.
        """
        home: Path = info.data.get("agent_home") or default_home()
        candidate = value.expanduser()
        if not candidate.is_absolute():
            candidate = home / candidate
        return candidate.resolve()

    @property
    def home(self) -> Path:
        """The resolved home. A property, not a field, so $HOME cannot match it."""
        return self.agent_home

    @property
    def config_path(self) -> Path:
        """``<home>/.env`` — the file get_settings() reads."""
        return self.home / ".env"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def drafts_dir(self) -> Path:
        return self.data_dir / "drafts"

    @property
    def history_dir(self) -> Path:
        return self.data_dir / "history"

    @property
    def captures_dir(self) -> Path:
        return self.data_dir / "captures"

    @property
    def web_dir(self) -> Path:
        """Built SPA package data, never the ``frontend/`` source tree."""
        return Path(__file__).resolve().parent / "web"

    @property
    def dubs_dir(self) -> Path:
        """Per-dub working directories: ``<data>/dubs/<dub_id>/``.

        Holds the extracted 16 kHz ASR WAV, the 24 kHz stereo bed, the speaker
        reference clip and ``project.json``. Deleted once a render succeeds.
        """
        return self.data_dir / "dubs"

    @property
    def artifacts_dir(self) -> Path:
        if self.mcp_base_path:
            return Path(self.mcp_base_path).resolve() / "artifacts"
        return self.data_dir / "artifacts"

    @property
    def bindings_path(self) -> Path:
        return self.data_dir / "bindings.json"

    @property
    def pronunciations_path(self) -> Path:
        return self.data_dir / PRONUNCIATION_FILE

    @property
    def pronunciations_dir(self) -> Path:
        """Project-scoped lexicons: ``<data>/pronunciations/<project>.json``.

        Deliberately separate from :attr:`pronunciations_path` so a project named
        ``json`` could never collide with the global file.
        """
        return self.data_dir / PROJECT_LEXICON_DIR

    @property
    def max_lexicon_entries(self) -> int:
        """Per-scope entry cap: the global, project, voice and per-call maps.

        A read-only view of the module constant, not an env knob: the caps are a
        security boundary (an unbounded alternation is an unbounded regex), so
        they are raised only by editing the contract, deliberately.
        """
        return MAX_LEXICON_ENTRIES

    @property
    def max_merged_lexicon_entries(self) -> int:
        """Cap on the merged global + project + voice + per-call map."""
        return MAX_MERGED_LEXICON_ENTRIES

    @property
    def max_hotwords(self) -> int:
        """Hotword cap offered to callers, read-only like the lexicon caps.

        Structural rather than an env knob: it is the decoder's own bias-list
        bound, so raising it means changing the contract deliberately.
        """
        return MAX_HOTWORDS

    @property
    def daemon_lock_path(self) -> Path:
        return self.data_dir / "daemon.lock"

    @property
    def base_url(self) -> str:
        """The daemon's own URL, with an IPv6 literal bracketed.

        ``http://::1:8000`` is not a URL: every colon in the literal is also the
        host/port separator, so clients parse the port as ``1:8000``. RFC 3986
        requires ``[::1]`` inside a URI authority, which is what callers of this
        property (the CLI, the MCP proxy) hand to httpx.
        """
        host = self.host.strip()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.voices_dir,
            self.inbox_dir,
            self.cache_dir,
            self.jobs_dir,
            self.drafts_dir,
            self.history_dir,
            self.captures_dir,
            self.dubs_dir,
            self.artifacts_dir,
            self.pronunciations_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=settings_env_file())


def reset_settings_cache() -> None:
    """Test helper: drop cached settings so env changes take effect."""
    get_settings.cache_clear()
