"""Configuration for the agent-voice core daemon.

The core daemon must never import torch. Engine-specific dependencies live in
isolated per-engine virtualenvs under ``engines/<engine_id>/.venv``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .pronunciations import (
    MAX_LEXICON_ENTRIES,
    MAX_MERGED_LEXICON_ENTRIES,
    PROJECT_LEXICON_DIR,
    PRONUNCIATION_FILE,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Server ---------------------------------------------------------
    host: str = Field(default="127.0.0.1", alias="AGENT_VOICE_HOST")
    port: int = Field(default=8000, alias="AGENT_VOICE_PORT")
    api_key: str = Field(default="", alias="AGENT_VOICE_API_KEY")

    # --- Defaults -------------------------------------------------------
    default_engine: str = Field(default="mock", alias="AGENT_VOICE_DEFAULT_ENGINE")
    default_voice_id: str = Field(default="", alias="AGENT_VOICE_DEFAULT_VOICE_ID")

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
    mcp_base_path: str = Field(default="", alias="MCP_BASE_PATH")

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
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    engines_dir: Path = Field(default=Path("./engines"), alias="ENGINES_DIR")

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

    @field_validator("data_dir", "engines_dir")
    @classmethod
    def _absolute(cls, value: Path) -> Path:
        """Resolve to an absolute path at load time.

        Workers are spawned with ``cwd=<engine dir>``, so any relative path in
        their environment (notably the IPC socket) would resolve somewhere else
        and the handshake would fail with a confusing ENOENT.
        """
        return value.expanduser().resolve()

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
            self.artifacts_dir,
            self.pronunciations_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop cached settings so env changes take effect."""
    get_settings.cache_clear()
