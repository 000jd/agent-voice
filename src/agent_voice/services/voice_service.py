"""Voice-library projections and draft commit helpers."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..ingest import slice_untrusted
from ..schemas import (
    AgentVoiceError,
    DraftTrim,
    ErrorCode,
    VoiceDetail,
    VoiceEngineSupport,
    VoiceMetadata,
    VoiceSummary,
)


def engine_support(ctx: Any) -> list[VoiceEngineSupport]:
    support: list[VoiceEngineSupport] = []
    for entry in ctx.registry.list_entries():
        if not entry.installed or entry.kind != "tts":
            continue
        manifest = ctx.registry.get(entry.engine_id).manifest
        if not manifest.capabilities.voice_clone:
            continue
        for model in manifest.models:
            if model.mode != "clone":
                continue
            support.append(
                VoiceEngineSupport(
                    engine_id=entry.engine_id,
                    model_alias=model.alias,
                    max_ref_samples=manifest.capabilities.max_ref_samples,
                )
            )
    return support


def summary(ctx: Any, metadata: VoiceMetadata) -> VoiceSummary:
    samples = ctx.voices.samples_for(metadata)
    support = engine_support(ctx)
    return VoiceSummary(
        voice_id=metadata.voice_id,
        name=metadata.name,
        kind=metadata.source,
        language=metadata.language,
        notes=metadata.notes,
        duration_sec=metadata.duration_sec,
        created_at=metadata.created_at,
        sample_count=len(samples),
        ref_text=metadata.ref_text,
        x_vector_only_mode=metadata.x_vector_only_mode,
        warnings=metadata.warnings,
        source=metadata.source,
        design_instruct=metadata.design_instruct,
        consent_recorded_at=metadata.consent_recorded_at,
        has_preview=bool(support),
        engine_support=support,
    )


def detail(ctx: Any, metadata: VoiceMetadata) -> VoiceDetail:
    return VoiceDetail(voice=summary(ctx, metadata), samples=ctx.voices.samples_for(metadata))


def prepare_from_draft(
    ctx: Any,
    *,
    draft_id: str,
    trim: DraftTrim | None,
) -> tuple[Path, Any]:
    """Return a canonical WAV path and its cleanup directory (if any)."""
    draft = ctx.drafts.get(draft_id)
    if trim is None:
        return ctx.drafts.audio_path(draft_id), None
    if trim.end_sec > draft.duration_sec + 0.05:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"trim end ({trim.end_sec:.2f}s) is past the draft duration "
            f"({draft.duration_sec:.2f}s)",
            "Choose an end inside the recording.",
        )
    duration = min(trim.end_sec, draft.duration_sec) - trim.start_sec
    if duration < 1.0:
        raise AgentVoiceError(
            ErrorCode.INVALID_REF_AUDIO,
            "the trimmed clip is shorter than one second",
            "Keep at least one second, preferably 3-10 seconds, of speech.",
        )
    temp = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".trim-"))
    try:
        output = temp / "trimmed.wav"
        slice_untrusted(
            ctx.drafts.audio_path(draft_id),
            output,
            start_sec=trim.start_sec,
            duration_sec=duration,
            demuxer="wav",
            max_seconds=ctx.settings.voice_draft_max_duration_seconds,
            max_decoded_bytes=ctx.settings.ingest_max_decoded_bytes,
        )
        return output, temp
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def commit_prepared(
    ctx: Any,
    *,
    name: str,
    prepared_wav: Path,
    ref_text: str,
    x_vector_only: bool,
    language: str,
    notes: str,
    consent: bool,
    overwrite: bool = False,
) -> VoiceMetadata:
    return ctx.voices.add_prepared(
        name=name,
        prepared_wav=prepared_wav,
        ref_text=ref_text,
        x_vector_only=x_vector_only,
        overwrite=overwrite,
        language=language,
        notes=notes,
        consent=consent,
    )
