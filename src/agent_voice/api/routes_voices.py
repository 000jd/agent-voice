"""Voice management endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request

from ..schemas import (
    AgentVoiceError,
    CloneVoiceRequest,
    DesignVoiceRequest,
    ErrorCode,
    VoiceListResponse,
)
from ..security import ensure_within_base
from ..services.voice_design_service import render_design

router = APIRouter(tags=["voices"])


def _confined_path(request: Request, raw_path: str) -> Path:
    """Resolve a caller-supplied path, respecting MCP_BASE_PATH when set."""
    ctx = request.app.state.ctx
    base = ctx.settings.mcp_base_path
    if base:
        return ensure_within_base(raw_path, base)
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"relative ref_audio_path is ambiguous: {raw_path}",
            "Pass an absolute path, or set MCP_BASE_PATH and use a relative one.",
        )
    if not candidate.is_file():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"ref_audio_path not found: {raw_path}",
            "Check the path exists and is readable by the daemon.",
        )
    return candidate


async def create_voice_from_payload(
    payload: dict, request: Request, *, overwrite: bool = False
) -> dict:
    """Create a voice from a raw payload dict.

    ``overwrite`` is not part of ``CloneVoiceRequest`` (a frozen contract), so it
    is read here instead: either from the raw payload dict (``/v1/clone`` takes a
    free-form body) or from the caller's explicit argument (the query parameter on
    ``POST /v1/voices``). Defaulting to False keeps creation idempotent rather than
    silently replacing a voice a running job may still reference.
    """
    if "overwrite" in payload:
        overwrite = bool(payload["overwrite"])
    request_model = CloneVoiceRequest(**payload)
    ctx = request.app.state.ctx
    source = _confined_path(request, request_model.ref_audio_path)
    metadata = ctx.voices.add(
        name=request_model.name,
        source_path=source,
        ref_text=request_model.ref_text,
        x_vector_only=request_model.x_vector_only,
        overwrite=overwrite,
    )
    return metadata.model_dump(mode="json")


@router.post("/v1/voices")
async def create_voice(payload: CloneVoiceRequest, request: Request, overwrite: bool = False):
    """Create a voice.

    ``?overwrite=true`` replaces an existing voice registered under the same name
    when its reference audio differs. Without it, a retry with identical audio is
    idempotent and a differing clip is rejected. ``overwrite`` is a query parameter
    rather than a body field because ``CloneVoiceRequest`` is a frozen contract.
    """
    return await create_voice_from_payload(
        payload.model_dump(), request, overwrite=overwrite
    )


@router.post("/v1/voices/design")
async def design_voice(payload: DesignVoiceRequest, request: Request):
    """Design a voice from a natural-language description.

    No reference clip is needed: the engine renders ``sample_text`` in the
    described timbre and, with ``save_as_clone``, registers that render as a
    normal voice. The preview is an ordinary artifact, so its path is playable
    immediately and ``voice_id`` can be passed as ``voice`` to any later call.
    """
    return (
        await render_design(
            request.app.state.ctx,
            payload,
            agent_id=request.headers.get("x-agent-id"),
        )
    ).model_dump(mode="json")


@router.get("/v1/voices")
async def list_voices(request: Request):
    voices = request.app.state.ctx.voices.list()
    return VoiceListResponse(voices=voices).model_dump(mode="json")


@router.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str, request: Request):
    ctx = request.app.state.ctx
    if not ctx.voices.delete(voice_id):
        raise AgentVoiceError(
            ErrorCode.VOICE_NOT_FOUND,
            f"no such voice: {voice_id}",
            "Call list_voices() to see available ids.",
        )
    return {"deleted": voice_id}
