"""Upload/recorded sample → safe draft → quality/peaks/transcript."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..ingest import (
    EXTENSION_BY_DEMUXER,
    analyse_quality,
    compute_peaks,
    extract_wav_untrusted,
    persist_upload,
    probe_untrusted,
    sniff_demuxer,
)
from ..schemas import AgentVoiceError, ErrorCode, ErrorResponse, VoiceDraft
from .transcribe_service import transcribe_media


async def create_voice_draft(
    ctx: Any,
    *,
    upload: Any,
    language: str | None = None,
) -> VoiceDraft:
    settings = ctx.settings
    stage = Path(tempfile.mkdtemp(dir=str(settings.data_dir), prefix=".ingest-"))
    try:
        uploaded = stage / "upload.bin"
        await persist_upload(upload, uploaded, max_bytes=settings.ingest_max_bytes)
        demuxer = sniff_demuxer(uploaded)
        probe_untrusted(
            uploaded,
            max_seconds=settings.voice_draft_max_duration_seconds,
            max_streams=settings.ingest_max_streams,
            max_channels=settings.ingest_max_channels,
            max_sample_rate=settings.ingest_max_sample_rate,
            timeout=settings.ingest_probe_timeout_seconds,
        )
        canonical = stage / "audio.wav"
        extract_wav_untrusted(
            uploaded,
            canonical,
            demuxer=demuxer,
            max_seconds=settings.voice_draft_max_duration_seconds,
            max_decoded_bytes=settings.ingest_max_decoded_bytes,
            max_streams=settings.ingest_max_streams,
        )
        quality = analyse_quality(canonical, max_duration=30.0)
        if quality.duration_sec <= 0:
            raise AgentVoiceError(
                ErrorCode.INVALID_REF_AUDIO,
                "the recording has no audible duration",
                "Record at least one second of speech.",
            )
        # A too-short draft is still allowed to be opened/trimmed/retried; the
        # commit path enforces the hard minimum.
        peaks = compute_peaks(canonical)

        transcript: str | None = None
        transcript_language: str | None = None
        transcript_engine: str | None = None
        transcript_error: ErrorResponse | None = None
        try:
            from ..api.routing import resolve_asr_target

            target = resolve_asr_target(
                settings=settings,
                registry=ctx.registry,
                engine=None,
                model=None,
            )
            result = await transcribe_media(
                ctx,
                audio_path=canonical,
                target=target,
                language=language,
                word_timestamps=False,
                hotwords=None,
                project=None,
                export_srt=False,
                agent_id=None,
                already_16k=False,
                capture=False,
            )
            transcript = result.text.strip() or None
            transcript_language = result.language or None
            transcript_engine = target.engine_id
        except AgentVoiceError as exc:
            transcript_error = exc.to_response()
        except Exception as exc:  # noqa: BLE001 - ASR must never block a draft
            transcript_error = ErrorResponse(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"automatic transcript failed: {exc}",
                suggestion="Keep the draft and type the reference text yourself.",
            )

        draft = ctx.drafts.create(
            audio_wav=canonical,
            original=uploaded,
            demuxer=EXTENSION_BY_DEMUXER.get(demuxer, "bin"),
            quality=quality,
            peaks=peaks,
            suggested_transcript=transcript,
            suggested_transcript_language=transcript_language,
            suggested_transcript_engine=transcript_engine,
            suggested_transcript_error=transcript_error,
        )
        return draft
    finally:
        shutil.rmtree(stage, ignore_errors=True)
