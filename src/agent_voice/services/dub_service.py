"""Dubbing: a two-stage pipeline split where the agent's own work sits.

The shape of this service is dictated by who can do what. Speech recognition and
audio assembly belong in the daemon (it owns ffmpeg, the GPU and the synthesis
cache); *translation* belongs to the agent, which is the only party that can read
the source language and write the target one. So a dub is:

``prepare_dub``  -> probe, extract, transcribe, pick a speaker reference, persist
``<agent translates>``
``render_dub``   -> synthesise each translated line, place it, mix, mux

Everything the second stage needs lives in ``<data>/dubs/<dub_id>/project.json``,
which is why ``prepare_dub`` can be an ordinary call and ``render_dub`` can be
retried hours later, in a different process, without re-running ASR.

Two decisions are worth stating up front because they shape the rest:

* **Admission before work.** A dub render validates every line's slot, the count
  and the media duration before the first synthesis. A bad slot discovered on line
  90 of 120 would otherwise mean paying for 89 renders and then throwing them
  away — and ``max_dub_segments`` exists for the same reason.
* **Never truncate a line.** A translation that does not fit its slot is sped up
  (bounded by ``max_atempo``) and, past that bound, *reported* as an overflow.
  Cutting the tail off a sentence produces confident nonsense; the caller can
  shorten the line, split it, or accept it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..api.routing import AsrTarget, ResolvedTarget, resolve_asr_target, resolve_target
from ..audio_utils import (
    atempo_to_duration,
    extract_asr_wav,
    extract_bed,
    mix_bed_with_voice,
    mux_video,
    place_on_timeline,
    probe_media,
    slice_clip,
    write_srt,
    write_vtt,
)
from ..schemas import (
    AgentVoiceError,
    DubOverflow,
    DubPrepareRequest,
    DubPrepareResult,
    DubRenderRequest,
    DubRenderResult,
    DubSegmentSpec,
    ErrorCode,
    TranscribeSegment,
)
from ..security import resolve_artifact_path, resolve_media_path
from .transcribe_service import transcribe_media
from .utterance_service import speak_utterance

logger = logging.getLogger(__name__)

PROJECT_FILE = "project.json"
"""The persisted dub plan: everything ``render_dub`` needs and nothing it does not."""

_DUB_ID_RE = re.compile(r"[0-9a-f]{16}")
"""A dub id is a directory name under ``dubs_dir``, so it is pattern-checked.

``uuid4().hex[:16]`` is always lowercase hex; anything else is either a typo or
an attempt to reach a path outside the dubs tree, and both get the same
"unknown dub_id" answer rather than a filesystem error.
"""

ASR_WAV = "asr.wav"
"""16 kHz mono copy of the source audio, used for ASR and the speaker reference."""

BED_WAV = "bed.wav"
"""24 kHz stereo copy of the original audio, resampled once for the final mix."""

SPEAKER_REF_WAV = "speaker_ref.wav"
"""The cleanest speech window from the source, for ``auto_clone_source``."""

MIN_REF_WINDOW_SEC = 5.0
MAX_REF_WINDOW_SEC = 10.0
"""The clone sweet spot, and the band the speaker-window search prefers.

Below ~5 s a Qwen-style clone has too little to work with and drifts; past ~10 s
the clip mostly adds prefill time. The search asks for this band first and only
then starts distinguishing candidates.
"""

SPEAKER_GAP_SEC = 0.4
"""A pause longer than this inside a candidate window means a splice or a breath.

Fewer such gaps wins, because a reference clip that contains a cut in the middle
teaches the model two acoustically different moments as one voice.
"""

EDGE_FRACTION = 0.05
"""Skip the first and last 5 % of the file: intros, outros, fades and music."""

OVERFLOW_TOLERANCE_SEC = 0.02
"""A line this close to its slot is left alone.

