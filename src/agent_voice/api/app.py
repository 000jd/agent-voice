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

3. **Middleware order.** ``@app.middleware("http")`` handlers are applied in
   *reverse* source order, so the handler declared **last** runs **first**.
   ``host_guard`` is therefore declared after ``bearer_auth``: a forged ``Host``
   must be rejected before any credential is examined. Verified empirically
   against the pinned Starlette/FastAPI versions; if that order ever inverted,
   the guard would silently stop protecting the auth check.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import __version__
from ..bindings import BindingStore
from ..cache import SynthesisCache
from ..config import Settings, get_settings
from ..engine_registry import EngineRegistry
from ..job_store import JobStore
from ..pronunciations import PronunciationStore
from ..schemas import (
    PROTOCOL_VERSION,
    AgentVoiceError,
    ErrorCode,
    ErrorResponse,
)
from ..security import is_loopback_client
from ..voice_store import VoiceStore
from ..worker_manager import WorkerManager

logger = logging.getLogger(__name__)

PROTECTED_PREFIXES = ("/v1", "/audio")
"""/audio is protected too: an artifact holds the user's own speech, or a clone of
their voice, so it is no less sensitive than the request that produced it."""

DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
"""Loopback names in both IPv6 spellings. ``AGENT_VOICE_ALLOWED_HOSTS`` extends
this set — it never replaces it, because relaxing the default is the
DNS-rebinding hole itself.

``testserver`` (what Starlette's TestClient sends) is deliberately NOT here: a
production default should describe production, and a test harness can add it
through the same setting every other extra host uses.
"""

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
}
"""Known artifact containers. A fixed extension list replaces v0.1's
``glob(f"{artifact_id}.*")``: a glob reads ``*`` and ``?`` in the id as pattern
syntax, so ``/audio/*`` enumerated the artifacts directory and served an
arbitrary file to an unauthenticated caller."""


class AppContext:
    """Everything a route module needs, created once per app."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_dirs()
        self.registry = EngineRegistry(settings.engines_dir)
        self.cache = SynthesisCache(settings.cache_dir, max_gb=settings.max_cache_gb)
        self.voices = VoiceStore(
            settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars
        )
        self.bindings = BindingStore(settings.bindings_path)
        self.pronunciations = PronunciationStore(settings.pronunciations_path)
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


# ---------------------------------------------------------------------------
# Host / Origin validation
# ---------------------------------------------------------------------------


def _canonical_host(value: str) -> str:
    """Reduce a ``Host`` header to a comparable hostname.

    Handles ``localhost``, ``127.0.0.1:8000``, ``[::1]:8000`` and a bare
    ``[::1]``. IPv6 brackets are dropped so both spellings match one entry.
    """
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host if end == -1 else host[1:end]
    # One colon separates host from port; several mean a bare IPv6 literal.
    if host.count(":") == 1:
        return host.partition(":")[0]
    return host


def allowed_host_names(settings: Settings) -> frozenset[str]:
    """The loopback defaults plus ``AGENT_VOICE_ALLOWED_HOSTS``."""
    extra = {_canonical_host(entry) for entry in settings.allowed_hosts}
    extra.discard("")
    defaults = {_canonical_host(name) for name in DEFAULT_ALLOWED_HOSTS}
    return frozenset(defaults | extra)


def _origin_hostname(origin: str) -> str:
    """Hostname out of an ``Origin`` URL, or ``""`` when it cannot be parsed.

    An opaque origin (``null`` from a ``file://`` page, or garbage) yields ``""``
    and is rejected: defaulting to "allow" on an unparseable origin is exactly
    the mistake a DNS-rebinding guard exists to prevent.
    """
    try:
        return (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return ""


def _is_loopback_bind(host: str) -> bool:
    """True for bind addresses that cannot be reached from off-box."""
    candidate = _canonical_host(host)
    return candidate in {"localhost", "::1"} or candidate.startswith("127.")


def _refuse_unsafe_bind(settings: Settings) -> None:
    """Refuse to serve a non-loopback interface with no authentication.

    Checked in ``create_app`` rather than only in the CLI so every entry point is
    covered, including ``uvicorn --factory agent_voice.api.app:create_app`` and
    the MCP shim spawning the daemon. An unauthenticated voice server on a LAN
    address is an open TTS/cloning relay for anyone who can reach the port.
    """
    if settings.api_key or _is_loopback_bind(settings.host):
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
        ctx.workers.on_reaper_tick = ctx.jobs.prune
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
        # Constant-time: `!=` short-circuits at the first differing byte, which
        # leaks the key prefix to anyone able to time the 401s.
        #
        # Compared as UTF-8 bytes, not str: `hmac.compare_digest` raises
        # TypeError on two `str` values when either holds a non-ASCII character,
        # so `Authorization: Bearer café` used to surface as a 500 instead of a
        # plain 401. `errors="replace"` keeps a lone surrogate from re-raising.
        if not hmac.compare_digest(
            header.encode("utf-8", errors="replace"), expected.encode("utf-8")
        ):
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(
                    error_code="BAD_REQUEST",
                    message="unauthorized",
                    suggestion="Send `Authorization: Bearer $AGENT_VOICE_API_KEY`.",
                ).model_dump(mode="json"),
            )
        return await call_next(request)

    # -- DNS-rebinding guard --------------------------------------------
    # Declared AFTER bearer_auth deliberately: this Starlette applies middleware
    # in reverse source order, so host_guard wraps — and therefore runs before —
    # bearer_auth. A forged Host must never reach the credential check.
    @app.middleware("http")
    async def host_guard(request: Request, call_next):
        raw_host = request.headers.get("host", "")
        if _canonical_host(raw_host) not in allowed_hosts:
            return JSONResponse(
                status_code=421,
                content=ErrorResponse(
                    error_code=ErrorCode.BAD_REQUEST,
                    message=f"host not allowed: {raw_host or '<missing>'}",
                    suggestion=(
                        "Connect via 127.0.0.1/localhost, or add this name to "
                        "AGENT_VOICE_ALLOWED_HOSTS. A rebound DNS name can reach "
                        "this port but must not be served."
                    ),
                ).model_dump(mode="json"),
            )

        # A present Origin must also be local: a browser page on another site is
        # the other half of the rebinding attack.
        origin = request.headers.get("origin")
        if origin is not None and _origin_hostname(origin) not in allowed_hosts:
            return JSONResponse(
                status_code=403,
                content=ErrorResponse(
                    error_code=ErrorCode.BAD_REQUEST,
                    message=f"origin not allowed: {origin}",
                    suggestion=(
                        "Cross-origin browser calls are rejected. Serve the page from "
                        "127.0.0.1/localhost, or add the origin's host to "
                        "AGENT_VOICE_ALLOWED_HOSTS."
                    ),
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
    "create_app",
    "record_mutation",
    "require_agent_mutation",
]
