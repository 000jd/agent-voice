"""Engine and model discovery plus agent-driven self-management.

Every mutating route here writes an audit line via :func:`record_mutation` before
returning: these endpoints write Python to disk and later execute it, which is the
highest-privilege thing the daemon can be asked to do.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess

from fastapi import APIRouter, Request

from ..engine_registry import WORKER_NAME
from ..schemas import (
    AgentVoiceError,
    EngineEntry,
    ErrorCode,
    JobStartedResponse,
    ModelEntry,
    RegisterEngineRequest,
    RegisterModelRequest,
)
from .app import record_mutation, require_agent_mutation

router = APIRouter(tags=["engines"])


@router.get("/v1/engines")
async def list_engines(request: Request) -> dict:
    ctx = request.app.state.ctx
    entries = ctx.registry.list_entries(
        loaded=ctx.workers.loaded_models(),
        devices=ctx.workers.worker_devices(),
    )
    return {"engines": [EngineEntry(**e.model_dump()).model_dump(mode="json") for e in entries]}


@router.post("/v1/engines")
async def register_engine(payload: RegisterEngineRequest, request: Request):
    ctx = request.app.state.ctx
    require_agent_mutation(ctx, request)

    record = ctx.registry.register_engine(
        engine_id=payload.engine_id,
        engine_yaml_content=payload.engine_yaml_content,
        pyproject_toml_content=payload.pyproject_toml_content,
        worker_py_content=payload.worker_py_content,
    )

    # Audited before the (optional) install job starts: if `uv sync` later runs
    # arbitrary build backends, the record of who staged the code already exists.
    record_mutation(
        ctx,
        agent_id=request.headers.get("x-agent-id"),
        action="register_engine",
        engine_id=record.engine_id,
        worker_path=record.directory / WORKER_NAME,
    )

    job_id = None
    if payload.install_venv and (record.directory / "pyproject.toml").is_file():
        job = ctx.jobs.create("install_engine")
        job_id = job.job_id
        ctx.jobs.register_task(
            job_id, asyncio.create_task(_run_install(ctx, record.engine_id, job_id))
        )

    return {
        "engine_id": record.engine_id,
        "installed": ctx.registry.get(record.engine_id).installed,
        "job_id": job_id,
    }


@router.post("/v1/engines/{engine_id}/install")
async def install_engine(engine_id: str, request: Request, device: str = "auto"):
    ctx = request.app.state.ctx
    require_agent_mutation(ctx, request)
    record = ctx.registry.get(engine_id)
    record_mutation(
        ctx,
        agent_id=request.headers.get("x-agent-id"),
        action="install_engine",
        engine_id=engine_id,
        worker_path=record.directory / WORKER_NAME,
    )
    job = ctx.jobs.create("install_engine")
    ctx.jobs.register_task(
        job.job_id,
        asyncio.create_task(_run_install(ctx, engine_id, job.job_id, device=device)),
    )
    return JobStartedResponse(job_id=job.job_id, kind="install_engine").model_dump(mode="json")


@router.get("/v1/models")
async def list_models(request: Request, engine_id: str | None = None):
    ctx = request.app.state.ctx
    models = ctx.registry.list_models(engine_id)
    loaded = ctx.workers.loaded_models()
    payload = []
    for model in models:
        entry = ModelEntry(**model.model_dump())
        entry.loaded = model.alias in loaded.get(model.engine_id, [])
        payload.append(entry.model_dump(mode="json"))
    return {"models": payload}


@router.post("/v1/models")
async def register_model(payload: RegisterModelRequest, request: Request):
    ctx = request.app.state.ctx
    require_agent_mutation(ctx, request)

    ctx.registry.register_model(
        engine_id=payload.engine_id,
        model_alias=payload.model_alias,
        hf_repo=payload.hf_repo,
        quality_tier=payload.quality_tier,
        est_vram_gb=payload.est_vram_gb,
    )

    record_mutation(
        ctx,
        agent_id=request.headers.get("x-agent-id"),
        action="register_model",
        engine_id=payload.engine_id,
        model_alias=payload.model_alias,
    )

    job_id = None
    if payload.download:
        job = ctx.jobs.create("download_model")
        job_id = job.job_id
        ctx.jobs.register_task(
            job_id,
            asyncio.create_task(
                _run_download(ctx, payload.engine_id, payload.model_alias, job_id)
            ),
        )

    return {"engine_id": payload.engine_id, "model_alias": payload.model_alias, "job_id": job_id}


@router.post("/v1/models/{engine_id}/{model_alias}/download")
async def download_model(engine_id: str, model_alias: str, request: Request):
    ctx = request.app.state.ctx
    require_agent_mutation(ctx, request)
    ctx.registry.model_spec(engine_id, model_alias)
    record_mutation(
        ctx,
        agent_id=request.headers.get("x-agent-id"),
        action="download_model",
        engine_id=engine_id,
        model_alias=model_alias,
    )
    job = ctx.jobs.create("download_model")
    ctx.jobs.register_task(
        job.job_id, asyncio.create_task(_run_download(ctx, engine_id, model_alias, job.job_id))
    )
    return JobStartedResponse(job_id=job.job_id, kind="download_model").model_dump(mode="json")


@router.post("/v1/models/unload")
async def unload_models(request: Request, force: bool = False):
    ctx = request.app.state.ctx
    result = await ctx.workers.unload_models(force=force)
    return result


# ---------------------------------------------------------------------------
# Background workers for install / download
# ---------------------------------------------------------------------------


async def _run_install(ctx, engine_id: str, job_id: str, *, device: str = "auto") -> None:
    """Install one engine for one device, as a job.

    The exact argv/env/project resolution lives in
    :func:`setup_wizard.plan_engine_install`, the same function the CLI's
    ``engines install`` uses — a device is a dependency set, so the two surfaces
    must not drift.
    """
    suffix = "" if device == "auto" else f" for {device}"
    ctx.jobs.mark_running(job_id, f"installing {engine_id}{suffix}")
    try:
        from ..engine_registry import clear_torch_probe_cache
        from ..hardware import describe_torch
        from ..setup_wizard import plan_engine_install, write_variant_stamp

        record = ctx.registry.get(engine_id)
        uv = shutil.which("uv")
        # `--experimental` is deliberately not reachable from the API: an
        # unproven device is an operator decision, made at a terminal.
        plan = plan_engine_install(
            engine_id=engine_id,
            directory=record.directory,
            manifest=record.manifest,
            device=device,
            allow_experimental=False,
            uv=uv or "uv",
        )

        if not plan.installable:
            # Dependency-free engines (like mock) need no install step.
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                plan.problem or f"engine '{engine_id}' has no pyproject.toml",
                "Dependency-free engines (like mock) need no install step."
                if not plan.problem
                else "Pick a device the engine publishes a variant project for.",
            )

        if uv is None:
            raise AgentVoiceError(
                ErrorCode.INTERNAL_ERROR,
                "uv is not on PATH",
                "Install uv (https://docs.astral.sh/uv/) to manage engine venvs.",
            )

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                list(plan.argv),
                env=plan.env,
                capture_output=True,
                timeout=3600,
                shell=False,
                cwd=str(plan.project_dir),
            ),
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", "replace")[-1500:]
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_INSTALLED,
                f"uv sync failed for '{engine_id}' ({plan.variant})",
                "Inspect the engine pyproject.toml; the full error is in the job result.",
                details={"stderr_tail": stderr, "argv": list(plan.argv), "device": plan.device},
            )

        # Switching a device swaps the venv's torch build, and the registry caches
        # one probe per interpreter; drop it before reload or the old build wins.
        clear_torch_probe_cache()
        ctx.registry.reload()
        refreshed = ctx.registry.get(engine_id)
        if not refreshed.installed:
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_INSTALLED,
                f"'{engine_id}' still reports uninstalled after sync",
                refreshed.why_unavailable or "Check the venv path in engine.yaml.",
            )
        try:
            write_variant_stamp(
                refreshed.directory, plan.device, describe_torch(refreshed.torch_info)
            )
        except OSError:
            pass  # metadata only: a read-only engines dir must not fail the job

        ctx.jobs.succeeded(
            job_id,
            {
                "engine_id": engine_id,
                "installed": True,
                "device": plan.device,
                "level": plan.level,
                "variant": plan.variant,
            },
        )
    except AgentVoiceError as exc:
        ctx.jobs.failed(job_id, exc.to_response())
    except Exception as exc:
        from ..schemas import ErrorResponse

        ctx.jobs.failed(
            job_id,
            ErrorResponse(error_code=ErrorCode.INTERNAL_ERROR, message=str(exc)),
        )


async def _run_download(ctx, engine_id: str, model_alias: str, job_id: str) -> None:
    ctx.jobs.mark_running(job_id, f"downloading {engine_id}:{model_alias}")
    try:
        spec = ctx.registry.model_spec(engine_id, model_alias)

        def _download() -> None:
            from huggingface_hub import snapshot_download

            snapshot_download(repo_id=spec.hf_repo, repo_type="model")

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _download)
        ctx.registry.reload()

        from ..engine_registry import hf_model_downloaded

        ctx.jobs.succeeded(
            job_id,
            {
                "engine_id": engine_id,
                "model_alias": model_alias,
                "hf_repo": spec.hf_repo,
                "downloaded_locally": hf_model_downloaded(spec.hf_repo),
            },
        )
    except AgentVoiceError as exc:
        ctx.jobs.failed(job_id, exc.to_response())
    except Exception as exc:
        from ..schemas import ErrorResponse

        ctx.jobs.failed(
            job_id,
            ErrorResponse(
                error_code=ErrorCode.INTERNAL_ERROR,
                message=f"download failed: {exc}",
                suggestion="Check network access to huggingface.co.",
            ),
        )
