"""FastAPI application factory.

Three integration details are easy to get wrong and all of them are load-bearing:

1. **Lifespan composition.** FastMCP's StreamableHTTP session manager is created
   in ``mcp_app.lifespan``. If that lifespan is not composed into the parent app,
   the first request to ``/mcp/`` fails with::

       FastMCP's StreamableHTTPSessionManager task group was not initialized.

   So the parent lifespan is merged with ``combine_lifespans`` — never replaced.

2. **Auth placement.** ``get_http_headers()`` inside an MCP tool withholds
   ``authorization`` by default, so Bearer auth cannot live in tool code. It is
   enforced by middleware on the parent app, which Starlette applies to mounted
   sub-apps too.

3. **Both HTTP and WebSocket.** ``@app.middleware("http")`` only sees HTTP
   scopes, so Host/Origin and Bearer checks live in the pure ASGI
   :class:`~agent_voice.api.guard.GuardMiddleware`, which handles both. A
   WebSocket is refused before ``accept()`` with close code 1008.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..bindings import BindingStore
from ..cache import SynthesisCache
from ..capture_store import CaptureStore
from ..config import Settings, get_settings
from ..engine_registry import EngineRegistry
from ..event_bus import EventBus
from ..history_store import HistoryStore
from ..job_store import JobStore
from ..pronunciations import PronunciationStore, ScopedLexicon
from ..schemas import (
    PROTOCOL_VERSION,
    AgentVoiceError,
    ErrorCode,
    ErrorResponse,
)
from ..security import is_loopback_client
from ..voice_drafts import VoiceDraftStore
from ..voice_store import VoiceStore
from ..worker_manager import WorkerManager
from .guard import (
    DEFAULT_ALLOWED_HOSTS,
    GuardMiddleware,
    allowed_host_names,
    is_loopback_bind,
)

logger = logging.getLogger(__name__)

ARTIFACT_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
"""Exactly what the daemon generates: 16 lowercase hex chars (``uuid4().hex[:16]``)."""

ARTIFACT_TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+")
"""Alphanumeric only: a plausible-but-wrong id rather than filesystem syntax.