``atempo`` on a 20 ms overrun costs a decode pass and a quality notch for no
audible benefit; the next line's silence absorbs it.
"""

ProgressCallback = Callable[[int, int, str], None]
"""Called as (completed_lines, total_lines, message) after each rendered line."""


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write JSON via a temp file and ``os.replace``.

    ``render_dub`` may run concurrently with nothing else, but a half-written
    project.json would make an otherwise recoverable failure fatal — there is no
    second copy of the ASR result.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _load_project(work: Path, dub_id: str) -> dict[str, Any]:
    """The persisted plan, with a refusal that names the real next step."""
    if _DUB_ID_RE.fullmatch(dub_id) is None:
        # The id becomes a directory component, so anything that is not 16 hex
        # characters is refused as unknown rather than joined onto dubs_dir: a
        # traversal here would read someone else's project file.
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown dub_id: {dub_id}",
            "Call dub_prepare(media_path=...) first; it returns the dub_id (16 hex "
            "characters) and the source segments to translate.",
        )
    path = work / PROJECT_FILE
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown dub_id: {dub_id}",
            "Call dub_prepare(media_path=...) first; it returns the dub_id (16 hex "
            "characters) and the source segments to translate.",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"dub project for {dub_id} is unreadable: {exc}",
            "Re-run dub_prepare(media_path=...) to rebuild it.",
        ) from exc


def _full_segments(
    transcript_segments: list[TranscribeSegment], json_path: str | None
) -> list[TranscribeSegment]:
    """Every transcribed segment, not the inline preview.

    ``TranscribeResult.segments`` is deliberately bounded, but a dub needs the
    whole timeline: a segment missing here is a line of dialogue that never gets
    translated. The JSON artifact written by the same call holds all of them, so
    read it back rather than widening the contract.
    """
    if json_path:
        try:
            payload = json.loads(Path(json_path).read_text("utf-8"))
            raw = payload.get("segments")
            if isinstance(raw, list) and len(raw) > len(transcript_segments):
                return [TranscribeSegment(**segment) for segment in raw]
        except (OSError, ValueError, TypeError):
            logger.warning("could not read the full transcript at %s", json_path)
    return list(transcript_segments)


def _speaker_window(
    segments: list[TranscribeSegment], duration_sec: float
) -> tuple[list[TranscribeSegment], float, float] | None:
    """The cleanest 5-10 s stretch of speech, or ``None`` when there is none.

    Selection order is 5-10 s span, then fewest >0.4 s internal gaps, then most
    words. The band comes first because a clone prompt is bounded by quality, not
    by availability: a 2 s clip clones badly and a 40 s clip clones no better than
    a 10 s one while costing prefill time. Gaps come second because a clip that
    spans a splice teaches the model a voice change. Words come last, as the tie
    breaker between equally clean windows.
    """
    if duration_sec <= 0 or not segments:
        return None

    low_edge = EDGE_FRACTION * duration_sec
    high_edge = (1.0 - EDGE_FRACTION) * duration_sec
    eligible = sorted(
        (
            segment
            for segment in segments
            if segment.end_sec > segment.start_sec
            and segment.text.strip()
            and segment.start_sec >= low_edge
            and segment.end_sec <= high_edge
        ),
        key=lambda segment: segment.start_sec,
    )
    if not eligible:
        return None

    single = [
        segment
        for segment in eligible
        if MIN_REF_WINDOW_SEC <= segment.end_sec - segment.start_sec <= MAX_REF_WINDOW_SEC
    ]
    if len(single) == 1:
        # Unambiguous: one segment is already exactly what we want, so no
        # concatenation can beat it on cleanliness.
        chosen = single
    else:
        best: tuple[tuple[int, int], list[TranscribeSegment]] | None = None
        for start in range(len(eligible)):
            total = 0.0
            for end in range(start, len(eligible)):
                total += eligible[end].end_sec - eligible[end].start_sec
                if total > MAX_REF_WINDOW_SEC:
                    break
                if total < MIN_REF_WINDOW_SEC:
                    continue
                run = eligible[start : end + 1]
                gaps = sum(
                    1
                    for previous, following in zip(run, run[1:], strict=False)
                    if following.start_sec - previous.end_sec > SPEAKER_GAP_SEC
                )
                words = sum(len(segment.text.split()) for segment in run)
                key = (gaps, -words)
                if best is None or key < best[0]:
                    best = (key, run)
        if best is None:
            # Nothing in the band: a 2 s clip clones badly and a 30 s one wastes
            # prefill, so a missing reference (and the normal voice rules) is the
            # honest answer instead of a poor clip.
            return None
        chosen = best[1]

    start_sec = chosen[0].start_sec
    end_sec = chosen[-1].end_sec
    if end_sec - start_sec <= 0:
        return None
    return chosen, start_sec, end_sec


def _extract_speaker_ref(
    work: Path, segments: list[TranscribeSegment], duration_sec: float
) -> tuple[Path | None, str]:
    """Slice the speaker reference out of ``asr.wav``; never fatal.

    A prepare that produced a transcript but no usable reference clip is still a
    successful prepare: the caller can pass ``voice=`` to the render instead of
    ``auto_clone_source``. Failing here would throw away a paid transcription.
    """
    window = _speaker_window(segments, duration_sec)
    if window is None:
        logger.info("no 5-10s speech window outside the file edges; no speaker reference")
        return None, ""

    chosen, start_sec, end_sec = window
    destination = work / SPEAKER_REF_WAV
    slice_clip(
        work / ASR_WAV,
        destination,
        start_sec=start_sec,
        duration_sec=end_sec - start_sec,
    )
    text = " ".join(segment.text.strip() for segment in chosen if segment.text.strip())
    return destination, text


async def prepare_dub(ctx: Any, payload: DubPrepareRequest) -> DubPrepareResult:
    """Probe, extract, transcribe and persist everything a render will need.

    Args:
        ctx: Application context (settings, registry, workers, lexicon, voices).
        payload: The media to dub and the ASR routing/vocabulary hints.

    Returns:
        The ``dub_id``, the full source segments to translate, and — when a clean
        window exists — a speaker reference clip and its transcript for
        ``auto_clone_source``.
    """
    # S1 admission. The path policy and the probe both run before a single byte is
    # decoded: a caller outside the sandbox, a silent file or an over-long video
    # must cost nothing but an ffprobe.
    media = resolve_media_path(
        payload.media_path,
        inbox_dir=ctx.settings.inbox_dir,
        mcp_base_path=ctx.settings.mcp_base_path,
        allow_any_path=ctx.settings.allow_any_path,
        field="media_path",
    )
    info = probe_media(media)
    if not info.has_audio:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"no audio stream in {media.name}",
            "Dubbing needs a source to transcribe. Pass media with an audio track, "
            "or extract it first with `ffmpeg -i in.mp4 -vn out.wav`.",
        )
    cap = ctx.settings.max_transcribe_seconds
    if info.duration_sec <= 0:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"{media.name} has no discernible duration",
            "Check the file is not truncated or a bare header; re-export it and retry.",
        )
    if info.duration_sec > cap:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"{media.name} is {info.duration_sec:.1f}s, limit is {cap:.0f}s",
            "Dub a shorter clip, or raise MAX_TRANSCRIBE_SECONDS for a job you have "
            "planned for; the cap is the same one transcription uses.",
        )

    # S2 working directory. Created only once the media is admissible, so a bad
    # request never leaves an empty ``dubs/<id>`` behind.
    dub_id = uuid.uuid4().hex[:16]
    work = ctx.settings.dubs_dir / dub_id
    work.mkdir(parents=True, exist_ok=False)

    # S3 extraction. Two files, two purposes, and both are produced here so the
    # render stage never touches the container again: 16 kHz mono for ASR, and a
    # 24 kHz stereo bed so the final mix never resamples per line.
    extract_asr_wav(media, work / ASR_WAV)
    extract_bed(media, work / BED_WAV)

    # S4 transcribe. The extracted 16 kHz file is handed over with already_16k, so
    # the ASR stage does not decode the source a second time just to resample it.
    target: AsrTarget = resolve_asr_target(
        settings=ctx.settings,
        registry=ctx.registry,
        engine=payload.asr_engine,
        model=payload.asr_model,
    )
    transcript = await transcribe_media(
        ctx,
        audio_path=work / ASR_WAV,
        target=target,
        language=payload.source_language,
        # Word timings are not used by a dub: a line is placed by its segment, so
        # paying for per-word decode tokens would buy nothing.
        word_timestamps=False,
        hotwords=payload.hotwords,
        project=payload.project,
        already_16k=True,
    )
    segments = _full_segments(transcript.segments, transcript.transcript_json_path)

    # S5 speaker reference, best-effort.
    speaker_ref_path: Path | None = None
    speaker_ref_text = ""
    if payload.extract_speaker_ref:
        speaker_ref_path, speaker_ref_text = _extract_speaker_ref(
            work, segments, info.duration_sec
        )

    # S6 persist. This file is the contract between the two stages: the agent
    # translates ``segments`` and comes back with the same slots.
    project = {
        "dub_id": dub_id,
        "media_path": str(media),
        "duration_sec": info.duration_sec,
        "source_language": transcript.language or (payload.source_language or ""),
        "speaker_ref_path": str(speaker_ref_path) if speaker_ref_path else None,
        "speaker_ref_text": speaker_ref_text,
        "segments": [segment.model_dump() for segment in segments],
        "engine_id": transcript.engine_id,
        "model_alias": transcript.model_alias,
        "created_at": time.time(),
    }
    project_path = work / PROJECT_FILE
    _atomic_write_json(project_path, project)

    return DubPrepareResult(
        dub_id=dub_id,
        media_path=str(media),
        duration_sec=round(info.duration_sec, 4),
        source_language=project["source_language"],
        speaker_ref_path=project["speaker_ref_path"],
        speaker_ref_text=speaker_ref_text,
        segments=segments,
        engine_id=transcript.engine_id,
        model_alias=transcript.model_alias,
        project_json_path=str(project_path),
    )


def _validate_segments(
    payload: DubRenderRequest, duration_sec: float, limit: int
) -> None:
    """Reject an impossible timeline before any synthesis is paid for.

    Every check here is cheap and every failure is the caller's to fix, so doing
    them up front is the difference between a fast ``BAD_REQUEST`` and a render
    that dies on line 90 with 89 cached-but-unusable takes.
    """
    if not payload.segments:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "dub_render has no segments",
            "Translate the segments dub_prepare returned and pass at least one "
            "DubSegmentSpec.",
        )
    if len(payload.segments) > limit:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"dub_render has {len(payload.segments)} segments, limit is {limit}",
            "Render in batches, or raise MAX_DUB_SEGMENTS for a job you have "
            "planned for.",
        )

    seen: set[int] = set()
    for segment in payload.segments:
        if segment.index in seen:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"duplicate segment index {segment.index}",
                "Each DubSegmentSpec needs a unique index; reuse the index "
                "dub_prepare reported for that source segment.",
            )
        seen.add(segment.index)
        if segment.start_sec < 0 or segment.end_sec <= segment.start_sec:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"segment {segment.index} has an empty or inverted slot: "
                f"start_sec={segment.start_sec:.3f}, end_sec={segment.end_sec:.3f}",
                "A slot must satisfy 0 <= start_sec < end_sec; copy the span from "
                "the source segment you translated.",
            )
        if segment.end_sec > duration_sec + 0.25:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"segment {segment.index} ends at {segment.end_sec:.3f}s, beyond the "
                f"{duration_sec:.3f}s media",
                "Slots must fall inside the source media (0.25s of slack is "
                "allowed); check the translated timings.",
            )
        if not segment.text.strip():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"segment {segment.index} has no text",
                "Every translated line must contain something to speak. Drop the "
                "segment if the source line is silent, or fill in its translation.",
            )


def _resolve_dub_voice(ctx: Any, payload: DubRenderRequest, project: dict[str, Any]) -> str | None:
    """Decide which voice speaks the dub, if any.

    Three explicit answers, and then the normal rules. ``auto_clone_source`` is
    the interesting one: it clones the source speaker under a deterministic name
    (``dub-<dub_id>``) so a retried render reuses the voice it registered instead
    of piling up duplicates — ``VoiceStore.add`` already returns the existing
    entry for identical content, so this only has to ask.
    """
    if payload.voice:
        return payload.voice
    if payload.speaker:
        # A preset timbre: resolve_target handles the custom_voice routing, and
        # passing a voice id as well would be rejected as a contradiction.
        return None

    ref = project.get("speaker_ref_path")
    if not (payload.auto_clone_source and ref):
        # Nothing chosen: let resolve_target apply its auto-voice rules, which
        # raise VOICE_REQUIRED with an inbox-aware suggestion when there is none.
        return None

    name = f"dub-{payload.dub_id}"
    existing = ctx.voices.find_by_name(name)
    if existing is not None:
        return existing.voice_id
    metadata = ctx.voices.add(
        name=name,
        source_path=Path(ref),
        ref_text=str(project.get("speaker_ref_text") or ""),
        overwrite=False,
    )
    logger.info("dub %s cloned the source speaker as voice %s", payload.dub_id, metadata.voice_id)
    return metadata.voice_id


async def render_dub(
    ctx: Any,
    payload: DubRenderRequest,
    *,
    on_progress: ProgressCallback | None = None,
) -> DubRenderResult:
    """Synthesise the translated lines, place them on the timeline and mux.

    Args:
        ctx: Application context (settings, registry, workers, voices, bindings).
        payload: The dub id, the translated segments and the mix/subtitle options.
        on_progress: Optional hook, invoked after every rendered line. The daemon
            turns this into job progress without the service knowing about jobs.

    Returns:
        The finished media plus what was cached, what overflowed and where the
        subtitle/timing sidecars landed.
    """
    started = time.perf_counter()

    work = ctx.settings.dubs_dir / payload.dub_id
    project = _load_project(work, payload.dub_id)
    duration_sec = float(project.get("duration_sec") or 0.0)
    media = Path(str(project.get("media_path") or ""))
    if not media.is_file():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"source media for dub {payload.dub_id} is gone: {media}",
            "Re-run dub_prepare(media_path=...) with the file back in place; the "
            "transcript is kept, but the picture has to come from somewhere.",
        )

    # S1 admission: every slot, the count, and the media duration, before a single
    # synthesis. All of it is the caller's to fix, so all of it fails fast.
    _validate_segments(payload, duration_sec, ctx.settings.max_dub_segments)

    # S2 routing. Resolving the first line up front fails fast (VOICE_REQUIRED,
    # MODEL_NOT_FOUND) before the work directory fills with rendered audio.
    resolved_voice = _resolve_dub_voice(ctx, payload, project)

    # Per-line routing. Most dubs repeat one (voice, engine, model, quality,
    # speaker, instruct) for every line, so resolution is memoised per tuple
    # instead of hoisted — a multi-speaker dub needs a different target per line,
    # and hoisting would silently ignore every line after the first.
    resolved: dict[
        tuple[str | None, str | None, str | None, str, str | None, str],
        ResolvedTarget,
    ] = {}

    def target_for(segment: DubSegmentSpec) -> ResolvedTarget:
        key = (
            segment.voice or resolved_voice,
            segment.engine or payload.engine,
            segment.model or payload.model,
            payload.quality,
            segment.speaker or payload.speaker,
            segment.instruct,
        )
        target = resolved.get(key)
        if target is None:
            target = resolve_target(
                settings=ctx.settings,
                registry=ctx.registry,
                voices=ctx.voices,
                bindings=ctx.bindings,
                voice=key[0],
                engine=key[1],
                model=key[2],
                quality=key[3],  # type: ignore[arg-type]
                speaker=key[4],
                instruct=key[5],
            )
            resolved[key] = target
        return target

    representative = target_for(payload.segments[0])

    total = len(payload.segments)
    lines: list[tuple[float, Path]] = []
    overflows: list[DubOverflow] = []
    cached_lines = 0
    succeeded = False

    try:
        for position, segment in enumerate(payload.segments):
            target = target_for(segment)
            outcome = await speak_utterance(
                ctx,
                text=segment.text,
                resolve=lambda target=target: target,
                language="Auto",
                speed=1.0,
                response_format="wav",
                take=segment.take,
                work_dir=work,
                project=payload.project,
            )
            result = outcome.result
            line = work / f"line-{position:04d}.wav"
            shutil.copyfile(result.file_path, line)
            if result.cached:
                cached_lines += 1

            slot = segment.end_sec - segment.start_sec
            audio_sec = float(result.duration_sec)
            if audio_sec > slot + OVERFLOW_TOLERANCE_SEC:
                # Sped up to fit when that lands inside the caller's ceiling, and
                # reported when it cannot: a truncated sentence is worse than a
                # slightly long line, and silently stretching the slot would break
                # the lip-sync the timings exist for.
                fitted = work / f"line-{position:04d}-fit.wav"
                atempo_used, still_overflows = atempo_to_duration(
                    line,
                    fitted,
                    target_sec=slot,
                    max_atempo=payload.max_atempo,
                )
                if still_overflows:
                    overflows.append(
                        DubOverflow(
                            index=segment.index,
                            slot_sec=round(slot, 4),
                            audio_sec=round(audio_sec, 4),
                            atempo=atempo_used,
                        )
                    )
                    logger.warning(
                        "dub %s line %s still overflows after %.2fx: %.2fs in a %.2fs slot",
                        payload.dub_id,
                        segment.index,
                        atempo_used,
                        audio_sec,
                        slot,
                    )
                line = fitted

            lines.append((segment.start_sec, line))
            if on_progress is not None:
                on_progress(position + 1, total, f"rendering line {position + 1}/{total}")

        # S3 placement. An all-silence buffer of the media's length, so a slot with
        # no line is heard as silence rather than shifting everything after it.
        voice_track = place_on_timeline(
            lines, work / "voice.wav", duration_sec=duration_sec
        )

        # S4 mix. The bed only ducks when it exists; a render with
        # keep_original_bed on media whose bed failed to extract still has to work.
        track = voice_track
        bed = work / BED_WAV
        bed_ducked = False
        if payload.keep_original_bed and bed.is_file():
            track, bed_ducked = mix_bed_with_voice(
                bed, voice_track, work / "mixed.wav", duck_db=payload.bed_duck_db
            )
            if not bed_ducked:
                logger.warning(
                    "dub %s: sidechaincompress unavailable, mixed at full bed level",
                    payload.dub_id,
                )
        elif payload.keep_original_bed:
            # The bed is extracted during prepare, so this means the work dir was
            # tampered with or a retry ran on a partial directory. Say so: a
            # voice-only mix is a different deliverable, not a silent success.
            logger.warning(
                "dub %s: original bed missing, rendered voice-only", payload.dub_id
            )

        # S5 subtitles. Built from the *translated* segments: the audience reads
        # what it hears, not the source language.
        cues = [(segment.start_sec, segment.end_sec, segment.text) for segment in payload.segments]
        final = resolve_artifact_path(
            f"dub_{payload.dub_id}",
            ".mp4",
            artifacts_dir=ctx.settings.artifacts_dir,
            mcp_base_path=ctx.settings.mcp_base_path,
        )
        burn_srt: Path | None = None
        srt_path: Path | None = None
        vtt_path: Path | None = None
        if payload.burn_subtitles:
            # Written inside the work directory, before the mux, because it is an
            # input to the encode rather than a deliverable of its own.
            burn_srt = write_srt(cues, work / "dub.srt")
        if payload.export_srt:
            srt_path = write_srt(cues, final.with_suffix(".srt"))
            vtt_path = write_vtt(cues, final.with_suffix(".vtt"))

        # S6 mux. A dub changes the audio, so the video stream is copied untouched.
        copied = mux_video(
            media,
            track,
            final,
            srt=burn_srt,
            burn_subtitles=payload.burn_subtitles,
        )

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        result = DubRenderResult(
            dub_id=payload.dub_id,
            file_path=str(final),
            duration_sec=round(duration_sec, 4),
            video_stream_copied=copied,
            bed_ducked=bed_ducked,
            segments_count=total,
            cached_segments=cached_lines,
            overflow_warnings=overflows,
            srt_path=str(srt_path) if srt_path else None,
            vtt_path=str(vtt_path) if vtt_path else None,
            timing_json_path=None,
            engine_id=representative.engine_id,
            model_alias=representative.model_alias,
            elapsed_ms=elapsed_ms,
        )
        # Mirrors script_service: the timing sidecar *is* the result, plus the
        # translated slots themselves, so a caller that kept only the mp4 can
        # still place captions or effects without re-probing the media.
        timing_path = final.with_suffix(".timing.json")
        result = result.model_copy(update={"timing_json_path": str(timing_path)})
        sidecar = {
            **result.model_dump(mode="json"),
            "segments": [segment.model_dump(mode="json") for segment in payload.segments],
        }
        timing_path.write_text(json.dumps(sidecar, indent=2), encoding="utf-8")

        logger.info(
            "dub %s rendered %d lines in %dms (%d cached, %d overflow)",
            payload.dub_id,
            total,
            elapsed_ms,
            cached_lines,
            len(overflows),
        )
        succeeded = True
        return result
    finally:
        # Success means the deliverable is in the artifacts directory and every
        # intermediate here (bed, asr.wav, per-line WAVs, mixed track) has been
        # consumed. On failure the directory is kept so a retry can reuse the
        # extraction and the synthesis cache instead of paying for them again.
        if succeeded:
            shutil.rmtree(work, ignore_errors=True)
        else:
            logger.warning(
                "dub %s failed; keeping %s so a retry can reuse it", payload.dub_id, work
            )
