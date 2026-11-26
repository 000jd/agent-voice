"""`agent-voice mcp`: a single-owner stdio shim that never loads models.

Agents speak stdio MCP to this process. It ensures exactly one daemon is running,
then proxies to the daemon's Streamable HTTP endpoint. That is what keeps a
stdio-only client from spawning a second GPU owner.

The lazy-start sequence is deliberately defensive:

1. ``GET /health`` — if it answers, just proxy.
2. Otherwise take ``data/daemon.lock`` so two concurrent agents cannot both
   start a daemon.
3. Spawn ``agent-voice serve`` detached.
4. Poll ``/health`` until it is ready (or fail with a useful message).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import httpx

from .config import Settings, get_settings
from .schemas import AgentVoiceError, ErrorCode

STARTUP_TIMEOUT_SECONDS = 120.0
POLL_INTERVAL_SECONDS = 0.2


def _is_healthy(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url}/health", timeout=1.0)
        return response.status_code == 200 and response.json().get("status") == "ok"
    except (httpx.HTTPError, ValueError):
        return False


def _spawn_daemon(settings: Settings) -> subprocess.Popen[bytes]:
    """Start the daemon in its own session so it survives the agent exiting."""
    logger = open(settings.data_dir / "daemon.log", "ab", buffering=0)  # noqa: SIM115
    creationflags = 0
    start_new_session = False
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        start_new_session = True

    return subprocess.Popen(
        [sys.executable, "-m", "agent_voice.cli", "serve"],
        stdout=logger,
        stderr=logger,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=start_new_session,
        shell=False,
    )


def ensure_daemon(settings: Settings) -> str:
    """Return a healthy daemon base URL, starting one under a lock if needed."""
    base_url = settings.base_url
    if _is_healthy(base_url):
        return base_url

    from filelock import FileLock

    settings.ensure_dirs()
    lock = FileLock(str(settings.daemon_lock_path), timeout=STARTUP_TIMEOUT_SECONDS)
    with lock:
        # Another agent may have started it while we waited for the lock.
        if _is_healthy(base_url):
            return base_url

        process = _spawn_daemon(settings)
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _is_healthy(base_url):
                return base_url
            if process.poll() is not None:
                raise AgentVoiceError(
                    ErrorCode.INTERNAL_ERROR,
                    f"daemon exited with code {process.returncode} during startup",
                    f"Inspect {settings.data_dir / 'daemon.log'} for the traceback.",
                )
            time.sleep(POLL_INTERVAL_SECONDS)

    raise AgentVoiceError(
        ErrorCode.TIMEOUT,
        f"daemon did not become healthy within {STARTUP_TIMEOUT_SECONDS:.0f}s",
        f"Check {settings.data_dir / 'daemon.log'}, then run `agent-voice serve` "
        "manually to see the error.",
    )


def build_proxy(settings: Settings, *, client_id: str | None):
    """Create the stdio<->HTTP MCP proxy.

    Custom headers are attached to the upstream transport so the daemon can apply
    the agent's voice binding. ``X-Agent-Id`` is convenience routing, not an
    authorization boundary; the bearer token (if any) is forwarded too.

    fastmcp 4 removed ``FastMCP.as_proxy`` in favour of ``ProxyProvider``, which
    takes a client *factory* rather than a client instance.
    """
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.providers.proxy import ProxyProvider

    headers: dict[str, str] = {}
    if client_id:
        headers["X-Agent-Id"] = client_id
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"

    # Trailing slash matters: /mcp returns 307, /mcp/ is the real endpoint.
    url = f"{settings.base_url}/mcp/"

    def factory() -> Client:
        return Client(StreamableHttpTransport(url=url, headers=headers or None))

    proxy: FastMCP = FastMCP("agent-voice-proxy")
    proxy.add_provider(ProxyProvider(factory))
    return proxy


def run_stdio_proxy() -> None:
    settings = get_settings()
    client_id = os.environ.get("AGENT_VOICE_CLIENT_ID") or None

    ensure_daemon(settings)
    proxy = build_proxy(settings, client_id=client_id)
    proxy.run(transport="stdio")


def stop_daemon(settings: Settings) -> bool:
    """Ask a running daemon to exit. Used by the CLI, never by the proxy."""
    existing = settings.data_dir / "daemon.pid"
    if not existing.is_file():
        return False
    try:
        pid = int(existing.read_text("utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    for _ in range(50):
        if not _is_healthy(settings.base_url):
            return True
        time.sleep(0.1)
    return False
