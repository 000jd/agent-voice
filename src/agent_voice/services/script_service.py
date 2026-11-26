"""Script rendering, independent of any transport.

Extracted from the FastAPI route so the MCP server can call it directly with an
explicit ``agent_id`` and ``client_host`` instead of faking a Request object.
That fake was not merely ugly: it reported ``client=None``, which silently
disabled the loopback check on every mutation that went through MCP.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

import soundfile as sf

from ..api.routing import resolve_target
from ..audio_utils import (
    SAMPLE_RATE,
    crossfade_stitch,
    fit_segment,
    probe_duration,
    silence,
    write_srt,
    write_vtt,
)
from ..schemas import (
    AgentVoiceError,
    BeatSpec,
    BeatTiming,
    ErrorCode,
    ScriptRequest,
    ScriptResult,
)
from ..security import resolve_artifact_path

logger = logging.getLogger(__name__)

CROSSFADE_MS = 20
"""Seam crossfade used by :func:`crossfade_stitch`; also the unit of timing drift."""

TIMING_TOLERANCE_SEC = 2e-3
"""Slack tolerated between the computed beat layout and the real master file."""

ProgressCallback = Callable[[int, int, str], None]
"""Called as (completed_beats, total_beats, message) after each rendered beat."""


def _segment_frames(path: Path) -> int:
    """Frame count of a rendered segment.

    Sample-accurate (not duration-rounded) because the crossfade shortfall is a
    frame count: approximating it would push every later onset off by a
    millisecond or two per seam.
    """
    try:
        return int(sf.info(str(path)).frames)
    except (RuntimeError, OSError):
        return max(0, int(round(probe_duration(path) * SAMPLE_RATE)))


async def render_script(
    ctx,
    payload: ScriptRequest,
    *,
    agent_id: str | None = None,
    on_beat: ProgressCallback | None = None,
) -> ScriptResult:
    """Render a multi-beat script into one stitched artifact.

    Args:
        ctx: Application context (settings, registry, workers, voices, jobs).
        payload: Beats plus their defaults.
        agent_id: Selective-routing id from ``X-Agent-Id``, if the transport has one.
        on_beat: Optional progress hook, invoked after every beat.
    """
    if not payload.beats:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST, "script has no beats", "Provide at least one beat."
        )

    # Every beat may name its own voice/engine/model — that is what makes a
    # multi-speaker podcast possible — but most scripts repeat the same tuple, so
    # resolution is memoised per tuple instead of hoisted out of the loop (which
    # silently ignored every beat after the first).
    resolved: dict[tuple[str | None, str | None, str | None, str], object] = {}

    def target_for(beat: BeatSpec):
        key = (
            beat.voice or payload.default_voice,
            beat.engine or payload.engine,
            beat.model,
            beat.quality or payload.quality,
        )
        target = resolved.get(key)
        if target is None:
            target = resolve_target(
                settings=ctx.settings,
                registry=ctx.registry,
                voices=ctx.voices,
                bindings=ctx.bindings,
                agent_id=agent_id,
                voice=key[0],
                engine=key[1],
                model=key[2],
                quality=key[3],  # type: ignore[arg-type]
            )
            resolved[key] = target
        return target

    # ScriptResult carries a single engine/model, so report the one the opening
    # beat used. Resolving it here also fails fast (VOICE_REQUIRED, ...) before any
    # work directory or worker exists.
    representative = target_for(payload.beats[0])

    artifact_id = uuid.uuid4().hex[:16]
    work_dir = ctx.settings.artifacts_dir / f".script-{artifact_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    segments: list[Path] = []
    layout: list[tuple[int, bool]] = []
    """(beat index, is_pause) per stitched segment, in playback order."""
    spoken_voice: dict[int, str | None] = {}
    beat_cache: dict[int, tuple[bool, str]] = {}
    total_beats = len(payload.beats)

    try:
        for index, beat in enumerate(payload.beats):
            target = target_for(beat)
            result = await ctx.workers.synthesize(
                engine_id=target.engine_id,
                model_alias=target.model_alias,
                text=beat.text,
                voice_id=target.voice_id,
                ref_text=target.ref_text,
                x_vector_only=target.x_vector_only,
                language=beat.language,
                speed=1.0,
                response_format="wav",
                take=beat.take,
            )
            segment = work_dir / f"beat-{index:03d}.wav"
            segment.write_bytes(Path(result.file_path).read_bytes())
            beat_cache[index] = (bool(result.cached), result.artifact_id)

            if beat.target_duration_sec and beat.fit_mode != "none":
                fitted = work_dir / f"beat-{index:03d}-fit.wav"
                fit_segment(
                    segment,
                    fitted,
                    target_duration_sec=beat.target_duration_sec,
                    fit_mode=beat.fit_mode,
                )
                segment = fitted

            segments.append(segment)
            layout.append((index, False))
            spoken_voice[index] = target.voice_id

            if beat.pause_after_sec > 0:
                # A real silence segment, so the gap exists in the master audio and
                # not merely in the reported timings. A mid-script pause is extended
                # by one crossfade so the seam it loses at the far end does not eat
                # into the pause the caller asked for: the next beat then starts at
                # exactly end_sec + pause_after_sec.
                pad = CROSSFADE_MS / 1000 if index < total_beats - 1 else 0.0
                pause = work_dir / f"beat-{index:03d}-pause.wav"
                sf.write(
                    pause,
                    silence(beat.pause_after_sec + pad, sample_rate=SAMPLE_RATE),
                    SAMPLE_RATE,
                    subtype="PCM_16",
                )
                segments.append(pause)
                layout.append((index, True))

            if on_beat is not None:
                on_beat(index + 1, total_beats, f"rendering beat {index + 1}/{total_beats}")

        master = work_dir / "master.wav"
        crossfade_stitch(segments, master, crossfade_ms=CROSSFADE_MS)
        total = probe_duration(master)

        # Timings must describe the audio we actually produced. A crossfade
        # overlaps each seam, so summing raw segment durations would drift past the
        # real file length. Mirror the stitcher's own arithmetic exactly: it removes
        # min(fade, accumulated, next) frames at each seam, and only when that is
        # more than one frame.
        counts = [_segment_frames(segment) for segment in segments]
        fade = max(1, int(SAMPLE_RATE * CROSSFADE_MS / 1000))
        losses: list[int] = []
        accumulated = counts[0]
        for count in counts[1:]:
            overlap = min(fade, accumulated, count)
            loss = overlap if overlap > 1 else 0
            losses.append(loss)
            accumulated += count - loss

        onsets: list[float] = []
        spans: list[float] = []
        cursor = 0
        for position, count in enumerate(counts):
            loss = losses[position] if position < len(losses) else 0
            span = max(0, count - loss)
            onsets.append(cursor / SAMPLE_RATE)
            spans.append(span / SAMPLE_RATE)
            cursor += span

        timings: list[BeatTiming] = []
        for position, (beat_index, is_pause) in enumerate(layout):
            if is_pause:
                continue
            beat = payload.beats[beat_index]
            start = onsets[position]
            span = spans[position]
            cached, beat_artifact = beat_cache.get(beat_index, (False, ""))
            timings.append(
                BeatTiming(
                    index=beat_index,
                    text=beat.text,
                    duration_sec=round(span, 4),
                    start_sec=round(start, 4),
                    end_sec=round(start + span, 4),
                    pause_after_sec=beat.pause_after_sec,
                    target_duration_sec=beat.target_duration_sec,
                    fit_mode=beat.fit_mode,
                    voice_id=spoken_voice[beat_index],
                    cached=cached,
                    artifact_id=beat_artifact,
                )
            )

        # Internal consistency check, deliberately not an assert (that would abort a
        # finished render over a rounding artefact). The layout — spoken parts plus
        # pauses, minus the crossfades — has to land on the end of the file we wrote.
        drift = abs(cursor / SAMPLE_RATE - total)
        if drift > TIMING_TOLERANCE_SEC:
            logger.warning(
                "script %s: beat layout ends at %.4fs but master.wav is %.4fs "
                "(drift %.4fs)",
                artifact_id,
                cursor / SAMPLE_RATE,
                total,
                drift,
            )

        final = resolve_artifact_path(
            artifact_id,
            f".{payload.format}",
            artifacts_dir=ctx.settings.artifacts_dir,
            mcp_base_path=ctx.settings.mcp_base_path,
        )
        if payload.format == "wav":
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(master.read_bytes())
        else:
            from ..audio_utils import convert_audio

            convert_audio(master, final, response_format=payload.format)

        result = ScriptResult(
            artifact_id=artifact_id,
            file_path=str(final),
            duration_sec=round(total, 4),
            beats=timings,
            engine_id=representative.engine_id,
            model_alias=representative.model_alias,
            format=payload.format,
        )

        if payload.export_srt:
            # Cues cover the spoken portion only: a pause after a beat is room for a
            # visual to land, not part of the utterance.
            cues = [(beat.start_sec, beat.end_sec, beat.text) for beat in timings]
            srt_path = write_srt(cues, final.with_suffix(".srt"))
            vtt_path = write_vtt(cues, final.with_suffix(".vtt"))
            timing_path = final.with_suffix(".timing.json")
            result = result.model_copy(
                update={
                    "srt_path": str(srt_path),
                    "vtt_path": str(vtt_path),
                    "timing_json_path": str(timing_path),
                }
            )
            timing_path.write_text(result.model_dump_json(indent=2), "utf-8")

        return result
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
