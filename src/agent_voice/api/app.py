"""FastAPI application factory.

Two integration details are easy to get wrong and both are load-bearing:

1. **Lifespan composition.** FastMCP's StreamableHTTP session manager is created
   in ``mcp_app.lifespan``. If that lifespan is not composed into the parent app,
   the first request to ``/mcp/`` fails with::

       FastMCP's StreamableHTTPSessionManager task group was not initialized.

   So the parent lifespan is merged with ``combine_lifespans`` — never replaced.

2. **Auth placement.** ``get_http_headers()`` inside an MCP tool withholds
   ``authorization`` by default, so Bearer auth cannot live in tool code. It is
   enforced by middleware on the parent app, which Starlette applies to mounted
   sub-apps too.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..bindings import BindingStore
from ..cache import SynthesisCache
from ..config import Settings, get_settings
from ..engine_registry import EngineRegistry
from ..job_store import JobStore
from ..schemas import (
    PROTOCOL_VERSION,
    AgentVoiceError,
    ErrorCode,
    ErrorResponse,
)
from ..security import is_loopback_client
from ..voice_store import VoiceStore
from ..worker_manager import WorkerManager

PROTECTED_PREFIXES = ("/v1",)
"""Everything under /v1 requires the bearer token when one is configured."""


class AppContext:
    """Everything a route module needs, created once per app."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.registry = EngineRegistry(settings.engines_dir)
        self.cache = SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb)
        self.voices = VoiceStore(settings.voices_dir)
        self.bindings = BindingStore(settings.bindings_path)
        self.jobs = JobStore(settings.jobs_dir)
        self.workers = WorkerManager(
            settings=settings,
            registry=self.registry,
            cache=self.cache,
            voices=self.voices,
        )


def _torch_loaded() -> bool:
    import sys

    return "torch" in sys.modules


def create_app(*, settings: Settings | None = None, mount_mcp: bool = True) -> FastAPI:
    settings = settings or get_settings()
    ctx = AppContext(settings)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI):
        await ctx.workers.start()
        app.state.ctx = ctx
        try:
            yield
        finally:
            await ctx.workers.stop()

    mcp_app = None
    if mount_mcp:
        from ..mcp_server import build_mcp_server

        mcp_server = build_mcp_server(ctx)
        # path="/" because we mount at /mcp; path="/mcp" would double-nest.
        mcp_app = mcp_server.http_app(path="/")

        from fastmcp.utilities.lifespan import combine_lifespans

        lifespan = combine_lifespans(app_lifespan, mcp_app.lifespan)
    else:
        lifespan = app_lifespan

    app = FastAPI(
        title="agent-voice",
        version=__version__,
        description="Headless multi-engine voice server for AI agents",
        lifespan=lifespan,
    )
    app.state.ctx = ctx

    # -- auth middleware (must cover the mounted MCP app too) -----------
    @app.middleware("http")
    async def bearer_auth(request: Request, call_next):
        api_key = request.app.state.ctx.settings.api_key
        path = request.url.path
        if not api_key or path == "/health" or path.startswith("/docs") or path.startswith(
            "/openapi"
        ):
            return await call_next(request)
        if not path.startswith(PROTECTED_PREFIXES) and not path.startswith("/mcp"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        expected = f"Bearer {api_key}"
        if header != expected:
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error_code="BAD_REQUEST",
                    message="unauthorized",
                    suggestion="Send `Authorization: Bearer $AGENT_VOICE_API_KEY`.",
                ).model_dump(mode="json"),
            )
        return await call_next(request)

    # -- error handling --------------------------------------------------
    @app.exception_handler(AgentVoiceError)
    async def agent_voice_error_handler(request: Request, exc: AgentVoiceError):
        return JSONResponse(
            status_code=exc.http_status,
            content={
                **exc.to_response().model_dump(mode="json"),
                **({"details": exc.details} if exc.details else {}),
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code="INTERNAL_ERROR",
                message=str(exc),
                suggestion="Check the daemon logs for a traceback.",
            ).model_dump(mode="json"),
        )

    # -- health ----------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "torch_loaded": _torch_loaded(),
        }

    # -- routes ----------------------------------------------------------
    from . import (
        routes_agent,
        routes_bindings,
        routes_engines,
        routes_openai,
        routes_stream,
        routes_voices,
    )

    for module in (
        routes_openai,
        routes_agent,
        routes_voices,
        routes_engines,
        routes_bindings,
        routes_stream,
    ):
        app.include_router(module.router)

    # -- artefacts -------------------------------------------------------
    @app.get("/audio/{artifact_id}")
    async def get_audio(artifact_id: str):
        from fastapi.responses import FileResponse

        if "/" in artifact_id or "\\" in artifact_id or ".." in artifact_id:
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                "invalid artifact id",
                "Use the id returned by speak().",
            )

        artifacts_dir = app.state.ctx.settings.artifacts_dir
        matches = list(artifacts_dir.glob(f"{artifact_id}.*")) if artifacts_dir.is_dir() else []
        if not matches:
            raise AgentVoiceError(
                ErrorCode.VOICE_NOT_FOUND,
                f"no artifact: {artifact_id}",
                "Artifacts are ephemeral; re-run speak() to regenerate.",
            )
        media = {
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".opus": "audio/ogg",
            ".flac": "audio/flac",
            ".pcm": "application/octet-stream",
        }
        path = matches[0]
        return FileResponse(path, media_type=media.get(path.suffix, "application/octet-stream"))

    if mcp_app is not None:
        app.mount("/mcp", mcp_app)

    return app


def require_agent_mutation(ctx: AppContext, request: Request | None = None) -> None:
    """Gate agent-driven engine mutation behind an explicit opt-in + loopback."""
    if not ctx.settings.allow_agent_engine_mutation:
        raise AgentVoiceError(
            ErrorCode.AGENT_MUTATION_DISABLED,
            "agent-driven engine mutation is disabled",
            "Set ALLOW_AGENT_ENGINE_MUTATION=true to let agents add engines or models.",
        )
    if request is not None:
        client_host = request.client.host if request.client else None
        if not is_loopback_client(client_host):
            raise AgentVoiceError(
                ErrorCode.AGENT_MUTATION_DISABLED,
                f"engine mutation is loopback-only (client was {client_host})",
                "Run the daemon on 127.0.0.1 to use register_engine from an agent.",
            )


__all__ = ["AppContext", "create_app", "require_agent_mutation"]