Distinguishes the two failures a client can make: ``doesnotexist`` is simply an
unknown artifact (404, same as an expired one), while ``*`` or ``../etc/passwd``
is malformed input (400) that must never reach the filesystem at all.
"""

ARTIFACT_MEDIA_TYPES: dict[str, str] = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".pcm": "application/octet-stream",
    # A dub lands beside the audio artifacts, so GET /audio/<id> serves it too.
    ".mp4": "video/mp4",
}
"""Known artifact containers. A fixed extension list replaces v0.1's
``glob(f"{artifact_id}.*")``: a glob reads ``*`` and ``?`` in the id as pattern
syntax, so ``/audio/*`` enumerated the artifacts directory and served an
arbitrary file to an unauthenticated caller.

Deliberately audio/video only: ``.json``/``.srt``/``.vtt`` are transcripts, and
those are returned to the caller as paths rather than published to this endpoint.
"""


class AppContext:
    """Everything a route module needs, created once per app."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.registry = EngineRegistry(settings.engines_dir)
        # A typo in AGENT_VOICE_DEVICES must fail at startup, not on the first
        # request; the registry is available here and the check is torch-free.
        from ..hardware import validate_device_preferences
        from ..ingest import configure_ingest

        validate_device_preferences(settings, self.registry)
        configure_ingest(settings.ingest_concurrency)
        self.cache = SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb)
        self.voices = VoiceStore(
            settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars
        )
        self.bindings = BindingStore(settings.bindings_path)
        self.pronunciations = PronunciationStore(settings.pronunciations_path)
        # The same global file plus the project and voice scopes. Routes that need
        # the merged map (and its protected spans) use this; the plain store stays
        # for the single-scope history and the CLI-facing bits.
        self.lexicon = ScopedLexicon(
            settings.data_dir,
            settings.voices_dir,
            global_path=settings.pronunciations_path,
        )
        self.events = EventBus()
        self.drafts = VoiceDraftStore(
            settings.drafts_dir,
            ttl_seconds=settings.voice_draft_ttl_seconds,
            max_count=settings.voice_draft_max_count,
            max_bytes=settings.voice_draft_max_bytes,
            quota_bytes=settings.voice_draft_quota_bytes,
        )
        self.history = HistoryStore(settings.history_dir, max_items=settings.history_max_items)
        self.captures = CaptureStore(
            settings.captures_dir,
            max_items=settings.capture_max_items,
            max_total_bytes=settings.capture_max_total_bytes,
            max_transcript_chars=settings.capture_max_transcript_chars,
        )
        self.jobs = JobStore(
            settings.jobs_dir,
            on_change=lambda job: self.events.publish(
                "job", {"job": job.model_dump(mode="json")}
            ),
        )
        self.workers = WorkerManager(
            settings=settings,
            registry=self.registry,
            cache=self.cache,
            voices=self.voices,
        )

    def prune_retention(self) -> dict[str, int]:
        """Prune all bounded stores and unreferenced old artifacts."""
        from ..artifact_store import prune_artifacts

        self.jobs.prune()
        self.drafts.prune()
        self.captures.prune()
        self.history.prune()
        removed = prune_artifacts(
            self.settings.artifacts_dir,
            referenced_ids=self.history.artifact_ids(),
            ttl_seconds=self.settings.artifact_ttl_seconds,
        )
        return {"artifacts": removed}


def _torch_loaded() -> bool:
    import sys

    return "torch" in sys.modules


def _refuse_unsafe_bind(settings: Settings) -> None:
    """Refuse to serve a non-loopback interface with no authentication.

    Checked in ``create_app`` rather than only in the CLI so every entry point is
    covered, including ``uvicorn --factory agent_voice.api.app:create_app`` and
    the MCP shim spawning the daemon. An unauthenticated voice server on a LAN
    address is an open TTS/cloning relay for anyone who can reach the port.

    "No authentication" means the *stripped* key is empty: ``AGENT_VOICE_API_KEY=" "``
    is whitespace, not a secret, and must not unlock a public bind.
    """
    api_key = (settings.api_key or "").strip()
    if api_key or is_loopback_bind(settings.host):
        return
    raise AgentVoiceError(
        ErrorCode.BAD_REQUEST,
        f"Refusing to bind {settings.host or 'all interfaces'} without "
        "AGENT_VOICE_API_KEY. Set a key or bind 127.0.0.1.",
        "Set AGENT_VOICE_API_KEY=<secret> (every /v1 and /audio request will then "
        "need `Authorization: Bearer <secret>`), or set AGENT_VOICE_HOST=127.0.0.1.",
    )


# ---------------------------------------------------------------------------
# Mutation audit log
# ---------------------------------------------------------------------------


def record_mutation(
    ctx: AppContext,
    *,
    agent_id: str | None,
    action: str,
    engine_id: str | None = None,
    model_alias: str | None = None,
    worker_path: str | Path | None = None,
) -> None:
    """Append one JSON line to ``<data_dir>/mutations.log``.

    Registering an engine writes code to disk and later executes it on the host,
    so who asked for what must be durably recorded — one append-only line per
    mutation.

    Best-effort on purpose: a read-only or full data directory must not turn a
    mutation that already succeeded into a 500 the agent will retry.
    """
    worker_sha256: str | None = None
    if worker_path is not None:
        try:
            path = Path(worker_path)
            if path.is_file():
                worker_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            worker_sha256 = None

    line = {
        "ts": datetime.now(UTC).isoformat(),
        "agent_id": agent_id,
        "action": action,
        "engine_id": engine_id,
        "model_alias": model_alias,
        "worker_sha256": worker_sha256,
    }
    try:
        log_path = Path(ctx.settings.data_dir) / "mutations.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("could not append mutation audit line for %s: %s", action, exc)


def create_app(*, settings: Settings | None = None, mount_mcp: bool = True) -> FastAPI:
    settings = settings or get_settings()
    _refuse_unsafe_bind(settings)
    ctx = AppContext(settings)

    @asynccontextmanager
    async def app_lifespan(app: FastAPI):
        await ctx.workers.start()
        app.state.ctx = ctx
        # A job left `pending` or `running` by a previous process can never
        # resume, so reconcile it to failed before anything can poll it, then
        # drop records old enough that no caller is still waiting on them.
        reconciled = ctx.jobs.reconcile_on_startup()
        pruned = ctx.jobs.prune()
        if reconciled:
            logger.info("reconciled %d interrupted job(s) to DAEMON_RESTARTED", reconciled)
        if pruned:
            logger.info("pruned %d stale job record(s)", pruned)
        ctx.prune_retention()
        ctx.workers.on_reaper_tick = ctx.prune_retention
        try:
            yield
        finally:
            ctx.workers.on_reaper_tick = None
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

    allowed_hosts = allowed_host_names(settings)

    from .body_limit import RequestBodyLimitMiddleware

    # Added before the guard so the guard remains the outermost middleware: an
    # unauthenticated oversized request must still get 401, not a body-size hint.
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.ingest_max_bytes)

    # One guard for HTTP *and* WebSocket. It rejects a forged Host/Origin and a
    # missing credential before the WebSocket handshake is accepted, and the
    # parent middleware also covers the mounted /mcp app. The key is stripped so
    # the guard and _refuse_unsafe_bind cannot disagree about "no key".
    app.add_middleware(
        GuardMiddleware,
        allowed_hosts=allowed_hosts,
        api_key=(settings.api_key or "").strip(),
    )

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

    # FastAPI's defaults answer with a bare ``{"detail": ...}``, which an agent
    # cannot branch on: unmounted paths and rejected bodies must still come back
    # in the one envelope every other failure uses. Status codes are unchanged.
    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error_code=ErrorCode.BAD_REQUEST,
                message=str(exc.detail),
                suggestion=f"{exc.status_code} for {request.url.path}; see /openapi.json "
                "for the routes this daemon serves.",
            ).model_dump(mode="json"),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "body"
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error_code=ErrorCode.BAD_REQUEST,
                message=f"{location}: {first.get('msg', 'invalid request')}",
                suggestion="Fix the field named above; see /openapi.json for the schema.",
            ).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        correlation_id = secrets.token_hex(6)
        logger.exception(
            "unhandled error cid=%s path=%s", correlation_id, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={
                **ErrorResponse(
                    error_code=ErrorCode.INTERNAL_ERROR,
                    message="internal server error",
                    suggestion=(
                        f"Retry once; if it persists, quote correlation id {correlation_id} "
                        "when reporting it."
                    ),
                ).model_dump(mode="json"),
                "correlation_id": correlation_id,
            },
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
        routes_asr,
        routes_bindings,
        routes_captures,
        routes_engines,
        routes_events,
        routes_history,
        routes_openai,
        routes_settings,
        routes_stream,
        routes_voice_drafts,
        routes_voices,
    )

    for module in (
        routes_openai,
        routes_agent,
        routes_asr,
        routes_voices,
        routes_voice_drafts,
        routes_history,
        routes_captures,
        routes_events,
        routes_settings,
        routes_engines,
        routes_bindings,
        routes_stream,
    ):
        app.include_router(module.router)

    # -- artefacts -------------------------------------------------------
    @app.get("/audio/{artifact_id}")
    async def get_audio(artifact_id: str):
        from fastapi.responses import FileResponse

        # Strict gate before any filesystem access: an id that is not exactly the
        # 16 lowercase hex chars ``uuid4().hex[:16]`` yields never names a file we
        # wrote. Attack-shaped ids (path or glob syntax) are a client error; a
        # plausible-but-unknown id is simply not found, which keeps "your id is
        # wrong" distinguishable from "that artifact expired".
        if ARTIFACT_ID_PATTERN.fullmatch(artifact_id) is None:
            if ARTIFACT_TOKEN_PATTERN.fullmatch(artifact_id):
                raise AgentVoiceError(
                    ErrorCode.VOICE_NOT_FOUND,
                    f"no artifact: {artifact_id}",
                    "Artifact ids are the 16 lowercase hex chars returned by speak().",
                )
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"invalid artifact id: {artifact_id!r}",
                "Artifact ids are the 16 lowercase hex chars returned by speak(); "
                "path and wildcard syntax is never accepted.",
            )

        artifacts_dir = app.state.ctx.settings.artifacts_dir
        # Exact-extension probe, never a glob: the id is validated above, so the
        # only remaining question is which known container it was written as.
        path = next(
            (
                candidate
                for suffix in ARTIFACT_MEDIA_TYPES
                if (candidate := artifacts_dir / f"{artifact_id}{suffix}").is_file()
            ),
            None,
        )
        if path is None:
            raise AgentVoiceError(
                ErrorCode.VOICE_NOT_FOUND,
                f"no artifact: {artifact_id}",
                "Artifacts are ephemeral; re-run speak() to regenerate.",
            )
        media_type = ARTIFACT_MEDIA_TYPES.get(path.suffix, "application/octet-stream")
        return FileResponse(path, media_type=media_type)

    if settings.ui_enabled:
        from .routes_ui import register_ui

        register_ui(app, settings)

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


__all__ = [
    "AppContext",
    "DEFAULT_ALLOWED_HOSTS",
    "create_app",
    "record_mutation",
    "require_agent_mutation",
]
