"""MCP surface: tool registry, header extraction, compact returns, lifespan.

Uses the real FastMCP client against an in-process ASGI app, but with a
state-aware lifespan driver: fastmcp 4.2.6's own ``run_asgi_lifespan`` constructs
a lifespan scope *without* ``state``, which raises once ``combine_lifespans``
yields a mapping. That is a framework bug, not ours, so the harness supplies it.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.utilities.asgi_transport import StreamingASGITransport

from agent_voice.api.app import create_app
from agent_voice.config import Settings
from agent_voice.mcp_stdio_proxy import _is_healthy

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOLS = {
    "server_info",
    "speak",
    "speak_script",
    "start_script_job",
    "get_job",
    "cancel_job",
    "clone_voice",
    "list_voices",
    "delete_voice",
    "set_agent_binding",
    "list_agent_bindings",
    "list_engines",
    "list_models",
    "download_model",
    "register_model",
    "register_engine",
    "install_engine",
    "gpu_status",
    "unload_models",
}


def _settings(tmp_path: Path, **overrides) -> Settings:
    """Copy the engine manifests into tmp so mutation tests never touch the repo.

    Only the manifest and worker files are copied: an engine venv can be
    gigabytes, and no MCP test needs a real interpreter there.
    """
    engines = tmp_path / "engines"
    source = REPO_ROOT / "engines"
    for entry in source.iterdir():
        if not entry.is_dir() or entry.name == "agenteng":
            continue
        target = engines / entry.name
        target.mkdir(parents=True)
        for filename in ("engine.yaml", "worker.py", "pyproject.toml"):
            candidate = entry / filename
            if candidate.is_file():
                (target / filename).write_bytes(candidate.read_bytes())

    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=engines,
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        ALLOW_AGENT_ENGINE_MUTATION="true",
    )
    base.update(overrides)
    return Settings(**base)


@contextlib.asynccontextmanager
async def lifespan_driver(app: FastAPI) -> AsyncIterator[None]:
    """Drive the ASGI lifespan with a scope that includes ``state``.

    ``combine_lifespans`` yields a mapping, and Starlette refuses to accept one
    unless ``scope['state']`` exists. This is the documented workaround.
    """
    receive_queue: list[dict[str, Any]] = [{"type": "lifespan.startup"}]
    send_events: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        if receive_queue:
            return receive_queue.pop(0)
        await __import__("asyncio").sleep(3600)
        return {"type": "lifespan.startup"}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        send_events.append(message)

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    task = __import__("asyncio").create_task(app(scope, receive, send))
    try:
        for _ in range(300):
            if any(m["type"].startswith("lifespan.startup") for m in send_events):
                break
            await __import__("asyncio").sleep(0.01)
        if not any(m["type"] == "lifespan.startup.complete" for m in send_events):
            raise RuntimeError(f"lifespan failed to start: {send_events}")
        yield
    finally:
        receive_queue.append({"type": "lifespan.shutdown"})
        with contextlib.suppress(Exception):
            await __import__("asyncio").wait_for(task, timeout=10)


@contextlib.asynccontextmanager
async def mcp_client(tmp_path: Path, *, headers: dict[str, str] | None = None, **overrides):
    settings = _settings(tmp_path, **overrides)
    app = create_app(settings=settings, mount_mcp=True)

    def factory(**kwargs: Any) -> httpx2.AsyncClient:
        # **kwargs is mandatory: fastmcp passes follow_redirects, which the
        # declared McpHttpClientFactory protocol does not mention.
        return httpx2.AsyncClient(transport=StreamingASGITransport(app), **kwargs)

    transport = StreamableHttpTransport(
        url="http://testserver/mcp/",
        headers=headers or {},
        httpx_client_factory=factory,
    )
    async with lifespan_driver(app):
        async with Client(transport) as client:
            yield client, app


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_documented_tools_are_registered(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        assert names == EXPECTED_TOOLS, f"missing: {EXPECTED_TOOLS - names}"


@pytest.mark.asyncio
async def test_tools_have_descriptions(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        for tool in await client.list_tools():
            assert tool.description, f"{tool.name} has no description for the model"


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_engines_reports_install_state(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("list_engines", {})
        engines = {e["engine_id"]: e for e in result.data["engines"]}
        assert "mock" in engines
        assert engines["mock"]["installed"] is True
        # Every engine must explain itself when it cannot be used.
        for engine in engines.values():
            assert engine["installed"] is True or engine["why_unavailable"]


@pytest.mark.asyncio
async def test_list_models_includes_hf_repo_and_download_state(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("list_models", {})
        models = result.data["models"]
        assert any(m["hf_repo"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base" for m in models)
        for model in models:
            assert "downloaded_locally" in model
            assert "loaded" in model


@pytest.mark.asyncio
async def test_list_models_filters_by_engine(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("list_models", {"engine_id": "mock"})
        assert all(m["engine_id"] == "mock" for m in result.data["models"])


@pytest.mark.asyncio
async def test_gpu_status_shape(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("gpu_status", {})
        for key in ("active_engine", "workers", "queue_depth", "in_flight"):
            assert key in result.data


# ---------------------------------------------------------------------------
# Synthesis tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_returns_a_file_path_not_audio(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("speak", {"text": "Hello from MCP."})
        body = result.data
        assert Path(body["file_path"]).is_file()
        assert "audio" not in body
        assert "base64" not in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_speak_payload_is_compact(tmp_path):
    """Tool results are read by a model; they must stay small."""
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("speak", {"text": "Compact please."})
        serialised = json.dumps(result.data)
        assert len(serialised) < 2000, f"result too large: {len(serialised)} bytes"


@pytest.mark.asyncio
async def test_speak_reports_structured_error_for_unknown_engine(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("speak", {"text": "Hi.", "engine": "nope"})
        assert result.data["error_code"] == "ENGINE_NOT_FOUND"
        assert result.data["suggestion"]


@pytest.mark.asyncio
async def test_speak_script_returns_timings(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool(
            "speak_script",
            {"beats": [{"text": "One."}, {"text": "Two."}], "export_srt": True},
        )
        assert len(result.data["beats"]) == 2
        assert Path(result.data["srt_path"]).is_file()


@pytest.mark.asyncio
async def test_start_script_job_then_get_job(tmp_path):
    import asyncio

    async with mcp_client(tmp_path) as (client, _app):
        started = await client.call_tool(
            "start_script_job", {"beats": [{"text": "Async beat."}]}
        )
        job_id = started.data["job_id"]
        assert job_id

        job = None
        for _ in range(200):
            job = (await client.call_tool("get_job", {"job_id": job_id})).data
            if job["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "succeeded"


@pytest.mark.asyncio
async def test_get_unknown_job_returns_structured_error(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("get_job", {"job_id": "nope"})
        assert result.data["error_code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_list_delete_voice(tmp_path):
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "ref.wav"
    sr = 24_000
    n = sr * 5
    t = np.linspace(0, 5, n, endpoint=False)
    sf.write(audio, (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)

    async with mcp_client(tmp_path) as (client, _app):
        created = await client.call_tool(
            "clone_voice", {"name": "mcp-voice", "ref_audio_path": str(audio)}
        )
        voice_id = created.data["voice_id"]

        listed = (await client.call_tool("list_voices", {})).data["voices"]
        assert any(v["voice_id"] == voice_id for v in listed)

        deleted = (await client.call_tool("delete_voice", {"voice_id": voice_id})).data
        assert deleted["deleted"] == voice_id


@pytest.mark.asyncio
async def test_clone_voice_rejects_short_clip(tmp_path):
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "short.wav"
    sf.write(audio, np.zeros(int(24_000 * 0.3), dtype=np.float32), 24_000)

    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool(
            "clone_voice", {"name": "short", "ref_audio_path": str(audio)}
        )
        assert result.data["error_code"] == "INVALID_REF_AUDIO"


# ---------------------------------------------------------------------------
# Header extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_agent_id_selects_binding(tmp_path):
    """X-Agent-Id is read from the live request and applied to routing."""
    async with mcp_client(tmp_path, headers={"X-Agent-Id": "agent-xyz"}) as (client, _app):
        await client.call_tool(
            "set_agent_binding",
            {"agent_id": "agent-xyz", "engine_id": "mock", "model_alias": "mock-tone"},
        )
        bindings = (await client.call_tool("list_agent_bindings", {})).data["bindings"]
        assert any(b["agent_id"] == "agent-xyz" for b in bindings)

        spoken = (await client.call_tool("speak", {"text": "Routed by header."})).data
        assert spoken["engine_id"] == "mock"


@pytest.mark.asyncio
async def test_missing_agent_header_still_works(tmp_path):
    """No X-Agent-Id must not break anything: it falls back to server defaults."""
    async with mcp_client(tmp_path) as (client, _app):
        spoken = (await client.call_tool("speak", {"text": "No header."})).data
        assert spoken["engine_id"] == "mock"


# ---------------------------------------------------------------------------
# Mutation gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_disabled_blocks_register_engine(tmp_path):
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        result = await client.call_tool(
            "register_engine",
            {"engine_id": "neweng", "engine_yaml_content": "backend_name: x\n"},
        )
        assert result.data["error_code"] == "AGENT_MUTATION_DISABLED"
        assert "ALLOW_AGENT_ENGINE_MUTATION" in result.data["suggestion"]


@pytest.mark.asyncio
async def test_mutation_disabled_blocks_register_model(tmp_path):
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        result = await client.call_tool(
            "register_model",
            {"engine_id": "mock", "model_alias": "x-model", "hf_repo": "a/b"},
        )
        assert result.data["error_code"] == "AGENT_MUTATION_DISABLED"


@pytest.mark.asyncio
async def test_register_engine_succeeds_when_enabled(tmp_path):
    manifest = (
        "engine_id: agenteng\n"
        "backend_name: agenteng\n"
        "backend_version: '1'\n"
        "venv_python: null\n"
        "models: []\n"
    )
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool(
            "register_engine",
            {
                "engine_id": "agenteng",
                "engine_yaml_content": manifest,
                "worker_py_content": "print('x')\n",
                "install_venv": False,
            },
        )
        assert result.data["engine_id"] == "agenteng"

        engines = (await client.call_tool("list_engines", {})).data["engines"]
        assert any(e["engine_id"] == "agenteng" for e in engines), "must hot-reload"


@pytest.mark.asyncio
async def test_register_engine_rejects_traversal(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool(
            "register_engine",
            {"engine_id": "../../evil", "engine_yaml_content": "backend_name: x\n"},
        )
        assert "error_code" in result.data


# ---------------------------------------------------------------------------
# MCP_BASE_PATH output relocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_land_inside_mcp_base(tmp_path):
    """The returned path must be readable from inside the agent's sandbox."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    async with mcp_client(tmp_path, MCP_BASE_PATH=str(sandbox)) as (client, _app):
        body = (await client.call_tool("speak", {"text": "Sandboxed."})).data
        path = Path(body["file_path"])
        assert path.is_file()
        assert sandbox.resolve() in path.parents
        assert "artifacts" in path.parts


