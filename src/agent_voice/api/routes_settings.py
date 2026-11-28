"""Server info, safe settings, lexicon, cache, auth and diagnostics."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Query, Request

from ..schemas import (
    AgentVoiceError,
    AuthCheckResponse,
    DiagnosticCheck,
    DiagnosticsResponse,
    ErrorCode,
    ServerInfoResponse,
    SettingsPatchRequest,
    SettingsResponse,
)
from ..services import settings_service
from ..services.server_info_service import server_info

router = APIRouter(tags=["studio"])


@router.get("/v1/server_info", response_model=ServerInfoResponse)
async def get_server_info(request: Request):
    return (await server_info(request.app.state.ctx)).model_dump(mode="json")


@router.get("/v1/settings", response_model=SettingsResponse)
async def get_settings_route(request: Request):
    return settings_service.response(request.app.state.ctx)


@router.patch("/v1/settings", response_model=SettingsResponse)
async def patch_settings(payload: SettingsPatchRequest, request: Request):
    return settings_service.patch(request.app.state.ctx, payload.values)


def _lexicon_payload(ctx, project: str | None, voice_id: str | None) -> dict:
    layers = [("global", None, None)]
    if project:
        layers.append((f"project:{project}", project, None))
    if voice_id:
        layers.append((f"voice:{voice_id}", None, voice_id))
    scopes = []
    for name, layer_project, layer_voice in layers:
        store = ctx.lexicon.store_for(project=layer_project, voice_id=layer_voice)
        scopes.append({"scope": name, "path": str(store.path), "entries": store.list()})
    merged = ctx.lexicon.effective(project=project, voice_id=voice_id)
    return {
        "project": project,
        "voice_id": voice_id,
        "scopes": scopes,
        "merged_entries": len(merged),
        "effective": dict(sorted(merged.items())),
        "pronunciation_hash": ctx.lexicon.hash(project=project, voice_id=voice_id),
    }


@router.get("/v1/lexicon")
async def get_lexicon(
    request: Request,
    project: str | None = None,
    voice_id: str | None = None,
):
    return _lexicon_payload(request.app.state.ctx, project, voice_id)


@router.put("/v1/lexicon")
async def put_lexicon(payload: dict, request: Request):
    ctx = request.app.state.ctx
    word = str(payload.get("word") or "").strip()
    replacement = payload.get("replacement")
    scope = str(payload.get("scope") or "global")
    project = payload.get("project")
    voice_id = payload.get("voice_id")
    if not word or not isinstance(replacement, str):
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            "word and replacement are required",
            'Send {"word":"kubectl","replacement":"cube control"}.',
        )
    if scope not in {"global", "project", "voice"}:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown lexicon scope: {scope}",
            "Use global, project or voice.",
        )
    if scope == "project" and not project:
        raise AgentVoiceError(ErrorCode.BAD_REQUEST, "project scope needs project", "")
    if scope == "voice" and not voice_id:
        raise AgentVoiceError(ErrorCode.BAD_REQUEST, "voice scope needs voice_id", "")
    if scope != "project":
        project = None
    if scope != "voice":
        voice_id = None
    try:
        store = ctx.lexicon.store_for(
            project=str(project) if project else None,
            voice_id=str(voice_id) if voice_id else None,
        )
        store.set(word, replacement)
        ctx.lexicon.clear_cache()
    except (AgentVoiceError, ValueError) as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"could not write pronunciation: {exc}",
            "Check the scope, entry length and the 500-entry cap.",
        ) from exc
    return _lexicon_payload(
        ctx, str(project) if project else None, str(voice_id) if voice_id else None
    )


@router.delete("/v1/lexicon")
async def delete_lexicon(
    request: Request,
    word: str,
    scope: str = Query("global"),
    project: str | None = None,
    voice_id: str | None = None,
):
    ctx = request.app.state.ctx
    if scope not in {"global", "project", "voice"}:
        raise AgentVoiceError(ErrorCode.BAD_REQUEST, f"unknown scope: {scope}", "")
    try:
        store = ctx.lexicon.store_for(
            project=project if scope == "project" else None,
            voice_id=voice_id if scope == "voice" else None,
        )
        deleted = store.delete(word)
        ctx.lexicon.clear_cache()
    except (AgentVoiceError, ValueError) as exc:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"could not delete pronunciation: {exc}",
            "Check the scope and term.",
        ) from exc
    if not deleted:
        raise AgentVoiceError(
            ErrorCode.RECORD_NOT_FOUND,
            f"no such pronunciation: {word}",
            "List the lexicon to see existing terms.",
        )
    return {"deleted": word}


def _cache_payload(ctx) -> dict:
    cache = ctx.cache
    entries = []
    for path in cache.cache_dir.rglob("*"):
        if not path.is_file() or path.suffix in {".lock", ".json", ".tmp"}:
            continue
        try:
            entries.append(path.stat().st_size)
        except OSError:
            continue
    return {
        "cache_dir": str(cache.cache_dir),
        "entries": len(entries),
        "total_bytes": sum(entries),
        "max_bytes": cache.max_bytes,
    }


@router.get("/v1/cache")
async def cache_stats(request: Request):
    return _cache_payload(request.app.state.ctx)


@router.delete("/v1/cache")
async def clear_cache(request: Request):
    ctx = request.app.state.ctx
    before = _cache_payload(ctx)
    ctx.cache.clear()
    after = _cache_payload(ctx)
    return {
        "files_deleted": max(0, before["entries"] - after["entries"]),
        "bytes_freed": max(0, before["total_bytes"] - after["total_bytes"]),
    }


@router.get("/v1/auth/check", response_model=AuthCheckResponse)
async def auth_check(request: Request):
    return AuthCheckResponse(auth_required=bool(request.app.state.ctx.settings.api_key))


def _mcp_config(home: Path, client: str) -> dict:
    server = {
        "command": "uv",
        "args": ["run", "--directory", str(home), "agent-voice", "mcp"],
        "env": {"AGENT_VOICE_CLIENT_ID": client},
    }
    if client == "vscode":
        return {"servers": {"agent-voice": {"type": "stdio", **server}}}
    return {"mcpServers": {"agent-voice": server}}


@router.get("/v1/mcp-config")
async def mcp_config(
    request: Request,
    client: str = Query("command-code"),
):
    if client not in {"claude-code", "claude-desktop", "vscode", "cursor", "command-code"}:
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"unknown MCP client: {client}",
            "Use claude-code, claude-desktop, vscode, cursor or command-code.",
        )
    return _mcp_config(request.app.state.ctx.settings.home, client)


def _check(name: str, ok: bool, detail: str, fix: str = "") -> DiagnosticCheck:
    return DiagnosticCheck(
        name=name,
        status="PASS" if ok else "FAIL",
        detail=detail,
        fix="" if ok else fix,
    )


@router.get("/v1/diagnostics", response_model=DiagnosticsResponse)
async def diagnostics(request: Request):
    ctx = request.app.state.ctx
    checks: list[DiagnosticCheck] = []
    checks.append(
        _check("python", True, f"{os.sys.version_info.major}.{os.sys.version_info.minor}")
    )
    for tool in ("ffmpeg", "ffprobe", "uv"):
        path = shutil.which(tool)
        checks.append(_check(tool, path is not None, path or "not on PATH", f"install {tool}"))
    checks.append(
        _check(
            "torch-in-core",
            "torch" not in __import__("sys").modules,
            "torch is not imported by the daemon"
            if "torch" not in __import__("sys").modules
            else "torch imported in core",
            "keep heavy imports inside engine workers",
        )
    )
    try:
        entries = ctx.registry.list_entries()
        installed = [entry.engine_id for entry in entries if entry.installed]
        checks.append(
            _check(
                "engines",
                bool(installed),
                ", ".join(installed) or "no engine installed",
                "agent-voice engines install mock",
            )
        )
        checks.append(
            _check(
                "asr",
                any(entry.kind == "asr" and entry.installed for entry in entries),
                f"default={ctx.settings.default_asr_engine}",
                f"agent-voice engines install {ctx.settings.default_asr_engine}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("engines", False, str(exc), "fix ENGINES_DIR"))
    try:
        await ctx.workers.gpu_status()
        checks.append(_check("gpu", True, "status readable"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("gpu", False, str(exc), "check the GPU driver"))
    checks.append(
        _check(
            "config",
            ctx.settings.config_path.parent.is_dir(),
            str(ctx.settings.config_path),
            "create the home directory",
        )
    )
    web_ok = (ctx.settings.web_dir / "200.html").is_file()
    checks.append(
        _check(
            "ui",
            web_ok,
            "built at src/agent_voice/web" if web_ok else "built UI not present",
            "run scripts/build_web.py",
        )
    )
    return DiagnosticsResponse(checked_at=time.time(), checks=checks)
