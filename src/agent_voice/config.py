"""Configuration for the agent-voice core daemon.

The core daemon must never import torch. Engine-specific dependencies live in
isolated per-engine virtualenvs under ``engines/<engine_id>/.venv``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # --- Cache / audio --------------------------------------------------
    max_cache_gb: float = Field(default=5.0, alias="MAX_CACHE_GB")
    chunk_chars: int = Field(default=400, ge=1, alias="CHUNK_CHARS")
    max_text_chars: int = Field(default=20000, ge=1, alias="MAX_TEXT_CHARS")
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
        default=False, alias="REQUIRE_ENGINE_APPROVAL"
    )
    """When true, register_engine stages a plugin under engines/.pending/<id>/
    instead of activating it. An operator promotes it with `engines approve`."""
    toolset: Literal["auto", "core", "full"] = Field(default="auto", alias="AGENT_VOICE_TOOLSET")
    """Which MCP tools to expose.

    ``auto`` exposes the read/synthesis surface when agent mutation is disabled,
    and the full surface when it is enabled. ``core`` and ``full`` force it.
    """
    mcp_base_path: str = Field(default="", alias="MCP_BASE_PATH")

    # --- HTTP security --------------------------------------------------
    allowed_hosts: list[str] = Field(default_factory=list, alias="AGENT_VOICE_ALLOWED_HOSTS")
    """Extra Host header values accepted by the DNS-rebinding guard, beyond the
    loopback defaults. Accepts a comma-separated string from the environment."""

    # --- Paths ----------------------------------------------------------
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    engines_dir: Path = Field(default=Path("./engines"), alias="ENGINES_DIR")

    @field_validator("normalize_lufs")
    @classmethod
    def _validate_lufs(cls, value: float) -> float:
        if not -40.0 <= value <= -5.0:
            raise ValueError("NORMALIZE_LUFS must be between -40 and -5")
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        """Accept `a,b,c` from the environment as well as a real list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

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
    def daemon_lock_path(self) -> Path:
        return self.data_dir / "daemon.lock"

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.voices_dir,
            self.cache_dir,
            self.jobs_dir,
            self.artifacts_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop cached settings so env changes take effect."""
    get_settings.cache_clear()
