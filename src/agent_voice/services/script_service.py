"""Script rendering, independent of any transport.

Extracted from the FastAPI route so the MCP server can call it directly with an
explicit ``agent_id`` and ``client_host`` instead of faking a Request object.
That fake was not merely ugly: it reported ``client=None``, which silently
disabled the loopback check on every mutation that went through MCP.

Each beat is rendered by :mod:`agent_voice.services.utterance_service`, the same
pipeline ``/v1/agent/speak`` uses, so markup, the scoped lexicon and code
normalization cannot behave differently inside a script. What stays here is the
part that is genuinely script-shaped: per-beat routing, ``fit_mode``, inter-beat
pauses, the stitched master, and the timing arithmetic that subtitles depend on.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

import soundfile as sf

from ..api.routing import ResolvedTarget, resolve_target
from ..audio_utils import (
    CROSSFADE_MS,
    SAMPLE_RATE,
    crossfade_stitch,
    fit_segment,
    probe_duration,
    silence,
    stitch_offsets,
    write_srt,
    write_vtt,
)
from ..markup import MarkupScript, parse_markup
from ..schemas import (
    AgentVoiceError,
    BeatSpec,
    BeatTiming,
    ErrorCode,
    ModelMode,
    ScriptRequest,
    ScriptResult,
)
from ..security import resolve_artifact_path
from ..text_norm import lexicon_mapping
from .utterance_service import speak_utterance

logger = logging.getLogger(__name__)

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
    import soundfile as sf

    try:
        return int(sf.info(str(path)).frames)
    except (RuntimeError, OSError):
        return max(0, int(round(probe_duration(path) * SAMPLE_RATE)))


def _effective(value, fallback):
    """Per-beat setting with the script-level default applied when omitted."""
    return fallback if value is None else value


async def render_script(
    ctx,
    payload: ScriptRequest,
    *,
    agent_id: str | None = None,
    on_beat: ProgressCallback | None = None,
) -> ScriptResult:
    """Render a multi-beat script into one stitched artifact.

    Each beat's spoken text passes through markup, the scoped lexicon and
    (optionally) code normalization, but the returned timings and subtitles keep
    the words as the caller wrote them — minus the tags, which are instructions,
    not content.

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

    beat_limit = ctx.settings.max_script_beats
    if len(payload.beats) > beat_limit:
        # Before routing and before the work directory: a script over the limit is
        # rejected at admission, so a runaway job never reserves GPU time or leaves
        # a scratch directory behind.
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"script has {len(payload.beats)} beats, limit is {beat_limit}",
            "Split it into smaller scripts or raise MAX_SCRIPT_BEATS.",
        )

    # Parse every beat before any worker exists. A malformed tag or an over-cap
    # beat near the end of a long script must not be discovered after the earlier
    # beats have already been rendered — and paid for. Parsing is pure, so this
    # costs nothing but a second pass over the text.
    parsed: list[MarkupScript] = [
        parse_markup(
            beat.text,
            markup=_effective(beat.markup, payload.markup),
            paragraph_gap_ms=_effective(beat.paragraph_gap_ms, payload.paragraph_gap_ms),
        )
        for beat in payload.beats
    ]

    # One lexicon per (project, resolved voice) pair. The voice layer is why this
    # cannot be hoisted before routing: an auto-selected voice brings its own
    # vocabulary, and a multi-speaker podcast has one voice per beat.
    dictionaries: dict[tuple[str | None, str | None], dict[str, str]] = {}

    # Every beat may name its own voice/engine/model — that is what makes a
    # multi-speaker podcast possible — but most scripts repeat the same tuple, so
    # resolution is memoised per tuple instead of hoisted out of the loop (which
    # silently ignored every beat after the first). speaker and instruct belong in
    # the key: two beats with the same model can still be a different timbre and a
    # different delivery.
    resolved: dict[
        tuple[str | None, str | None, str | None, str, str | None, str],
        ResolvedTarget,
    ] = {}

    def target_for(beat: BeatSpec) -> ResolvedTarget:
        key = (
            beat.voice or payload.default_voice,
            beat.engine or payload.engine,
            beat.model,
            beat.quality or payload.quality,
            beat.speaker,
            beat.instruct,
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
                speaker=key[4],
                instruct=key[5],
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
    spoken: dict[int, tuple[str | None, ModelMode, str | None, str]] = {}
    """beat index -> (voice_id, mode, speaker, instruct) that actually spoke it."""
    beat_cache: dict[int, tuple[bool, str]] = {}
    beat_display: dict[int, str] = {}
    """beat index -> text for subtitles, with tags stripped."""
    beat_had_markup: dict[int, bool] = {}
    total_beats = len(payload.beats)

    try:
        for index, beat in enumerate(payload.beats):
            target = target_for(beat)
            spell_identifiers = _effective(
                beat.spell_identifiers, payload.spell_identifiers
            )
            # A fit mode owns the duration of this beat, so a [slow]/[fast] in the
            # text must not fight the tempo change that lands it on the target.
            fitting = bool(beat.target_duration_sec) and beat.fit_mode != "none"

            lexicon_key = (payload.project, target.voice_id)
            mapping = dictionaries.get(lexicon_key)
            if mapping is None:
                mapping = lexicon_mapping(
                    ctx,
                    project=payload.project,
                    voice_id=target.voice_id,
                    overrides=payload.pronunciations,
                )
                dictionaries[lexicon_key] = mapping

            outcome = await speak_utterance(
                ctx,
                text=beat.text,
                script=parsed[index],
                resolve=lambda target=target: target,
                spell_identifiers=spell_identifiers,
                project=payload.project,
                overrides=payload.pronunciations,
                mapping=mapping,
                language=beat.language,
                speed=beat.speed,
                response_format="wav",
                take=beat.take,
                markup_speed=not fitting,
                work_dir=work_dir,
            )
            result = outcome.result
            segment = work_dir / f"beat-{index:03d}.wav"
            segment.write_bytes(Path(result.file_path).read_bytes())
            beat_cache[index] = (bool(result.cached), result.artifact_id)
            beat_display[index] = outcome.display_text
            beat_had_markup[index] = outcome.had_markup

            if fitting:
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
            spoken[index] = (
                target.voice_id,
                target.mode,
                target.speaker,
                target.instruct,
            )

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

        # Timings must describe the audio we actually produced. The crossfade
        # overlaps each seam, so summing raw segment durations would drift past the
        # real file length; stitch_offsets mirrors the stitcher's own arithmetic.
        offsets = stitch_offsets([_segment_frames(segment) for segment in segments])
        layout_end = sum(span for _onset, span in offsets)

        timings: list[BeatTiming] = []
        for position, (beat_index, is_pause) in enumerate(layout):
            if is_pause:
                continue
            beat = payload.beats[beat_index]
            start, span = offsets[position]
            cached, beat_artifact = beat_cache.get(beat_index, (False, ""))
            voice_id, beat_mode, beat_speaker, beat_instruct = spoken[beat_index]
            timings.append(
                BeatTiming(
                    index=beat_index,
                    # The display text: tags are instructions, so they are stripped
                    # here; a pronunciation rewrite is how the line is spoken, not
                    # what was scripted, so that stays in the subtitle too.
                    text=beat_display.get(beat_index, beat.text),
                    duration_sec=round(span, 4),
                    start_sec=round(start, 4),
                    end_sec=round(start + span, 4),
                    pause_after_sec=beat.pause_after_sec,
                    target_duration_sec=beat.target_duration_sec,
                    fit_mode=beat.fit_mode,
                    voice_id=voice_id,
                    mode=beat_mode,
                    speaker=beat_speaker,
                    instruct=beat_instruct,
                    had_markup=beat_had_markup.get(beat_index, False),
                    cached=cached,
                    artifact_id=beat_artifact,
                )
            )

        # Internal consistency check, deliberately not an assert (that would abort a
        # finished render over a rounding artefact). The layout — spoken parts plus
        # pauses, minus the crossfades — has to land on the end of the file we wrote.
        drift = abs(layout_end - total)
        if drift > TIMING_TOLERANCE_SEC:
            logger.warning(
                "script %s: beat layout ends at %.4fs but master.wav is %.4fs "
                "(drift %.4fs)",
                artifact_id,
                layout_end,
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
