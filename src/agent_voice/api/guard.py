"""Pure ASGI Host/Origin and Bearer guard for HTTP *and* WebSocket scopes.

Starlette's ``@app.middleware("http")`` only sees HTTP scopes; a WebSocket
handshake bypasses both the DNS-rebinding guard and Bearer auth. This middleware
runs for both scope types and rejects an unauthorized WebSocket with close code
1008 *before* ``accept()``, so a page on another origin can never attach.
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Any
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from ..schemas import ErrorCode, ErrorResponse
from ..settings_types import SettingsLike

DEFAULT_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})
"""Loopback names in both IPv6 spellings. ``AGENT_VOICE_ALLOWED_HOSTS`` extends
this set — it never replaces it, because relaxing the default is the
DNS-rebinding hole itself.

``testserver`` (what Starlette's TestClient sends) is deliberately NOT here: a
production default should describe production, and a test harness can add it
through the same setting every other extra host uses.
"""

PROTECTED_PREFIXES = ("/v1", "/audio", "/docs", "/redoc", "/openapi")
"""/audio is protected too: an artifact holds the user's own speech, or a clone of
their voice, so it is no less sensitive than the request that produced it. The
generated API docs describe every route and are protected when a key is set; the
UI does not need them."""

AUTH_EXEMPT_PATHS = frozenset({"/health"})
AUTH_EXEMPT_PREFIXES: tuple[str, ...] = ()

WS_POLICY_VIOLATION = 1008
"""RFC 6455 policy violation: the handshake was refused before accept."""


def _strip_root_dot(host: str) -> str:
    """Drop one trailing root-label dot: ``localhost.`` is ``localhost``.

    DNS treats ``localhost.`` as the fully-qualified spelling of the same name, so
    refusing one while accepting the other is an inconsistency no resolver makes —
    and a client that sends the FQDN form would be locked out of its own daemon.
    Exactly one dot is removed: ``..`` is not a hostname.
    """
    return host[:-1] if host.endswith(".") else host


def _canonical_host(value: str) -> str:
    """Reduce a ``Host`` header to a comparable hostname.

    Handles ``localhost``, ``localhost.``, ``127.0.0.1:8000``, ``[::1]:8000`` and
    a bare ``[::1]``. IPv6 brackets are dropped so both spellings match one entry,
    and a single trailing root dot is discarded.
    """
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            host = host[1:end]
    # One colon separates host from port; several mean a bare IPv6 literal.
    elif host.count(":") == 1:
        host = host.partition(":")[0]
    return _strip_root_dot(host)


def allowed_host_names(settings: SettingsLike) -> frozenset[str]:
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

    A trailing root dot is stripped, so ``http://localhost.:8000`` matches the
    same allow-list entry as ``http://localhost:8000``.
    """
    try:
        return _strip_root_dot((urlsplit(origin).hostname or "").lower())
    except ValueError:
        return ""


def is_loopback_bind(host: str) -> bool:
    """True for bind addresses that cannot be reached from off-box.

    Only an IP literal that ``ipaddress`` recognises as loopback (``127.0.0.0/8``
    or ``::1``) and the name ``localhost`` qualify. A string that merely *looks*
    address-shaped does not: ``127.evil.example`` is an ordinary DNS name, and
    treating it as loopback let a no-key daemon bind it as if it were local.
    """
    candidate = _canonical_host(host)
    if not candidate:
        return False
    if candidate == "localhost":
        return True
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        # Not an IP literal: a hostname is loopback only by the exact name above.
        return False
    return address.is_loopback


def _bearer_matches(header: str, api_key: str) -> bool:
    """Constant-time comparison over UTF-8 bytes.

    ``hmac.compare_digest`` raises ``TypeError`` on two ``str`` values when either
    holds a non-ASCII character, so a non-ASCII header used to surface as a 500.
    Byte comparison returns False (→ 401) and is still constant-time.
    """
    expected = f"Bearer {api_key}"
    return hmac.compare_digest(
        header.encode("utf-8", errors="replace"), expected.encode("utf-8")
    )


def _ws_credential(headers: Headers) -> tuple[str, str]:
    """The bearer credential and the subprotocol spelling it arrived in.

    Browsers cannot set ``Authorization`` on a WebSocket, so the accepted token
    transport is ``Sec-WebSocket-Protocol: bearer, <key>``. The second element of
    the result is the client's *own* spelling of the ``bearer`` token (``""`` when
    the credential came from ``Authorization``): RFC 6455 requires the server to
    echo one of the protocols the client offered, so a client that sent ``Bearer``
    must get ``Bearer`` back, not our lowercase spelling.
    """
    header = headers.get("authorization", "")
    if header:
        return header, ""

    offered = headers.get("sec-websocket-protocol", "")
    parts = [part.strip() for part in offered.split(",") if part.strip()]
    for index, part in enumerate(parts):
        if part.lower() == "bearer" and index + 1 < len(parts):
            return f"Bearer {parts[index + 1]}", part
    return "", ""


def _error_response(
    status_code: int, message: str, suggestion: str
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            error_code=ErrorCode.BAD_REQUEST,
            message=message,
            suggestion=suggestion,
        ).model_dump(mode="json"),
    )


