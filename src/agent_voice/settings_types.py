"""Structural type for the settings object.

Route modules and the MCP server only read a handful of settings attributes.
Describing them here keeps those modules importable in tests that build a
lightweight stand-in, without pulling in pydantic-settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class SettingsLike(Protocol):
    host: str
    port: int
    api_key: str
    default_engine: str
    default_voice_id: str
    model_idle_ttl_seconds: float
    max_active_engines: int
    max_resident_models: int
    max_gpu_vram_gb: float
    min_engine_residency_seconds: float
    min_free_vram_gb: float
    enable_vram_pressure_yield: bool
    max_queue_depth: int
    max_cache_gb: float
    chunk_chars: int
    max_text_chars: int
    max_script_beats: int
    max_instruct_chars: int
    max_design_sample_chars: int
    max_ref_text_chars: int
    normalize_lufs: float
    stream_queue_maxsize: int
    stream_queue_timeout_seconds: float
    allow_agent_engine_mutation: bool
    require_engine_approval: bool
    toolset: str
    allowed_hosts: list[str]
    mcp_base_path: str
    data_dir: Path
    engines_dir: Path

    @property
    def voices_dir(self) -> Path: ...

    @property
    def cache_dir(self) -> Path: ...

    @property
    def jobs_dir(self) -> Path: ...

    @property
    def artifacts_dir(self) -> Path: ...

    @property
    def bindings_path(self) -> Path: ...

    @property
    def pronunciations_path(self) -> Path: ...

    @property
    def daemon_lock_path(self) -> Path: ...

    @property
    def base_url(self) -> str: ...
