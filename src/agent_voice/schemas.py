"""Frozen wire contract for agent-voice.

IMMUTABLE CONTRACT. Every wave after Wave 1 codes against this module.
Changing anything here requires the main agent to amend the contract and
re-run all dependent test authors.

This module must remain import-safe without torch installed.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .pronunciations import validate_pronunciation_map

PROTOCOL_VERSION = 1

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{1,40}$")
ALIAS_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def validate_slug(value: str) -> str:
    """Engine ids are strictly alphanumeric slugs.

    Rejects anything that could traverse the filesystem or shadow internals.
    Lives here (not in security.py) because it is part of the frozen contract.
    """
    if not isinstance(value, str) or not SLUG_PATTERN.match(value):
        raise ValueError(
            "must match ^[a-z0-9][a-z0-9_]{1,40}$ (lowercase alphanumeric/underscore, "
            "2-41 chars, cannot start with underscore)"
        )
    if value.startswith("_"):
        raise ValueError("must not start with an underscore (reserved for internals)")
    return value


def validate_alias(value: str) -> str:
    """Model aliases are looser than engine ids: they may contain dots and dashes.

    Shipped aliases like ``1.7b-base`` make this necessary. They still cannot
    contain path separators or traversal sequences, because they become cache
    directory components.
    """
    if not isinstance(value, str) or not ALIAS_PATTERN.match(value):
        raise ValueError(
            "must match ^[a-z0-9][a-z0-9._-]{0,63}$ (lowercase, may contain . _ -)"
        )
    if ".." in value:
        raise ValueError("must not contain '..'")
    return value


VOICE_NAME_PATTERN = re.compile(r"^[\w .\-]{1,64}$")
"""Human-facing voice names: 1-64 word chars, spaces, dots and hyphens.