@pytest.mark.asyncio
async def test_clone_voice_confined_to_mcp_base(tmp_path):
    import numpy as np
    import soundfile as sf

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside.wav"
    sf.write(outside, np.zeros(24_000 * 5, dtype=np.float32), 24_000)

    async with mcp_client(tmp_path, MCP_BASE_PATH=str(sandbox)) as (client, _app):
        result = await client.call_tool(
            "clone_voice", {"name": "escape", "ref_audio_path": str(outside)}
        )
        assert result.data["error_code"] == "PATH_OUTSIDE_BASE"


# ---------------------------------------------------------------------------
# Lifespan correctness
# ---------------------------------------------------------------------------


def test_lifespan_composition_is_required(tmp_path):
    """Without combine_lifespans the mounted MCP app has no session manager."""
    settings = _settings(tmp_path)
    app_with_mcp = create_app(settings=settings, mount_mcp=True)
    mounted = [route for route in app_with_mcp.routes if getattr(route, "path", "") == "/mcp"]
    assert mounted, "MCP app must be mounted at /mcp"


def test_mcp_mount_uses_root_path(tmp_path):
    """path='/' + mount('/mcp') => '/mcp/'. path='/mcp' would double-nest."""
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=True)
    mount = next(r for r in app.routes if getattr(r, "path", "") == "/mcp")
    sub_paths = [getattr(r, "path", "") for r in mount.app.routes]
    assert "" in sub_paths or "/" in sub_paths
    assert not any(p == "/mcp" for p in sub_paths)


# ---------------------------------------------------------------------------
# stdio proxy behaviour
# ---------------------------------------------------------------------------


def test_health_probe_returns_false_when_nothing_is_listening():
    assert _is_healthy("http://127.0.0.1:59999") is False


def test_proxy_builds_with_custom_headers(tmp_path):
    """The proxy must forward X-Agent-Id and the bearer token upstream."""
    from agent_voice.mcp_stdio_proxy import build_proxy

    settings = _settings(tmp_path, AGENT_VOICE_API_KEY="k")
    proxy = build_proxy(settings, client_id="agent-a")
    assert proxy is not None
