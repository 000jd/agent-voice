"""Durable generation history."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from ..schemas import (
    AgentVoiceError,
    DesignVoiceRequest,
    ErrorCode,
    ErrorResponse,
    HistoryItem,
    HistoryListResponse,
    JobStartedResponse,
    RerenderRequest,
    SpeakRequest,
)

router = APIRouter(tags=["history"])


@router.get("/v1/history", response_model=HistoryListResponse)
async def list_history(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    kind: str | None = None,
    voice: str | None = None,
    favorite: bool | None = None,
    q: str | None = None,
):
    ctx = request.app.state.ctx
    items, next_cursor = ctx.history.list(
        limit=limit, cursor=cursor, kind=kind, voice=voice, favorite=favorite, q=q
    )
    return HistoryListResponse(items=items, next_cursor=next_cursor)


@router.get("/v1/history/{entry_id}", response_model=HistoryItem)
async def get_history(entry_id: str, request: Request):
    return request.app.state.ctx.history.get(entry_id)


@router.delete("/v1/history/{entry_id}")
async def delete_history(entry_id: str, request: Request):
    ctx = request.app.state.ctx
    ctx.history.get(entry_id)
    ctx.history.delete(entry_id)
    return {"deleted": entry_id}


@router.post("/v1/history/{entry_id}/favorite", response_model=HistoryItem)
async def favorite_history(entry_id: str, request: Request):
    return request.app.state.ctx.history.toggle_favorite(entry_id)


@router.post("/v1/history/{entry_id}/rerender")
async def rerender_history(entry_id: str, payload: RerenderRequest, request: Request):
    ctx = request.app.state.ctx
    item = ctx.history.get(entry_id)
    if not item.rerenderable:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"history kind '{item.kind}' cannot be rerendered",
            "Regenerate from Speak, or re-transcribe from Transcribe.",
        )
    stored = ctx.history.request(entry_id)
    kind = item.kind
    job = ctx.jobs.create("rerender")

    async def runner() -> None:
        ctx.jobs.mark_running(job.job_id, f"rerendering {kind}")
        try:
            if kind == "speak":
                from ..services.history_service import record_speak
                from .routes_openai import run_synthesis

                body = SpeakRequest(**stored)
                result = await run_synthesis(
                    request,
                    text=body.text,
                    voice=body.voice,
                    engine=body.engine,
                    model=body.model,
                    quality=body.quality,
                    language=body.language,
                    response_format=body.format,
                    seed=payload.seed if payload.seed is not None else body.seed,
                    speed=body.speed,
                    take=payload.take if payload.take is not None else body.take,
                    speaker=body.speaker,
                    instruct=body.instruct,
                    pronunciations=body.pronunciations,
                    markup=body.markup,
                    spell_identifiers=body.spell_identifiers,
                    paragraph_gap_ms=body.paragraph_gap_ms,
                    project=body.project,
                )
                new_item = record_speak(ctx, result=result, text=body.text, request=body)
            elif kind == "design":
                from ..services.history_service import record_design
                from ..services.voice_design_service import render_design

                body = DesignVoiceRequest(**stored)
                if payload.take is not None:
                    body = body.model_copy(update={"take": payload.take})
                if payload.seed is not None:
                    body = body.model_copy(update={"seed": payload.seed})
                result = await render_design(ctx, body)
                new_item = record_design(ctx, result=result, request=body)
            else:
                raise AgentVoiceError(
                    ErrorCode.BAD_REQUEST,
                    f"history kind '{kind}' cannot be rerendered",
                    "Regenerate from the original screen.",
                )
            ctx.jobs.succeeded(job.job_id, new_item.model_dump(mode="json"))
        except AgentVoiceError as exc:
            ctx.jobs.failed(job.job_id, exc.to_response())
        except asyncio.CancelledError:
            ctx.jobs.update(job.job_id, status="cancelled")
            raise
        except Exception as exc:  # noqa: BLE001
            ctx.jobs.failed(
                job.job_id,
                ErrorResponse(
                    error_code=ErrorCode.INTERNAL_ERROR,
                    message=str(exc),
                    suggestion="Check the daemon logs for a traceback.",
                ),
            )

    ctx.jobs.register_task(job.job_id, asyncio.create_task(runner()))
    return JobStartedResponse(job_id=job.job_id, kind="rerender").model_dump(mode="json")
