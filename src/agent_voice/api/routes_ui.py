"""Serve the built Studio SPA at /ui.

The SPA is a static client. Assets are public on the loopback origin; the API
remains bearer-protected. Host/Origin checks still run in GuardMiddleware. This
module is imported only when UI serving is enabled.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "media-src 'self' blob:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def web_dir(settings) -> Path:
    return Path(settings.web_dir)


def web_available(settings) -> bool:
    return (web_dir(settings) / "200.html").is_file()


def _safe_path(root: Path, relative: str) -> Path | None:
    if not relative or "\\" in relative or "\x00" in relative:
        return None
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"..", ""} for part in posix.parts):
        return None
    candidate = (root / Path(*posix.parts)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _serve(path: Path, *, immutable: bool) -> FileResponse:
    headers = dict(SECURITY_HEADERS)
    if immutable:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        headers["Cache-Control"] = "no-cache"
    return FileResponse(path, headers=headers)


def register_ui(app: FastAPI, settings) -> None:
    root = web_dir(settings)
    fallback = root / "200.html"
    if not fallback.is_file():
        return

    @app.get("/ui", include_in_schema=False)
    async def ui_root():
        return RedirectResponse(url="/ui/", status_code=307, headers=SECURITY_HEADERS)

    @app.get("/ui/", include_in_schema=False)
    async def ui_index():
        return _serve(fallback, immutable=False)

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def ui_asset(path: str):
        candidate = _safe_path(root, path)
        if candidate is None:
            raise HTTPException(status_code=404, detail="asset not found")
        if candidate.is_file():
            immutable = path.startswith("_app/immutable/")
            return _serve(candidate, immutable=immutable)
        # A missing hashed asset is a genuine 404, never the SPA shell. The
        # SvelteKit build puts every hashed file under _app/immutable.
        if path.startswith("_app/"):
            raise HTTPException(status_code=404, detail="asset not found")
        return _serve(fallback, immutable=False)
