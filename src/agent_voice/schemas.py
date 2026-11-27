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

EngineKind = Literal["tts", "asr"]
"""Which family a plugin belongs to.

``tts`` speaks (``synthesize``/``stream_pcm``), ``asr`` listens (``transcribe``).
The default is ``tts`` on every manifest field, so an engine directory that never
mentions the key keeps behaving exactly as it did before v0.5.
"""

EngineDevice = Literal["gpu", "cpu"]
"""Where a worker's compute runs, for scheduling rather than routing.

``gpu`` engines compete for the single GPU owner slot (``MAX_ACTIVE_ENGINES``);
``cpu`` engines do not, so a CPU transcription can run while a GPU render is
resident instead of evicting it. This is the *default* scheduling class; the
resolved runtime device (``ComputeDevice``) is what actually decides.
"""

ComputeDevice = Literal["cpu", "cuda", "mps", "rocm"]
"""The concrete backend a worker runs on. ``mps`` also covers Fermion's MLX
path on Apple silicon; the operator-facing spelling stays ``mps``."""

DevicePreference = Literal["auto", "cpu", "cuda", "mps", "rocm"]
"""Operator preference: ``auto`` resolves per engine from the manifest and host."""

MAX_HOTWORDS = 25
"""Ceiling on ``hotwords`` — the decoder's own vocabulary-bias bound.

A contract constant, not an env knob: an unbounded word list is unbounded decoder
work, the same reasoning that keeps the lexicon caps structural.
"""


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
    word_timestamps: bool = False
    """``transcribe`` returns per-word ``start_sec``/``end_sec`` when asked."""
    hotwords: bool = False
    """The engine accepts a ``hotwords`` list and biases its decoder toward it."""
    multilingual: bool = False
    """One checkpoint covers several languages rather than one fixed language."""


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
    kind: EngineKind = "tts"
    """``tts`` speaks, ``asr`` transcribes. Default keeps v0.4 manifests valid."""
    device: EngineDevice = "gpu"
    """Default scheduling class; ``cpu`` is exempt from the GPU owner slot."""
    default_device: ComputeDevice | None = None
    """The engine's preferred runtime device for ``auto``. ``None`` lets the
    resolver pick from the host (cuda -> mps -> rocm -> cpu)."""
    supported_devices: list[ComputeDevice] = Field(default_factory=lambda: ["cpu"])
    """Devices this engine's code and dependency variants can actually attempt.

    Conservative by default: an old manifest that never mentions the key is
    treated as CPU-only rather than promised a GPU path it lacks.
    """
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
    kind: EngineKind = "tts"
    """Inherited from the engine manifest so list_models() can be filtered."""


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
    kind: EngineKind = "tts"
    device: EngineDevice = "gpu"
    default_device: ComputeDevice | None = None
    supported_devices: list[ComputeDevice] = Field(default_factory=list)
    resolved_device: str | None = None
    """What a running worker actually reports (``cuda``, ``cpu``, ...); None
    until this engine has been spawned in this process."""
    word_timestamps: bool = False
    hotwords: bool = False
    multilingual: bool = False
    languages: list[str] = Field(default_factory=list)
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
    markup: bool = True
    """Interpret the whitelisted expressive tags. False passes every bracket
    through literally, for narrating markup syntax itself."""
    spell_identifiers: bool = False
    """Expand code-like tokens before synthesis. Subtitles keep the written text."""
    paragraph_gap_ms: int = Field(default=0, ge=0, le=5000)
    """Silence inserted at each blank-line paragraph break inside this payload."""
    project: str | None = None
    """Named scoped-lexicon project (validated slug), merged over the global map."""

    @field_validator("pronunciations")
    @classmethod
    def _pronunciations(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        validate_pronunciation_map(value)
        return value

    @field_validator("project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
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
    markup: bool | None = None
    """Omit to inherit ScriptRequest.markup; an explicit value wins."""
    spell_identifiers: bool | None = None
    """Omit to inherit ScriptRequest.spell_identifiers; an explicit value wins."""
    paragraph_gap_ms: int | None = Field(default=None, ge=0, le=5000)
    """Omit to inherit ScriptRequest.paragraph_gap_ms; an explicit value wins."""
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    """Base beat speed; [slow]/[fast] multiply it and the result is clamped."""
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
    markup: bool = True
    spell_identifiers: bool = False
    paragraph_gap_ms: int = Field(default=0, ge=0, le=5000)
    project: str | None = None

    @field_validator("pronunciations")
    @classmethod
    def _pronunciations(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        validate_pronunciation_map(value)
        return value

    @field_validator("project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
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
    had_markup: bool = False
    """True when the beat's spoken text came from parsed expressive markup."""
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
# Transcription (kind: asr)
# ---------------------------------------------------------------------------


class TranscribeWord(BaseModel):
    word: str
    start_sec: float
    end_sec: float


class TranscribeSegment(BaseModel):
    """One timed span of speech, as ASR engines and dubbing both use it."""

    index: int
    start_sec: float
    end_sec: float
    text: str
    words: list[TranscribeWord] = Field(default_factory=list)
    """Only populated when the request asked for word timestamps."""


def validate_hotwords(value: list[str] | None) -> list[str] | None:
    """Cap and sanitise a hotword list before it reaches a decoder.

    Whitespace-only entries are dropped rather than rejected: a caller splitting a
    comma-separated prompt will produce them, and they carry no meaning.
    """
    if value is None:
        return None
    cleaned = [term.strip() for term in value if term and term.strip()]
    if len(cleaned) > MAX_HOTWORDS:
        raise ValueError(
            f"at most {MAX_HOTWORDS} hotwords are accepted (got {len(cleaned)})"
        )
    for term in cleaned:
        if len(term) > 64:
            raise ValueError(f"hotword is longer than 64 characters: {term[:20]}...")
    return cleaned


class TranscribeRequest(BaseModel):
    """A local-path transcription. Used by /v1/agent/transcribe and transcribe()."""

    audio_path: str
    engine: str | None = None
    model: str | None = None
    language: str | None = None
    """None or "auto" lets the engine detect it. A fixed-language checkpoint
    ignores this rather than guessing."""
    hotwords: list[str] | None = None
    word_timestamps: bool = False
    export_srt: bool = False
    project: str | None = None
    """Named scoped-lexicon project whose terms are offered as hotwords."""

    @field_validator("hotwords")
    @classmethod
    def _hotwords(cls, value: list[str] | None) -> list[str] | None:
        return validate_hotwords(value)

    @field_validator("project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
        return value


class TranscribeResult(BaseModel):
    text: str
    language: str = ""
    """What the engine reported. Empty or "auto" when it cannot say."""
    duration_sec: float
    segments_count: int
    segments: list[TranscribeSegment] = Field(default_factory=list)
    """A bounded preview. The full list is in ``transcript_json_path``."""
    engine_id: str
    model_alias: str
    backend_name: str = ""
    backend_version: str = ""
    elapsed_ms: int = 0
    realtime_multiple: float | None = None
    """audio_sec / wall_sec: 20.0 means 20x faster than real time."""
    truncated: bool = False
    """The backend flagged a decode window that ran out of token budget."""
    segments_omitted: int = 0
    """How many segments were dropped from the inline preview."""
    transcript_json_path: str | None = None
    srt_path: str | None = None
    vtt_path: str | None = None


# ---------------------------------------------------------------------------
# Dubbing (two-stage: prepare here, translate in the agent, render here)
# ---------------------------------------------------------------------------


class DubSegmentSpec(BaseModel):
    """One translated line, placed at the source segment's slot."""

    index: int
    start_sec: float
    end_sec: float
    text: str
    voice: str | None = None
    speaker: str | None = None
    instruct: str = ""
    engine: str | None = None
    model: str | None = None
    take: int | None = Field(default=None, ge=1)


class DubPrepareRequest(BaseModel):
    media_path: str
    source_language: str | None = None
    asr_engine: str | None = None
    asr_model: str | None = None
    hotwords: list[str] | None = None
    extract_speaker_ref: bool = True
    project: str | None = None

    @field_validator("hotwords")
    @classmethod
    def _hotwords(cls, value: list[str] | None) -> list[str] | None:
        return validate_hotwords(value)

    @field_validator("project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
        return value


class DubPrepareResult(BaseModel):
    dub_id: str
    media_path: str
    duration_sec: float
    source_language: str = ""
    speaker_ref_path: str | None = None
    speaker_ref_text: str = ""
    segments: list[TranscribeSegment] = Field(default_factory=list)
    engine_id: str
    model_alias: str
    project_json_path: str


class DubOverflow(BaseModel):
    """A line whose audio still overran its slot at the maximum tempo."""

    index: int
    slot_sec: float
    audio_sec: float
    atempo: float


class DubRenderRequest(BaseModel):
    dub_id: str
    segments: list[DubSegmentSpec]
    voice: str | None = None
    speaker: str | None = None
    auto_clone_source: bool = False
    """Clone the source speaker from speaker_ref.wav, reusing a voice already
    saved under ``dub-<dub_id>`` instead of registering a duplicate."""
    engine: str | None = None
    model: str | None = None
    quality: QualityTier = "high"
    keep_original_bed: bool = True
    bed_duck_db: float = Field(default=-18.0, ge=-40.0, le=0.0)
    max_atempo: float = Field(default=1.35, ge=1.0, le=2.0)
    """Hard ceiling on speed-up. A line that still overflows is reported, never
    truncated."""
    export_srt: bool = True
    burn_subtitles: bool = False
    project: str | None = None

    @field_validator("project")
    @classmethod
    def _project(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
        return value


class DubRenderResult(BaseModel):
    dub_id: str
    file_path: str
    duration_sec: float
    video_stream_copied: bool
    """True when the video was remuxed without re-encoding."""
    bed_ducked: bool = False
    """True when the original bed was mixed under the voice with a real
    sidechain duck. False means no bed was available, the ffmpeg build lacks
    ``sidechaincompress``/``alimiter``, or ``keep_original_bed`` was false."""
    segments_count: int
    cached_segments: int = 0
    """How many lines came from the synthesis cache. Re-rendering after editing
    one line should report all the others here."""
    overflow_warnings: list[DubOverflow] = Field(default_factory=list)
    srt_path: str | None = None
    vtt_path: str | None = None
    timing_json_path: str | None = None
    engine_id: str = ""
    model_alias: str = ""
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

JobStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class Job(BaseModel):
    job_id: str
    kind: Literal["script", "download_model", "install_engine", "dub"]
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
    cpu_engines: list[str] = Field(default_factory=list)
    """CPU workers that are alive but deliberately do not hold the GPU slot."""
    worker_devices: dict[str, str] = Field(default_factory=dict)
    """engine_id -> the device the worker itself reported."""
    worker_vram_mb: dict[str, int | None] = Field(default_factory=dict)
    """engine_id -> the worker's own allocation, None when it cannot say."""
    vram_source: Literal["nvml", "worker", "none"] = "none"
    """Where `vram_used_mb`/`vram_free_mb` came from; ``worker`` means no NVML."""
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
