"""Admission cap for multipart upload paths.

Starlette's multipart parser caps non-file fields but not file parts, so a
declared Content-Length is checked here before the body is parsed. The staging
helper still counts actual bytes; this middleware is an early, cheap refusal for
honest clients and a guard against a giant declared body.
"""

from __future__ import annotations

from typing import Any

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from ..schemas import ErrorCode, ErrorResponse

UPLOAD_PATHS = frozenset(
    {
        "/v1/audio/transcriptions",
        "/v1/voice-drafts",
        "/v1/voices/import",
    }
)


class RequestBodyLimitMiddleware:
    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") not in UPLOAD_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        raw = headers.get("content-length")
        if raw:
            try:
                length = int(raw)
            except ValueError:
                await self._reject(scope, receive, send)
                return
            if length > self.max_bytes + 1024 * 1024:
                await self._reject(scope, receive, send)
                return
        await self.app(scope, receive, send)

    async def _reject(self, scope: dict, receive: Any, send: Any) -> None:
        response = JSONResponse(
            status_code=413,
            content=ErrorResponse(
                error_code=ErrorCode.MEDIA_TOO_LARGE,
                message=f"upload exceeds the {self.max_bytes // (1024 * 1024)} MiB limit",
                suggestion="Trim or compress the recording before uploading.",
            ).model_dump(mode="json"),
        )
        await response(scope, receive, send)