class GuardMiddleware:
    """Host/Origin + Bearer guard applied to HTTP and WebSocket scopes."""

    def __init__(
        self,
        app: Any,
        *,
        allowed_hosts: frozenset[str],
        api_key: str,
    ) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts
        self.api_key = api_key

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        raw_host = headers.get("host", "")
        if _canonical_host(raw_host) not in self.allowed_hosts:
            await self._reject(
                scope,
                receive,
                send,
                http_status=421,
                message=f"host not allowed: {raw_host or '<missing>'}",
                suggestion=(
                    "Connect via 127.0.0.1/localhost, or add this name to "
                    "AGENT_VOICE_ALLOWED_HOSTS. A rebound DNS name can reach "
                    "this port but must not be served."
                ),
            )
            return

        origin = headers.get("origin")
        if origin is not None and _origin_hostname(origin) not in self.allowed_hosts:
            await self._reject(
                scope,
                receive,
                send,
                http_status=403,
                message=f"origin not allowed: {origin}",
                suggestion=(
                    "Cross-origin browser calls are rejected. Serve the page from "
                    "127.0.0.1/localhost, or add the origin's host to "
                    "AGENT_VOICE_ALLOWED_HOSTS."
                ),
            )
            return

        if not self.api_key:
            await self.app(scope, receive, send)
            return

        if scope["type"] == "http":
            path = scope.get("path", "")
            if path in AUTH_EXEMPT_PATHS or path.startswith(AUTH_EXEMPT_PREFIXES):
                await self.app(scope, receive, send)
                return
            if not path.startswith(PROTECTED_PREFIXES) and not path.startswith("/mcp"):
                await self.app(scope, receive, send)
                return
            if not _bearer_matches(headers.get("authorization", ""), self.api_key):
                await self._reject(
                    scope,
                    receive,
                    send,
                    http_status=401,
                    message="unauthorized",
                    suggestion="Send `Authorization: Bearer $AGENT_VOICE_API_KEY`.",
                )
                return
            await self.app(scope, receive, send)
            return

        token, subprotocol = _ws_credential(headers)
        if not _bearer_matches(token, self.api_key):
            await self._reject(
                scope,
                receive,
                send,
                http_status=401,
                message="unauthorized",
                suggestion=(
                    "Send `Authorization: Bearer $AGENT_VOICE_API_KEY` or offer "
                    "`Sec-WebSocket-Protocol: bearer, <key>`."
                ),
            )
            return

        if not subprotocol:
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "websocket.accept":
                # The browser offered "bearer" in its protocol list; selecting it
                # is what lets a JS WebSocket client complete the handshake. Echo
                # the client's spelling verbatim (RFC 6455 §4.1: the server must
                # pick one of the offered protocol strings).
                message = {**message, "subprotocol": subprotocol}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _reject(
        self,
        scope: dict,
        receive: Any,
        send: Any,
        *,
        http_status: int,
        message: str,
        suggestion: str,
    ) -> None:
        if scope["type"] == "websocket":
            # A WebSocket must be refused before accept(); consuming connect and
            # sending a close frame yields a clean 1008 to the client.
            #
            # What the wire actually shows depends on the server. A real uvicorn
            # (and any compliant ASGI server) has not sent 101 yet, so this
            # ``websocket.close`` in the CONNECTING state *is* a failed handshake:
            # uvicorn answers the HTTP upgrade request with **403 Forbidden** and
            # the browser's WebSocket sees an error. Starlette's TestClient raises
            # ``WebSocketDisconnect(1008)`` for the same ASGI sequence. Both are
            # the same refusal — the security property is "never accepted", and it
            # is asserted at the ASGI level in test_security_hardening.py.
            try:
                await receive()
            except Exception:
                return
            await send(
                {
                    "type": "websocket.close",
                    "code": WS_POLICY_VIOLATION,
                    "reason": message[:120],
                }
            )
            return

        response = _error_response(http_status, message, suggestion)
        await response(scope, receive, send)