Voice ids are random hex and never derived from the name; the pattern exists so a
name cannot smuggle a path separator, a newline or a control character into
metadata, logs or subtitles.
"""


def validate_voice_name(value: str) -> str:
    if not isinstance(value, str) or VOICE_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(
            "voice name must be 1-64 characters of letters, digits, spaces, '.', '-' or '_'"
        )
    return value


SAMPLE_RATE = 24_000
"""Engine output sample rate. PCM_CHUNK payloads are always 24 kHz mono int16 LE."""

ResponseFormat = Literal["wav", "mp3", "opus", "flac", "pcm"]
"""pcm is headerless raw 24 kHz mono int16 LE."""

QualityTier = Literal["high", "fast"]

ModelMode = Literal["clone", "custom_voice", "voice_design"]
"""What a checkpoint can do: clone a reference, speak preset timbres, or design a voice."""


class ErrorCode(StrEnum):
    """Single error taxonomy shared by IPC, REST and MCP."""

    WORKER_BUSY = "WORKER_BUSY"
    WORKER_CRASHED = "WORKER_CRASHED"
    CUDA_OOM = "CUDA_OOM"
    JOB_CANCELLED = "JOB_CANCELLED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    ENGINE_NOT_FOUND = "ENGINE_NOT_FOUND"
    ENGINE_NOT_INSTALLED = "ENGINE_NOT_INSTALLED"
    VOICE_NOT_FOUND = "VOICE_NOT_FOUND"
    VOICE_REQUIRED = "VOICE_REQUIRED"
    """No voice exists or was selected, and the engine cannot synthesise without
    one. Distinct from VOICE_NOT_FOUND so an agent can tell "clone a voice first"
    apart from "that voice id is wrong"."""
    INVALID_REF_AUDIO = "INVALID_REF_AUDIO"
    TEXT_TOO_LONG = "TEXT_TOO_LONG"
    PATH_OUTSIDE_BASE = "PATH_OUTSIDE_BASE"
    AGENT_MUTATION_DISABLED = "AGENT_MUTATION_DISABLED"
    INVALID_SLUG = "INVALID_SLUG"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    TIMEOUT = "TIMEOUT"
    BACKPRESSURE = "BACKPRESSURE"
    DAEMON_RESTARTED = "DAEMON_RESTARTED"
    """A job was pending or running when the daemon stopped. It cannot be resumed,
    so it is reconciled to failed on the next startup rather than lying as
    "running" forever."""
    BAD_REQUEST = "BAD_REQUEST"
    INTERNAL_ERROR = "INTERNAL_ERROR"


HTTP_STATUS_BY_ERROR: dict[ErrorCode, int] = {
    ErrorCode.WORKER_BUSY: 409,
    ErrorCode.WORKER_CRASHED: 500,
    ErrorCode.CUDA_OOM: 503,
    ErrorCode.JOB_CANCELLED: 409,
    ErrorCode.MODEL_NOT_FOUND: 404,
    ErrorCode.ENGINE_NOT_FOUND: 404,
    ErrorCode.ENGINE_NOT_INSTALLED: 409,
    ErrorCode.VOICE_NOT_FOUND: 404,
    ErrorCode.VOICE_REQUIRED: 422,
    ErrorCode.INVALID_REF_AUDIO: 422,
    ErrorCode.TEXT_TOO_LONG: 413,
    ErrorCode.PATH_OUTSIDE_BASE: 403,
    ErrorCode.AGENT_MUTATION_DISABLED: 403,
    ErrorCode.INVALID_SLUG: 422,
    ErrorCode.MANIFEST_INVALID: 422,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.BACKPRESSURE: 429,
    ErrorCode.DAEMON_RESTARTED: 410,
    ErrorCode.BAD_REQUEST: 400,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ErrorResponse(BaseModel):
    """Canonical error body. Never a bare string, never a stack trace."""

    error_code: ErrorCode
    message: str
    suggestion: str = ""


class AgentVoiceError(Exception):
    """Raised anywhere in the daemon; rendered as :class:`ErrorResponse`."""

    def __init__(
        self,
        error_code: ErrorCode,
        message: str,
        suggestion: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.suggestion = suggestion
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_ERROR.get(self.error_code, 500)

    def to_response(self) -> ErrorResponse:
        return ErrorResponse(
            error_code=self.error_code, message=self.message, suggestion=self.suggestion
        )


# ---------------------------------------------------------------------------
# Engine manifest (engines/<engine_id>/engine.yaml)
# ---------------------------------------------------------------------------


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str
    hf_repo: str
    quality_tier: QualityTier = "high"
    est_vram_gb: float = 4.4
    default: bool = False
    mode: ModelMode = "clone"
    supports_instruct: bool = False
    preset_speakers: list[str] = Field(default_factory=list)

    @field_validator("alias")
    @classmethod
    def _alias_slug(cls, value: str) -> str:
        return validate_alias(value)


class EngineCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    streaming: bool = False
    pcm_native: bool = False
    voice_clone: bool = True
    voice_design: bool = False
    custom_voice: bool = False
    instruct_control: bool = False
    languages: list[str] = Field(default_factory=list)


class EngineManifest(BaseModel):
    """Declarative plugin manifest. Declares backend pins so the cache key is
    computable *before* a worker is ever spawned (see architecture note 2.4)."""

    model_config = ConfigDict(extra="forbid")

    engine_id: str
    display_name: str = ""
    backend_name: str
    backend_version: str = "0"
    protocol_version: int = PROTOCOL_VERSION
    venv_python: str | None = None
    """Relative path to the engine venv interpreter. None => core sys.executable."""
    capabilities: EngineCapabilities = Field(default_factory=EngineCapabilities)
    models: list[ModelSpec] = Field(default_factory=list)

    @field_validator("engine_id")
    @classmethod
    def _engine_slug(cls, value: str) -> str:
        return validate_slug(value)


class ModelEntry(BaseModel):
    """A model as reported by list_models(), across engines."""

    engine_id: str
    alias: str
    hf_repo: str
    quality_tier: QualityTier
    est_vram_gb: float
    is_default: bool = False
    downloaded_locally: bool = False
    loaded: bool = False
    mode: ModelMode = "clone"
    supports_instruct: bool = False
    preset_speakers: list[str] = Field(default_factory=list)


class EngineEntry(BaseModel):
    """An engine as reported by list_engines()."""

    engine_id: str
    display_name: str = ""
    backend_name: str
    backend_version: str
    installed: bool
    venv_python: str | None = None
    streaming: bool = False
    pcm_native: bool = False
    custom_voice: bool = False
    voice_design: bool = False
    instruct_control: bool = False
    models: list[ModelEntry] = Field(default_factory=list)
    why_unavailable: str | None = None


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


class VoiceMetadata(BaseModel):
    voice_id: str
    name: str
    ref_wav_sha256: str
    ref_text: str = ""
    x_vector_only_mode: bool = False
    duration_sec: float
    created_at: float
    warnings: list[str] = Field(default_factory=list)
    source: Literal["cloned", "designed"] = "cloned"
    """Where the reference came from: a user clip or a VoiceDesign render."""
    design_instruct: str = ""
    """The natural-language description used when source == "designed"."""

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_voice_name(value)


class CloneVoiceRequest(BaseModel):
    name: str
    ref_audio_path: str
    ref_text: str = ""
    x_vector_only: bool = False

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_voice_name(value)


class VoiceListResponse(BaseModel):
    voices: list[VoiceMetadata]


DEFAULT_DESIGN_SAMPLE_TEXT = (
    "Welcome! Today we are going to break down this concept step by step, "
    "clearly and intuitively."
)
"""The 5-8 s anchor used when a caller does not supply its own sentence."""


class DesignVoiceRequest(BaseModel):
    name: str
    instruct: str
    sample_text: str = DEFAULT_DESIGN_SAMPLE_TEXT
    language: str = "English"
    save_as_clone: bool = True
    candidates: int = Field(default=1, ge=1, le=5)
    take: int | None = Field(default=None, ge=1)
    seed: int | None = None
    overwrite: bool = False
    engine: str | None = None
    model: str | None = None
    quality: QualityTier = "high"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return validate_voice_name(value)


class DesignCandidate(BaseModel):
    take: int
    seed: int | None = None
    file_path: str
    download_url: str = ""
    duration_sec: float


class DesignVoiceResult(BaseModel):
    voice_id: str | None = None
    name: str
    preview_file_path: str
    duration_sec: float
    source: Literal["designed"] = "designed"
    design_instruct: str
    engine_id: str
    model_alias: str
    candidates: list[DesignCandidate] = Field(default_factory=list)
    """When candidates > 1, every take to audition; the first is also the preview."""


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


class AgentBinding(BaseModel):
    agent_id: str
    voice_id: str | None = None
    engine_id: str | None = None
    model_alias: str | None = None


class BindingsResponse(BaseModel):
    bindings: list[AgentBinding]


class SetBindingRequest(BaseModel):
    voice_id: str | None = None
    engine_id: str | None = None
    model_alias: str | None = None


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    speaker: str | None = None
    instruct: str = ""
    engine: str | None = None
    model: str | None = None
    quality: QualityTier = "high"
    language: str = "Auto"
    format: ResponseFormat = "wav"
    seed: int | None = None
    speed: float = 1.0
    take: int | None = Field(default=None, ge=1)
    """Alternate delivery of the same line. Derives a repeatable seed when `seed`
    is not given, so take=2 is reproducible and cache-stable."""
    pronunciations: dict[str, str] | None = None
    """Per-call word -> spoken-form overrides merged over data/pronunciations.json."""

    @field_validator("pronunciations")
    @classmethod
    def _pronunciations(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        validate_pronunciation_map(value)
        return value


class SpeechRequest(BaseModel):
    """Canonical OpenAI /v1/audio/speech body."""

    model: str = "tts-1"
    voice: str | None = None
    input: str
    response_format: ResponseFormat = "wav"
    speed: float = 1.0


class SynthesisResult(BaseModel):
    artifact_id: str
    file_path: str
    """Absolute path. Materialised inside MCP_BASE_PATH/artifacts when that is set."""
    duration_sec: float
    sample_rate: int = SAMPLE_RATE
    format: ResponseFormat = "wav"
    engine_id: str
    model_alias: str
    backend_name: str
    backend_version: str
    voice_id: str | None = None
    mode: ModelMode = "clone"
    speaker: str | None = None
    cached: bool = False
    streaming_mode: Literal["native", "emulated"] | None = None
    download_url: str = ""
    """Relative HTTP URL for this artifact, e.g. /audio/<id>. Saves an agent a
    filesystem read when it would rather hand a URL to another process."""
    chunks_used: int = 1
    """How many text chunks were synthesised and stitched. >1 means the input
    exceeded chunk_chars and was split."""
    elapsed_ms: int = 0
    """Wall-clock cost of this call. On a cache hit this is the (tiny) lookup
    time, which makes latency regressions obvious to a caller."""
    take: int | None = None
    """Which alternate delivery was rendered. A new take yields different audio
    without disturbing the cache entry for take 1."""
    realtime_multiple: float | None = None
    """duration_sec / elapsed_sec: 0.86 means slightly slower than real time,
    7.1 about 7x faster. None for streamed output."""
    real_time_factor: float | None = None
    """Deprecated alias of realtime_multiple, kept for one release."""


class BeatSpec(BaseModel):
    text: str
    voice: str | None = None
    speaker: str | None = None
    instruct: str = ""
    engine: str | None = None
    model: str | None = None
    quality: QualityTier | None = None
    """Omit to inherit ScriptRequest.quality; an explicit value wins."""
    language: str = "Auto"
    target_duration_sec: float | None = None
    fit_mode: Literal["none", "pad", "atempo"] = "none"
    pause_after_sec: float = Field(default=0.0, ge=0.0, le=10.0)
    """Silence inserted after this beat, before the next one starts.

    Video pipelines (Manim, slideshows) need this to leave room for a visual to
    land. Included in the reported timings, so subtitles stay frame-accurate.
    """
    take: int | None = Field(default=None, ge=1)
    """Alternate delivery of this beat's line, for re-recording one beat without
    touching the others."""


class ScriptRequest(BaseModel):
    beats: list[BeatSpec]
    default_voice: str | None = None
    engine: str | None = None
    quality: QualityTier = "high"
    format: ResponseFormat = "wav"
    export_srt: bool = True
    pronunciations: dict[str, str] | None = None
    """Per-call overrides applied to every beat's spoken text."""

    @field_validator("pronunciations")
    @classmethod
    def _pronunciations(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        validate_pronunciation_map(value)
        return value


class BeatTiming(BaseModel):
    index: int
    text: str
    duration_sec: float
    """Spoken length of this beat, excluding any trailing pause."""
    start_sec: float
    end_sec: float
    """End of the spoken portion. A following beat starts at end_sec + pause_after_sec."""
    pause_after_sec: float = 0.0
    target_duration_sec: float | None = None
    fit_mode: str = "none"
    voice_id: str | None = None
    """Which voice actually spoke this beat, so a multi-speaker script is
    auditable from the result alone."""
    mode: ModelMode = "clone"
    speaker: str | None = None
    instruct: str = ""
    cached: bool = False
    """True when re-rendering an edited script reused this beat from disk.
    Editing one beat of twelve should show eleven instant hits."""
    artifact_id: str = ""
    """The per-beat artifact id, so a caller can fetch or replace one beat."""


class ScriptResult(BaseModel):
    artifact_id: str
    file_path: str
    duration_sec: float
    beats: list[BeatTiming]
    srt_path: str | None = None
    vtt_path: str | None = None
    timing_json_path: str | None = None
    engine_id: str
    model_alias: str
    format: ResponseFormat = "wav"


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class Job(BaseModel):
    job_id: str
    kind: Literal["script", "download_model", "install_engine"]
    status: JobStatus = "pending"
    progress: float = 0.0
    message: str = ""
    created_at: float
    updated_at: float
    result: dict[str, Any] | None = None
    error: ErrorResponse | None = None


# ---------------------------------------------------------------------------
# GPU lifecycle
# ---------------------------------------------------------------------------


class GpuStatus(BaseModel):
    active_engine: str | None = None
    loaded_models: list[str] = Field(default_factory=list)
    workers: dict[str, str] = Field(default_factory=dict)
    """engine_id -> alive|starting|dead"""
    vram_used_mb: int | None = None
    vram_free_mb: int | None = None
    queue_depth: int = 0
    in_flight: int = 0
    unload_in_seconds: float | None = None
    last_used_at: float | None = None


class UnloadResult(BaseModel):
    unloaded: list[str] = Field(default_factory=list)
    cancelled_jobs: int = 0


# ---------------------------------------------------------------------------
# Engine/model registration (agent-driven)
# ---------------------------------------------------------------------------


class DownloadModelRequest(BaseModel):
    engine_id: str
    model_alias: str


class RegisterModelRequest(BaseModel):
    engine_id: str
    model_alias: str
    hf_repo: str
    quality_tier: QualityTier = "high"
    est_vram_gb: float = 4.4
    download: bool = True


class RegisterEngineRequest(BaseModel):
    engine_id: str
    engine_yaml_content: str
    pyproject_toml_content: str | None = None
    worker_py_content: str | None = None
    install_venv: bool = True


class JobStartedResponse(BaseModel):
    job_id: str
    kind: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    protocol_version: int = PROTOCOL_VERSION
    torch_loaded: bool = False


# ---------------------------------------------------------------------------
# MCP tool payload aliases (kept explicit so MCP and REST cannot drift)
# ---------------------------------------------------------------------------

AgentId = Annotated[str, Field(description="Convenience routing id, not an auth boundary")]
