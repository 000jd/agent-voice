"""Persistent transcription captures."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.background import BackgroundTask

from ..audio_utils import write_srt, write_vtt
from ..ingest import analyse_quality, compute_peaks, extract_wav_untrusted
from ..schemas import (
    AgentVoiceError,
    CaptureDetail,
    CaptureListResponse,
    CapturePatchRequest,
    ErrorCode,
)

router = APIRouter(tags=["captures"])

EXPORT_MEDIA = {
    "txt": "text/plain; charset=utf-8",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "json": "application/json",
}


@router.get("/v1/captures", response_model=CaptureListResponse)
async def list_captures(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    q: str | None = None,
):
    items, next_cursor = request.app.state.ctx.captures.list(limit=limit, cursor=cursor, q=q)
    return CaptureListResponse(items=items, next_cursor=next_cursor)


@router.get("/v1/captures/{capture_id}", response_model=CaptureDetail)
async def get_capture(capture_id: str, request: Request):
    return request.app.state.ctx.captures.detail(capture_id)


@router.patch("/v1/captures/{capture_id}", response_model=CaptureDetail)
async def patch_capture(capture_id: str, payload: CapturePatchRequest, request: Request):
    return request.app.state.ctx.captures.patch(
        capture_id, transcript=payload.transcript, title=payload.title
    )


@router.delete("/v1/captures/{capture_id}")
async def delete_capture(capture_id: str, request: Request):
    ctx = request.app.state.ctx
    ctx.captures.detail(capture_id)
    ctx.captures.delete(capture_id)
    return {"deleted": capture_id}


@router.get("/v1/captures/{capture_id}/audio")
async def capture_audio(capture_id: str, request: Request):
    ctx = request.app.state.ctx
    ctx.captures.detail(capture_id)
    path = ctx.captures.audio_path(capture_id)
    if not path.is_file():
        raise AgentVoiceError(
            ErrorCode.RECORD_NOT_FOUND,
            f"capture audio is missing: {capture_id}",
            "Re-transcribe the media.",
        )
    return FileResponse(path, media_type="audio/wav", filename=f"{capture_id}.wav")


@router.get("/v1/captures/{capture_id}/export")
async def export_capture(capture_id: str, request: Request, format: str = Query("txt")):
    if format not in EXPORT_MEDIA:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unsupported capture export format: {format}",
            "Use txt, srt, vtt or json.",
        )
    ctx = request.app.state.ctx
    detail = ctx.captures.detail(capture_id)
    if format == "txt":
        return PlainTextResponse(
            detail.transcript,
            media_type=EXPORT_MEDIA[format],
            headers={"Content-Disposition": f'attachment; filename="{capture_id}.txt"'},
        )
    if format == "json":
        return JSONResponse(
            detail.model_dump(mode="json"),
            headers={"Content-Disposition": f'attachment; filename="{capture_id}.json"'},
        )
    temp = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".capture-export-"))
    cues = [(segment.start_sec, segment.end_sec, segment.text) for segment in detail.segments]
    if format == "srt":
        path = write_srt(cues, temp / f"{capture_id}.srt")
    else:
        path = write_vtt(cues, temp / f"{capture_id}.vtt")
    return FileResponse(
        path,
        media_type=EXPORT_MEDIA[format],
        filename=path.name,
        background=BackgroundTask(lambda: shutil.rmtree(temp, ignore_errors=True)),
    )


@router.post("/v1/captures/{capture_id}/voice-draft", status_code=201)
async def capture_to_draft(capture_id: str, request: Request):
    ctx = request.app.state.ctx
    detail = ctx.captures.detail(capture_id)
    source = ctx.captures.audio_path(capture_id)
    if not source.is_file():
        raise AgentVoiceError(
            ErrorCode.RECORD_NOT_FOUND,
            f"capture audio is missing: {capture_id}",
            "Re-transcribe the media.",
        )
    temp = Path(tempfile.mkdtemp(dir=str(ctx.settings.data_dir), prefix=".capture-draft-"))
    try:
        canonical = temp / "audio.wav"
        extract_wav_untrusted(
            source,
            canonical,
            demuxer="wav",
            max_seconds=ctx.settings.voice_draft_max_duration_seconds,
            max_decoded_bytes=ctx.settings.ingest_max_decoded_bytes,
        )
        quality = analyse_quality(canonical, max_duration=30.0)
        draft = ctx.drafts.create(
            audio_wav=canonical,
            original=source,
            demuxer="wav",
            quality=quality,
            peaks=compute_peaks(canonical),
            suggested_transcript=detail.transcript,
            suggested_transcript_language=detail.language or None,
            suggested_transcript_engine=detail.engine_id or None,
            suggested_transcript_error=None,
        )
        return draft
    finally:
        shutil.rmtree(temp, ignore_errors=True)
