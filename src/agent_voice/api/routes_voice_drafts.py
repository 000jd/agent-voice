"""Voice-draft upload/check/trim endpoints."""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse

from ..schemas import AgentVoiceError, ErrorCode, VoiceDraft
from ..services.voice_draft_service import create_voice_draft

router = APIRouter(tags=["voice-drafts"])


@router.post("/v1/voice-drafts", response_model=VoiceDraft, status_code=201)
async def upload_draft(
    request: Request,
    file: UploadFile = File(...),
    language: str | None = Form(None),
) -> VoiceDraft:
    ctx = request.app.state.ctx
    return await create_voice_draft(ctx, upload=file, language=language)


@router.get("/v1/voice-drafts/{draft_id}", response_model=VoiceDraft)
async def get_draft(draft_id: str, request: Request) -> VoiceDraft:
    return request.app.state.ctx.drafts.get(draft_id)


@router.get("/v1/voice-drafts/{draft_id}/audio")
async def draft_audio(draft_id: str, request: Request):
    ctx = request.app.state.ctx
    ctx.drafts.get(draft_id)
    path = ctx.drafts.audio_path(draft_id)
    if not path.is_file():
        raise AgentVoiceError(
            ErrorCode.RECORD_NOT_FOUND,
            f"draft audio is missing: {draft_id}",
            "Upload the sample again.",
        )
    return FileResponse(path, media_type="audio/wav", filename=f"{draft_id}.wav")


@router.delete("/v1/voice-drafts/{draft_id}")
async def delete_draft(draft_id: str, request: Request) -> dict[str, str]:
    ctx = request.app.state.ctx
    ctx.drafts.get(draft_id)
    if not ctx.drafts.delete(draft_id):
        raise AgentVoiceError(
            ErrorCode.RECORD_NOT_FOUND,
            f"no such draft: {draft_id}",
            "Refresh the draft state.",
        )
    return {"deleted": draft_id}
