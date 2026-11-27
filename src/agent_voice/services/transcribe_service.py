"""Transcription, independent of any transport.

Both v0.5 surfaces that listen — ``/v1/agent/transcribe`` and ``dub_prepare`` —
need the same four things before a decoder sees a sample: a probe that says the
file has audio and how long it is, an admission cap, a hotword list merged with
the project's lexicon, and a 16 kHz mono extraction. Writing that once here is
what stops the dubbing path from growing a second, subtly different version of
"is this file acceptable" — the same reason ``script_service`` and
``utterance_service`` exist.

Admission comes first, deliberately: a silent screen recording or a three-hour
lecture is refused after one ``ffprobe`` and no ffmpeg decode and no worker wake.
The order inside :func:`transcribe_media` is the contract; the annotation on each
step says what it is protecting.

The full transcript goes to a JSON artifact whose name is a hash of the request,
so re-transcribing the same clip lands on the same path. The inline ``segments``
in :class:`TranscribeResult` are a bounded preview — an hour of speech can be
thousands of segments, and handing that back over MCP would drown an agent that
only wanted to know what was said.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ..api.routing import AsrTarget
from ..audio_utils import (
    extract_asr_wav,
    probe_media,
    write_srt,
    write_vtt,
)
from ..schemas import (
    AgentVoiceError,
    ErrorCode,
    TranscribeResult,
    TranscribeSegment,
    TranscribeWord,
)
from ..security import resolve_artifact_path
from ..text_norm import lexicon_mapping

logger = logging.getLogger(__name__)

TRANSCRIPT_INLINE_SEGMENTS = 40
"""How many segments ride along in the result. The rest are in the JSON artifact.

