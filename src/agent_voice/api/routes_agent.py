"""Agent-oriented endpoints: scripts (sync + async), jobs, GPU status.

Script rendering itself lives in :mod:`agent_voice.services.script_service` so the
MCP server can call it with an explicit ``agent_id`` instead of faking a Request.
This module is only the HTTP transport: parse, pick sync vs job, render.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Query, Request

from ..schemas import (
    AgentVoiceError,
    ErrorCode,
    ErrorResponse,
    GpuStatus,
    JobStartedResponse,
    ScriptRequest,
)
from ..services.script_service import render_script

router = APIRouter(tags=["agent"])


@router.post("/v1/agent/script")
async def agent_script(
    payload: ScriptRequest,
    request: Request,
    async_: bool = Query(False, alias="async"),
    legacy_async: bool = Query(False, alias="async_"),
):
    """Render a multi-beat script.

    ``?async=true`` returns a ``job_id`` immediately; poll ``GET /v1/jobs/{id}``.
    ``?async_=true`` still works: it was this parameter's de-facto name (Python
    cannot declare an argument called ``async``), and clients already use it.
    """
    ctx = request.app.state.ctx
    agent_id = request.headers.get("x-agent-id")

    if not (async_ or legacy_async):
        result = await render_script(ctx, payload, agent_id=agent_id)
        from ..services.history_service import record_script

        record_script(
            ctx,
            result=result,
            beats=[beat.model_dump(mode="json") for beat in payload.beats],
            request=payload,
        )
        return result.model_dump(mode="json")

    job = ctx.jobs.create("script")

    def on_beat(done: int, total: int, message: str) -> None:
        # Per-beat progress, so a long podcast render is observable while it runs
        # instead of appearing stuck at 0.01 until the last beat lands.
        ctx.jobs.update(job.job_id, progress=(done / total if total else 1.0), message=message)

    async def runner() -> None:
        ctx.jobs.mark_running(job.job_id)
        try:
            result = await render_script(ctx, payload, agent_id=agent_id, on_beat=on_beat)
            from ..services.history_service import record_script

            item = record_script(
                ctx,
                result=result,
                beats=[beat.model_dump(mode="json") for beat in payload.beats],
                request=payload,
            )
            ctx.jobs.succeeded(
                job.job_id, {**result.model_dump(mode="json"), "history_id": item.id}
            )
        except AgentVoiceError as exc:
            ctx.jobs.failed(job.job_id, exc.to_response())
        except asyncio.CancelledError:
            ctx.jobs.update(job.job_id, status="cancelled")
            raise
        except Exception as exc:
            ctx.jobs.failed(
                job.job_id,
                ErrorResponse(
                    error_code=ErrorCode.INTERNAL_ERROR,
                    message=str(exc),
                    suggestion="Check the daemon logs for a traceback.",
                ),
            )

    ctx.jobs.register_task(job.job_id, asyncio.create_task(runner()))
    return JobStartedResponse(job_id=job.job_id, kind="script").model_dump(mode="json")


@router.get("/v1/jobs")
async def list_jobs(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    kind: str | None = None,
    status: str | None = None,
):
    jobs = request.app.state.ctx.jobs.list(limit=limit)
    if kind:
        jobs = [job for job in jobs if job.kind == kind]
    if status:
        jobs = [job for job in jobs if job.status == status]
    return {"jobs": [job.model_dump(mode="json") for job in jobs]}


@router.get("/v1/jobs/{job_id}")
async def get_job(job_id: str, request: Request):
    return request.app.state.ctx.jobs.get(job_id).model_dump(mode="json")


@router.post("/v1/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    return request.app.state.ctx.jobs.cancel(job_id).model_dump(mode="json")


@router.get("/v1/gpu")
async def gpu_status(request: Request):
    ctx = request.app.state.ctx
    status = await ctx.workers.gpu_status()
    return GpuStatus(**status).model_dump(mode="json")
