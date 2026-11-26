"""Per-agent binding endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import AgentVoiceError, BindingsResponse, ErrorCode, SetBindingRequest

router = APIRouter(tags=["bindings"])

AGENT_ID_HEADER = "x-agent-id"


@router.get("/v1/agents/bindings")
async def list_bindings(request: Request):
    bindings = request.app.state.ctx.bindings.list()
    return BindingsResponse(bindings=bindings).model_dump(mode="json")


@router.put("/v1/agents/bindings")
async def set_binding(payload: SetBindingRequest, request: Request):
    """Set the binding for the calling agent.

    The agent is identified by ``X-Agent-Id``. That header is convenience routing
    on a local single-user box, not an authorization boundary.
    """
    ctx = request.app.state.ctx
    agent_id = request.headers.get(AGENT_ID_HEADER)
    if not agent_id:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "missing X-Agent-Id header",
            "Send `X-Agent-Id: <name>` to identify which agent this binding is for.",
        )
    if payload.engine_id:
        ctx.registry.get(payload.engine_id)
    if payload.voice_id and not ctx.voices.exists(payload.voice_id):
        raise AgentVoiceError(
            ErrorCode.VOICE_NOT_FOUND,
            f"no such voice: {payload.voice_id}",
            "Call list_voices() first.",
        )

    binding = ctx.bindings.set(
        agent_id,
        voice_id=payload.voice_id,
        engine_id=payload.engine_id,
        model_alias=payload.model_alias,
    )
    return binding.model_dump(mode="json")


@router.delete("/v1/agents/bindings")
async def delete_binding(request: Request):
    ctx = request.app.state.ctx
    agent_id = request.headers.get(AGENT_ID_HEADER)
    if not agent_id:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "missing X-Agent-Id header",
            "Send `X-Agent-Id: <name>`.",
        )
    deleted = ctx.bindings.delete(agent_id)
    return {"deleted": agent_id if deleted else None}
