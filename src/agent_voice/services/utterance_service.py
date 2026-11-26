"""The one synthesis pipeline every surface shares.

Before v0.4.0 the REST route, the WebSocket, the MCP tool and each script beat
each had their own small version of "validate, rewrite the text, synthesize", and
they had already drifted (subtitles, pronunciation order, speed). Adding markup,
scoped lexicons and code normalization to four copies would have been four
subtly different behaviours. This module is the single implementation, in the
order the architecture fixes:

``S0 admission`` -> ``S1 parse markup`` -> ``S2 resolve_target`` -> ``S3 lexicon``
-> ``S4 code normalization`` -> ``S5 per-segment synthesis`` -> ``S6 stitch``

Why that order:

* Parsing happens on the **raw** text, before any rewriting: a lexicon
  replacement may itself contain ``[`` or ``]``, so rewriting first could
  fabricate a tag or destroy one.
* ``resolve_target`` sits between parse and normalization because the voice-scoped
  lexicon needs the resolved ``voice_id`` — including the auto-selected one.
* Lexicon before code normalization, with the lexicon's spans protected: a
  mapping like ``kubectl -> "cube control"`` wins over the heuristic, and
  ``[spell]`` runs are touched by neither. Double processing is structurally
  impossible rather than merely unlikely.

Caching: each speech segment goes through ``WorkerManager.synthesize``, so the
segment text and speed already enter the cache key. Pauses and paragraph gaps are
local silence and are never cached, and ``[emphasis]`` gain is applied at stitch
time, after the segment cache. ``CACHE_KEY_FIELDS`` is therefore untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import soundfile as sf

from ..api.routing import ResolvedTarget
from ..audio_utils import (
    CROSSFADE_MS,
    SAMPLE_RATE,
    apply_gain_file,
    convert_audio,
    crossfade_stitch,
    silence,
)
from ..markup import MAX_SPEED, MIN_SPEED, MarkupScript, parse_markup
from ..schemas import AgentVoiceError, ErrorCode, SynthesisResult
from ..security import resolve_artifact_path
from ..text_norm import lexicon_mapping, normalize_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlannedSegment:
    """One planned unit of synthesis, with every choice already made."""

    kind: Literal["speech", "pause"] = "speech"
    text: str = ""
    """The exact string handed to the engine (lexicon + code normalization done)."""
    display_text: str = ""
    """What the caller wrote, for subtitles and ``BeatTiming.text``."""
    speed: float = 1.0
    """Markup speed multiplier; ``1.0`` when the caller disabled markup speed."""
    gain_db: float = 0.0
    silence_ms: int = 0
    origin: str = "text"

    @property
    def is_pause(self) -> bool:
        return self.kind == "pause"


@dataclass(frozen=True)
class UtterancePlan:
    """A fully resolved utterance: ordered segments plus their display form."""

    raw_text: str
    display_text: str
    segments: tuple[PlannedSegment, ...]
    had_markup: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def speech(self) -> tuple[PlannedSegment, ...]:
        return tuple(segment for segment in self.segments if not segment.is_pause)

    @property
    def has_pauses(self) -> bool:
        return any(segment.is_pause for segment in self.segments)


@dataclass(frozen=True)
class UtteranceOutcome:
    """The rendered artifact plus the plan that produced it."""

    result: SynthesisResult
    plan: UtterancePlan

    @property
    def display_text(self) -> str:
        return self.plan.display_text

    @property
    def had_markup(self) -> bool:
        return self.plan.had_markup


@dataclass
class _RenderState:
    components: list[Path] = field(default_factory=list)
    component_keys: list[str] = field(default_factory=list)
    cached: bool = True
    chunks_used: int = 0
    backend_name: str = ""
    backend_version: str = ""


def clamp_speed(speed: float) -> float:
    """Every engine can speak 0.5x-2.0x; markup may ask for more, so clamp."""
    return min(MAX_SPEED, max(MIN_SPEED, float(speed)))


def effective_speed(base: float, multiplier: float = 1.0) -> float:
    """Combine a caller's speed with the markup multiplier for one segment.

    A segment with no markup speed keeps the caller's value untouched: that is
    the pre-v0.4 behaviour, and staging an unusually slow or fast read is a real
    use. Once markup is involved the product is clamped to the range every engine
    can speak — ``[slow]`` on top of ``speed=1.5`` must not become 4x.
    """
    if multiplier == 1.0:
        return float(base)
    return clamp_speed(float(base) * float(multiplier))


def _log_warnings(plan: UtterancePlan) -> None:
    for warning in plan.warnings:
        logger.warning("markup: %s", warning)


def plan_utterance(
    ctx: Any,
    script: MarkupScript,
    *,
    target: ResolvedTarget,
    project: str | None = None,
    spell_identifiers: bool = False,
    overrides: dict[str, str] | None = None,
    mapping: dict[str, str] | None = None,
    markup_speed: bool = True,
) -> UtterancePlan:
    """Resolve the spoken form of every segment (S3 + S4).

    Args:
        ctx: Application context; supplies the scoped lexicon.
        script: The parsed utterance from :func:`agent_voice.markup.parse_markup`.
        target: The already-resolved routing decision — its ``voice_id`` selects
            the voice layer of the lexicon.
        project: Named scoped-lexicon project from the request, if any.
        spell_identifiers: Apply code normalization outside the lexicon spans.
        overrides: Per-call lexicon overrides.
        mapping: A pre-computed effective lexicon (the script renderer memoises it
            per ``(project, voice_id)``); resolved here when omitted.
        markup_speed: False makes the fit mode the only thing that sets duration
            ("fit_mode wins over markup speed").
    """
    warnings = list(script.warnings)
    effective_mapping = (
        mapping
        if mapping is not None
        else lexicon_mapping(
            ctx, project=project, voice_id=target.voice_id, overrides=overrides
        )
    )

    segments: list[PlannedSegment] = []
    for segment in script.segments:
        if segment.is_pause:
            segments.append(
                PlannedSegment(
                    kind="pause", silence_ms=segment.silence_ms, origin=segment.origin
                )
            )
            continue
        spoken = normalize_text(
            segment.text,
            mapping=effective_mapping,
            spell=segment.spell,
            expand_identifiers=spell_identifiers,
        )
        if not spoken.strip():
            continue
        segments.append(
            PlannedSegment(
                kind="speech",
                text=spoken,
                display_text=segment.display_text,
                speed=segment.speed if markup_speed else 1.0,
                gain_db=segment.gain_db,
                origin=segment.origin,
            )
        )

    if not any(not segment.is_pause for segment in segments):
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "text has no speakable content",
            "Send some words, or use pause_after_sec (scripts) to insert silence.",
        )

    plan = UtterancePlan(
        raw_text=script.raw_text,
        display_text=script.display_text,
        segments=tuple(segments),
        had_markup=script.had_markup,
        warnings=tuple(warnings),
    )
    _log_warnings(plan)
    return plan


def _frames(path: Path) -> int:
    """Frame count of a rendered component, sample-accurate where possible."""
    import soundfile as sf

    try:
        return int(sf.info(str(path)).frames)
    except (RuntimeError, OSError):
        return 0


def _component_artifact_id(component_keys: Sequence[str], response_format: str) -> str:
    """Content-derived id for the stitched artifact.

    The components are the per-segment cache keys (each already a hash of the
    text, voice, speed and everything else that shaped that audio), plus the
    layout: pause lengths and emphasis gain. Identical requests therefore land on
    the identical path, which is what lets a repeat report ``cached=True`` and
    hand back the same file.
    """
    payload = json.dumps(
        {"components": list(component_keys), "format": response_format},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _next_is_speech(segments: Sequence[PlannedSegment], index: int) -> bool:
    return any(not segment.is_pause for segment in segments[index + 1 :])


async def render_utterance(
    ctx: Any,
    plan: UtterancePlan,
    *,
    target: ResolvedTarget,
    language: str = "Auto",
    seed: int | None = None,
    speed: float = 1.0,
    response_format: str = "wav",
    take: int | None = None,
    work_dir: Path | None = None,
) -> SynthesisResult:
    """Synthesize, gain, stitch and convert one planned utterance (S5 + S6).

    Silence is local (never cached), gain is applied to the segment audio before
    stitching, and the format conversion happens exactly once at the end — an
    mp3 render costs one ffmpeg pass, not one per segment.
    """
    settings = ctx.settings
    started = time.monotonic()
    own_work_dir = work_dir is None
    work = (
        Path(work_dir)
        if work_dir is not None
        else settings.artifacts_dir / f".utter-{uuid.uuid4().hex[:8]}"
    )
    work.mkdir(parents=True, exist_ok=True)
    state = _RenderState()

    try:
        for index, segment in enumerate(plan.segments):
            if segment.is_pause:
                if segment.silence_ms <= 0:
                    continue
                # A mid-utterance silence is 20 ms longer than asked for: the
                # crossfade at its far seam would otherwise eat into the pause the
                # caller requested. Trailing silence needs no compensation.
                pad_ms = CROSSFADE_MS if _next_is_speech(plan.segments, index) else 0
                pause_path = work / f"seg-{index:03d}-pause.wav"
                sf.write(
                    pause_path,
                    silence((segment.silence_ms + pad_ms) / 1000.0),
                    SAMPLE_RATE,
                    subtype="PCM_16",
                )
                state.components.append(pause_path)
                state.component_keys.append(f"pause:{segment.silence_ms}+{pad_ms}")
                continue

            segment_speed = effective_speed(speed, segment.speed)
            result = await ctx.workers.synthesize(
                engine_id=target.engine_id,
                model_alias=target.model_alias,
                text=segment.text,
                voice_id=target.voice_id,
                ref_text=target.ref_text,
                x_vector_only=target.x_vector_only,
                # Resolved, not raw: a preset speaker must never reach the worker
                # as a clone request, and a designed line carries its instruction.
                mode=target.mode,
                speaker=target.speaker,
                instruct=target.instruct,
                language=language,
                seed=seed,
                speed=segment_speed,
                response_format="wav",
                take=take,
            )
            state.cached = state.cached and bool(result.cached)
            state.chunks_used += int(result.chunks_used or 1)
            state.backend_name = state.backend_name or result.backend_name
            state.backend_version = state.backend_version or result.backend_version
            state.component_keys.append(
                f"{result.artifact_id}@{round(segment_speed, 6)}:{segment.gain_db}"
            )

            component = Path(result.file_path)
            if segment.gain_db:
                component = apply_gain_file(
                    component, work / f"seg-{index:03d}-gain.wav", segment.gain_db
                )
            state.components.append(component)

        if not state.components:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "text has no speakable content",
                "Send some words, or use pause_after_sec (scripts) to insert silence.",
            )

        if len(state.components) == 1:
            # Nothing to stitch: keep the component byte-for-byte instead of
            # round-tripping it through the float mixer.
            master = state.components[0]
            duration_sec = _frames(master) / float(SAMPLE_RATE)
        else:
            master = work / "utterance.wav"
            duration_sec = crossfade_stitch(
                state.components, master, crossfade_ms=CROSSFADE_MS
            )

        artifact_id = _component_artifact_id(state.component_keys, response_format)
        final = resolve_artifact_path(
            artifact_id,
            f".{response_format}",
            artifacts_dir=settings.artifacts_dir,
            mcp_base_path=settings.mcp_base_path,
        )
        if response_format == "wav":
            final.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(master, final)
        else:
            # One conversion for the whole utterance, after the stitch.
            convert_audio(
                master,
                final,
                response_format=response_format,
                normalize_lufs=(
                    settings.normalize_lufs if response_format != "pcm" else None
                ),
            )

        elapsed_ms = int((time.monotonic() - started) * 1000)
        realtime_multiple = (
            duration_sec / (elapsed_ms / 1000.0) if elapsed_ms > 0 else None
        )
        return SynthesisResult(
            artifact_id=artifact_id,
            file_path=str(final),
            duration_sec=round(duration_sec, 4),
            sample_rate=SAMPLE_RATE,
            format=response_format,
            engine_id=target.engine_id,
            model_alias=target.model_alias,
            backend_name=state.backend_name,
            backend_version=state.backend_version,
            # A preset or designed render has no reference voice to report.
            voice_id=target.voice_id if target.mode == "clone" else None,
            mode=target.mode,
            speaker=target.speaker,
            cached=state.cached,
            chunks_used=max(1, state.chunks_used),
            elapsed_ms=elapsed_ms,
            take=take,
            download_url=f"/audio/{artifact_id}",
            realtime_multiple=realtime_multiple,
            real_time_factor=realtime_multiple,
        )
    finally:
        if own_work_dir:
            shutil.rmtree(work, ignore_errors=True)


async def speak_utterance(
    ctx: Any,
    *,
    text: str,
    resolve: Callable[[], ResolvedTarget],
    markup: bool = True,
    spell_identifiers: bool = False,
    paragraph_gap_ms: int = 0,
    project: str | None = None,
    overrides: dict[str, str] | None = None,
    language: str = "Auto",
    seed: int | None = None,
    speed: float = 1.0,
    response_format: str = "wav",
    take: int | None = None,
    markup_speed: bool = True,
    work_dir: Path | None = None,
    script: MarkupScript | None = None,
    mapping: dict[str, str] | None = None,
) -> UtteranceOutcome:
    """Parse, resolve, normalize and render one utterance.

    ``resolve`` is a callable rather than an already-resolved target because the
    order is load-bearing: markup is parsed on the raw text *first*, and the
    target is resolved *before* the voice-scoped lexicon is applied. Passing the
    callable keeps that ordering in one place instead of trusting five call sites
    to do it in the same order.

    ``script`` lets a caller hand over an already-parsed utterance (the script
    renderer validates every beat before the first worker exists); ``mapping``
    does the same for the effective lexicon, which a multi-beat script memoises
    per ``(project, voice_id)``.
    """
    if script is None:
        script = parse_markup(text, markup=markup, paragraph_gap_ms=paragraph_gap_ms)
    target = resolve()
    plan = plan_utterance(
        ctx,
        script,
        target=target,
        project=project,
        spell_identifiers=spell_identifiers,
        overrides=overrides,
        mapping=mapping,
        markup_speed=markup_speed,
    )
    result = await render_utterance(
        ctx,
        plan,
        target=target,
        language=language,
        seed=seed,
        speed=speed,
        response_format=response_format,
        take=take,
        work_dir=work_dir,
    )
    return UtteranceOutcome(result=result, plan=plan)