Forty is roughly a minute of speech: enough for an agent to answer "what is this
about" and to sanity-check the timings, small enough that a long transcript does
not become a megabyte of tool output.
"""


def _audio_digest(path: Path) -> str:
    """SHA-256 of the media, for the deterministic transcript artifact id."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _transcript_id(
    *,
    media_sha256: str,
    engine_id: str,
    model_alias: str,
    language: str | None,
    word_timestamps: bool,
    hotwords: list[str] | None,
) -> str:
    """A stable 16-hex id for one transcription request.

    Derived from everything that can change the answer, so the same clip with the
    same settings overwrites its own artifact instead of piling up copies — and
    asking again with word timestamps lands somewhere else, because it *is* a
    different transcript.
    """
    payload = json.dumps(
        {
            "media_sha256": media_sha256,
            "engine_id": engine_id,
            "model_alias": model_alias,
            "language": language,
            "word_timestamps": bool(word_timestamps),
            "hotwords": list(hotwords or []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _project_terms(ctx: Any, project: str | None) -> list[str]:
    """The project lexicon's terms, offered to the decoder as vocabulary bias.

    A scoped lexicon is the operator's list of words that matter to this project
    ("Kubernetes", a product name, a surname). Those are exactly the words a
    general ASR model mis-hears, so the same file that shapes pronunciation is
    the best available source of hotwords. Only the keys are used: the values are
    spoken forms for a TTS engine and would be nonsense in a decoder's bias list.
    """
    if not project:
        return []
    try:
        mapping = lexicon_mapping(ctx, project=project, voice_id=None, overrides=None)
    except (AgentVoiceError, ValueError):
        # A malformed project lexicon must not fail a transcription: hotwords are
        # an optimisation and the clip can still be decoded without them.
        logger.warning("ignoring project lexicon %r for hotwords", project, exc_info=True)
        return []
    return [term for term in mapping if term.strip()]


def _merge_hotwords(
    ctx: Any, hotwords: list[str] | None, project: str | None
) -> list[str]:
    """Caller hotwords plus the project's terms, capped by slicing.

    The caller's list is already validated by the contract (``validate_hotwords``
    caps it and drops blanks), so overflowing here is not a caller error — it is
    the merge. Slicing keeps the request legitimate instead of turning a helpful
    project lexicon into a ``BAD_REQUEST`` the caller cannot fix.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for term in list(hotwords or []) + _project_terms(ctx, project):
        cleaned = term.strip()
        # Dedupe case-insensitively while keeping the caller's spelling: a decoder
        # biases on words, not on capitalisation, so "Kubernetes" and "kubernetes"
        # are one hotword and sending both only narrows the list.
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            merged.append(cleaned)
    return merged[: ctx.settings.max_hotwords]


def _coerce_segments(raw_segments: Any) -> list[TranscribeSegment]:
    """Map the worker's reply onto the contract, tolerating missing fields.

    A backend that omits ``words`` (the normal case when word timestamps were not
    requested) or reports segments without an index must not produce a 500: the
    segment text and its span are the information the caller actually needs.
    """
    segments: list[TranscribeSegment] = []
    if not isinstance(raw_segments, list):
        return segments
    for position, raw in enumerate(raw_segments):
        if not isinstance(raw, dict):
            continue
        words = [
            TranscribeWord(
                word=str(word.get("word", "")),
                start_sec=float(word.get("start_sec") or 0.0),
                end_sec=float(word.get("end_sec") or 0.0),
            )
            for word in raw.get("words") or []
            if isinstance(word, dict)
        ]
        segments.append(
            TranscribeSegment(
                index=int(raw.get("index", position)),
                start_sec=float(raw.get("start_sec") or 0.0),
                end_sec=float(raw.get("end_sec") or 0.0),
                text=str(raw.get("text", "")),
                words=words,
            )
        )
    return segments


def _cues(segments: list[TranscribeSegment]) -> list[tuple[float, float, str]]:
    return [(segment.start_sec, segment.end_sec, segment.text) for segment in segments]


async def transcribe_media(
    ctx: Any,
    *,
    audio_path: Path,
    target: AsrTarget,
    language: str | None = None,
    word_timestamps: bool = False,
    hotwords: list[str] | None = None,
    project: str | None = None,
    export_srt: bool = False,
    agent_id: str | None = None,
    already_16k: bool = False,
) -> TranscribeResult:
    """Transcribe one local media file on ``target``'s engine.

    Args:
        ctx: Application context (settings, registry, workers, lexicon).
        audio_path: The media to transcribe. Any container ffmpeg can demux.
        target: The already-resolved ASR engine and alias.
        language: A fixed language, or None to let the engine detect it.
        word_timestamps: Ask the backend for per-word spans. Costs decode tokens,
            so it is off unless a caller needs word-level caption timing.
        hotwords: Decoder vocabulary bias. The project lexicon's terms are appended.
        project: Named scoped-lexicon project whose terms become hotwords.
        export_srt: Also write ``.srt``/``.vtt`` beside the JSON artifact.
        agent_id: Caller identity, carried through for the worker's own logging.
            Not used for routing: :func:`resolve_asr_target` already decided that.
        already_16k: The caller guarantees ``audio_path`` is already 16 kHz mono
            PCM (``dub_prepare`` hands over the ``asr.wav`` it just extracted).
            Skipping the extraction saves a full decode pass on media that can be
            an hour long; the public callers never set it.

    Returns:
        The transcript, with a bounded inline segment preview and a path to the
        full JSON.
    """
    started = time.perf_counter()

    # S1 admission. Everything that can refuse the file happens before the first
    # decode: probe, audio-stream check, duration cap. A three-hour lecture or a
    # silent screen recording must cost one ffprobe, not an ASR run.
    info = probe_media(audio_path)
    if not info.has_audio:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"no audio stream in {Path(audio_path).name}",
            "Transcription needs sound. Pass a file with an audio track, or drop "
            "the audio with e.g. `ffmpeg -i in.mp4 -vn out.wav` first.",
        )
    if info.duration_sec <= 0:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"{Path(audio_path).name} has no discernible duration",
            "Check the file is not truncated or a bare header; re-export it and retry.",
        )
    cap = ctx.settings.max_transcribe_seconds
    if info.duration_sec > cap:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"{Path(audio_path).name} is {info.duration_sec:.1f}s, limit is {cap:.0f}s",
            "Split the media into shorter clips, or raise MAX_TRANSCRIBE_SECONDS for "
            "a batch you have planned for.",
        )

    # S2 vocabulary bias. Merged after admission so an over-cap merge cannot
    # change whether the file is acceptable.
    merged_hotwords = _merge_hotwords(ctx, hotwords, project)

    tmp: Path | None = None
    try:
        if already_16k:
            asr_source = Path(audio_path)
        else:
            # S3 decode. Always re-extract, even from a WAV that is already mono:
            # one pass that normalises rate, channels and sample format removes a
            # whole class of "the clip happened to be 44.1 kHz" bug and costs
            # nothing next to the decode the backend is about to do.
            #
            # Created lazily so the already_16k path (dub_prepare, which extracted
            # its own asr.wav) leaves no scratch directory at all. The parent is
            # created here rather than assumed: a caller may point artifacts_dir at
            # a directory that has not been touched since the last cleanup.
            ctx.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(dir=str(ctx.settings.artifacts_dir), prefix=".asr-"))
            asr_source = tmp / "asr.wav"
            extract_asr_wav(audio_path, asr_source)

        # S4 the worker. Only here does GPU/CPU time get reserved.
        reply = await ctx.workers.transcribe(
            engine_id=target.engine_id,
            model_alias=target.model_alias,
            audio_path=asr_source,
            language=language,
            word_timestamps=word_timestamps,
            hotwords=merged_hotwords or None,
        )

        if not isinstance(reply, dict):
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                f"asr engine '{target.engine_id}' returned a non-object reply",
                "This is a plugin bug: the transcribe reply must be a JSON object.",
            )

        # S5 map onto the contract.
        all_segments = _coerce_segments(reply.get("segments"))
        count = len(all_segments)
        preview = all_segments[:TRANSCRIPT_INLINE_SEGMENTS]
        duration = float(reply.get("duration_sec") or info.duration_sec)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        realtime_multiple = (
            duration / (elapsed_ms / 1000.0) if duration > 0 and elapsed_ms > 0 else None
        )

        result = TranscribeResult(
            text=str(reply.get("text") or ""),
            language=str(reply.get("language") or (language or "")),
            duration_sec=round(duration, 4),
            segments_count=count,
            segments=preview,
            engine_id=target.engine_id,
            model_alias=str(reply.get("model_alias") or target.model_alias),
            backend_name=str(reply.get("backend_name") or ""),
            backend_version=str(reply.get("backend_version") or ""),
            elapsed_ms=elapsed_ms,
            realtime_multiple=realtime_multiple,
            truncated=bool(reply.get("truncated")),
            segments_omitted=max(0, count - TRANSCRIPT_INLINE_SEGMENTS),
        )

        # S6 persist. The id is derived from the request, so a retry re-uses its
        # own file rather than leaving a trail of near-identical transcripts.
        transcript_id = _transcript_id(
            media_sha256=_audio_digest(audio_path),
            engine_id=target.engine_id,
            model_alias=target.model_alias,
            language=language,
            word_timestamps=word_timestamps,
            hotwords=merged_hotwords,
        )
        json_path = resolve_artifact_path(
            transcript_id,
            ".json",
            artifacts_dir=ctx.settings.artifacts_dir,
            mcp_base_path=ctx.settings.mcp_base_path,
        )
        json_path.parent.mkdir(parents=True, exist_ok=True)
        full = {
            **result.model_dump(exclude={"segments"}),
            # Every segment, not the preview: this file is why the preview can be
            # small. Timings, not text alone, are what a dub or a caption file needs.
            "segments": [segment.model_dump() for segment in all_segments],
            "source_path": str(audio_path),
            "hotwords": merged_hotwords,
            "word_timestamps": bool(word_timestamps),
        }
        json_path.write_text(json.dumps(full, indent=2), encoding="utf-8")
        result = result.model_copy(update={"transcript_json_path": str(json_path)})

        if export_srt:
            cues = _cues(all_segments)
            srt_path = write_srt(cues, json_path.with_suffix(".srt"))
            vtt_path = write_vtt(cues, json_path.with_suffix(".vtt"))
            result = result.model_copy(
                update={"srt_path": str(srt_path), "vtt_path": str(vtt_path)}
            )

        logger.info(
            "transcribed %s on %s:%s in %dms (%d segments, %.1fx)%s",
            Path(audio_path).name,
            target.engine_id,
            target.model_alias,
            elapsed_ms,
            count,
            realtime_multiple or 0.0,
            f" for agent {agent_id}" if agent_id else "",
        )
        return result
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
