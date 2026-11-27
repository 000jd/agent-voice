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


def payload(result):
    """Unwrap a CallToolResult into plain JSON, recursively.

    Tools that return a Pydantic model arrive as a ``Root`` wrapper (fastmcp
    builds a RootModel output schema), and a list of models arrives as a list of
    ``Root`` objects. Both carry exactly the same data as a dict, so tests should
    not care which shape the server chose.
    """
    return _unwrap(result.data)


def _unwrap(data):
    if data is None or isinstance(data, (bool, int, float, bytes)):
        return data
    if isinstance(data, list):
        return [_unwrap(item) for item in data]
    if isinstance(data, tuple):
        return [_unwrap(item) for item in data]
    if isinstance(data, dict):
        return {key: _unwrap(value) for key, value in data.items()}
    if isinstance(data, str):
        # Only unwrap a serialized object/array. A plain string must stay a string:
        # json.loads("3469177413594335") is an int, and uuid4().hex[:16] produces
        # an all-digit job id about once in 1,300 runs, which would turn the id
        # the test feeds back into get_job into a type error.
        if data.lstrip()[:1] not in {"{", "["}:
            return data
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data

    # fastmcp wraps a tool's typed return in its own ``Root`` container. It is not
    # a pydantic RootModel (no ``.root``), it simply exposes the model's fields as
    # attributes and mirrors them in ``__dict__``. Pydantic models additionally
    # offer ``model_dump``. Handle all three shapes so the tests assert on data,
    # not on the transport's wrapper class.
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        return _unwrap(dump())
    if hasattr(data, "__dict__") and data.__dict__:
        return {key: _unwrap(value) for key, value in vars(data).items()}
    return data


def test_payload_keeps_a_digit_only_string_a_string():
    """A job id that happens to be all digits must not become an int."""
    assert _unwrap("1234567890123456") == "1234567890123456"
    assert _unwrap('{"a": 1}') == {"a": 1}
    assert _unwrap("[1, 2]") == [1, 2]


async def call_expecting_error(client, name: str, arguments: dict) -> dict:
    """Call a tool that must fail, returning its structured error payload.

    v0.2 raises ``ToolError`` (so MCP clients see ``isError=true``) instead of
    returning a dict with ``isError=false``, which clients could not distinguish
    from success. The JSON body is unchanged, so this returns it parsed.
    """
    from fastmcp.exceptions import ToolError

    try:
        result = await client.call_tool(name, arguments)
    except ToolError as exc:
        return json.loads(str(exc))
    raise AssertionError(
        f"{name} should have raised ToolError, got a successful result: "
        f"{payload(result)!r}"
    )


EXPECTED_TOOLS = {
    "server_info",
    "speak",
    "speak_script",
    "start_script_job",
    "get_job",
    "cancel_job",
    "clone_voice",
    "design_voice",
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
    # v0.5.0: speech in and dubbing are the feature, not an operator extra, so
    # they are core rather than full-only.
    "transcribe",
    "dub_prepare",
    "dub_render",
    "start_dub_job",
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

    Shutdown matters for wall-clock: the receive queue hands out exactly the two
    lifespan messages and then signals end-of-stream. Anything else makes the
    app task wait forever and every teardown pays the full timeout — which is
    what made this suite take four minutes.
    """
    asyncio_mod = __import__("asyncio")
    receive_queue: asyncio_mod.Queue[dict[str, Any]] = asyncio_mod.Queue()
    send_events: list[dict[str, Any]] = []
    started: asyncio_mod.Event = asyncio_mod.Event()
    stopped: asyncio_mod.Event = asyncio_mod.Event()

    await receive_queue.put({"type": "lifespan.startup"})

    async def receive() -> dict[str, Any]:
        return await receive_queue.get()

    async def send(message: dict[str, Any]) -> None:
        send_events.append(message)
        if message["type"] == "lifespan.startup.complete":
            started.set()
        elif message["type"] in {
            "lifespan.shutdown.complete",
            "lifespan.shutdown.failed",
        }:
            stopped.set()

    scope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}
    task = asyncio_mod.create_task(app(scope, receive, send))
    try:
        # Poll the event, not a sleep loop, so startup is as fast as the app.
        try:
            await asyncio_mod.wait_for(started.wait(), timeout=30)
        except (TimeoutError, asyncio_mod.TimeoutError) as exc:
            raise RuntimeError(f"lifespan failed to start: {send_events}") from exc
        yield
    finally:
        await receive_queue.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(Exception):
            await asyncio_mod.wait_for(stopped.wait(), timeout=10)
        with contextlib.suppress(Exception):
            await asyncio_mod.wait_for(task, timeout=5)
        if not task.done():
            task.cancel()
            with contextlib.suppress(Exception):
                await task


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
        engines = {e["engine_id"]: e for e in payload(result)["engines"]}
        assert "mock" in engines
        assert engines["mock"]["installed"] is True
        # Every engine must explain itself when it cannot be used.
        for engine in engines.values():
            assert engine["installed"] is True or engine["why_unavailable"]


@pytest.mark.asyncio
async def test_list_models_includes_hf_repo_and_download_state(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("list_models", {})
        models = payload(result)["models"]
        assert any(m["hf_repo"] == "Qwen/Qwen3-TTS-12Hz-1.7B-Base" for m in models)
        for model in models:
            assert "downloaded_locally" in model
            assert "loaded" in model


@pytest.mark.asyncio
async def test_list_models_filters_by_engine(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("list_models", {"engine_id": "mock"})
        assert all(m["engine_id"] == "mock" for m in payload(result)["models"])


@pytest.mark.asyncio
async def test_gpu_status_shape(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("gpu_status", {})
        for key in ("active_engine", "workers", "queue_depth", "in_flight"):
            assert key in payload(result)


@pytest.mark.asyncio
async def test_server_info_advertises_preset_speakers_and_voice_design(tmp_path):
    """server_info must name the timbres speak(speaker=...) accepts right now.

    On the mock engine the custom_voice and voice_design checkpoints ship as
    ``local/`` repos, so both answers are available without a download. An agent
    reads this before deciding between speaker=, instruct= and design_voice().
    """
    async with mcp_client(tmp_path) as (client, _app):
        info = payload(await client.call_tool("server_info", {}))
        assert {"Ryan", "Serena"} <= set(info["preset_speakers"])
        assert info["voice_design_available"] is True
        # Still a free snapshot: nothing is resident until something is asked for.
        assert info["voices_count"] == 0


# ---------------------------------------------------------------------------
# Synthesis tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speak_returns_a_file_path_not_audio(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("speak", {"text": "Hello from MCP."})
        body = payload(result)
        assert Path(body["file_path"]).is_file()
        assert "audio" not in body
        assert "base64" not in json.dumps(body).lower()


@pytest.mark.asyncio
async def test_speak_payload_is_compact(tmp_path):
    """Tool results are read by a model; they must stay small."""
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool("speak", {"text": "Compact please."})
        serialised = json.dumps(payload(result))
        assert len(serialised) < 2000, f"result too large: {len(serialised)} bytes"


@pytest.mark.asyncio
async def test_speak_reports_realtime_multiple_and_its_deprecated_alias(tmp_path):
    """v0.3.3 renames the RTF field; both names carry the same number for now."""
    async with mcp_client(tmp_path) as (client, _app):
        body = payload(await client.call_tool("speak", {"text": "How fast is this?"}))
        assert "realtime_multiple" in body
        assert "real_time_factor" in body
        assert body["realtime_multiple"] == body["real_time_factor"]
        assert body["realtime_multiple"] is not None, "a real render has a wall clock"


@pytest.mark.asyncio
async def test_speak_reports_structured_error_for_unknown_engine(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(client, "speak", {"text": "Hi.", "engine": "nope"})
        assert body["error_code"] == "ENGINE_NOT_FOUND"
        assert body["suggestion"]


@pytest.mark.asyncio
async def test_speak_with_speaker_and_instruct_reports_its_mode(tmp_path):
    """A preset timbre plus delivery direction is custom_voice, not a clone."""
    async with mcp_client(tmp_path) as (client, _app):
        body = payload(
            await client.call_tool(
                "speak",
                {
                    "text": "Preset timbre with a delivery instruction.",
                    "speaker": "Ryan",
                    "instruct": "slow, warm and calm",
                },
            )
        )
        assert body["mode"] == "custom_voice"
        assert body["speaker"] == "Ryan"
        assert body["voice_id"] is None, "a preset speaker has no reference clip"
        assert body["model_alias"] == "mock-custom"
        assert Path(body["file_path"]).is_file()


@pytest.mark.asyncio
async def test_speak_on_a_preset_works_with_no_voice_registered(tmp_path):
    """speaker= is the one way to hear audio on a fresh install with no clips."""
    async with mcp_client(tmp_path) as (client, _app):
        assert payload(await client.call_tool("list_voices", {}))["voices"] == []
        body = payload(
            await client.call_tool("speak", {"text": "No voice needed.", "speaker": "Serena"})
        )
        assert body["mode"] == "custom_voice"
        assert body["speaker"] == "Serena"
        assert Path(body["file_path"]).is_file()


@pytest.mark.asyncio
async def test_speak_rejects_voice_and_speaker_together(tmp_path):
    """Two timbres in one request: whichever won would be a guess."""
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(
            client, "speak", {"text": "Both.", "voice": "any-voice", "speaker": "Ryan"}
        )
        assert body["error_code"] == "BAD_REQUEST"
        assert body["suggestion"]


@pytest.mark.asyncio
async def test_speak_rejects_unknown_preset_speaker(tmp_path):
    """The suggestion must name the speakers that do exist."""
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(
            client, "speak", {"text": "Who?", "speaker": "Nobody"}
        )
        assert body["error_code"] == "BAD_REQUEST"
        assert "Ryan" in body["suggestion"]


@pytest.mark.asyncio
async def test_speak_script_returns_timings(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = await client.call_tool(
            "speak_script",
            {"beats": [{"text": "One."}, {"text": "Two."}], "export_srt": True},
        )
        assert len(payload(result)["beats"]) == 2
        assert Path(payload(result)["srt_path"]).is_file()


@pytest.mark.asyncio
async def test_start_script_job_then_get_job(tmp_path):
    import asyncio

    async with mcp_client(tmp_path) as (client, _app):
        started = await client.call_tool(
            "start_script_job", {"beats": [{"text": "Async beat."}]}
        )
        job_id = payload(started)["job_id"]
        assert job_id

        job = None
        for _ in range(200):
            job = payload(await client.call_tool("get_job", {"job_id": job_id}))
            if job["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0.05)
        assert job["status"] == "succeeded"


@pytest.mark.asyncio
async def test_get_unknown_job_returns_structured_error(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(client, "get_job", {"job_id": "nope"})
        assert body["error_code"] == "BAD_REQUEST"


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
        voice_id = payload(created)["voice_id"]

        listed = payload(await client.call_tool("list_voices", {}))["voices"]
        assert any(v["voice_id"] == voice_id for v in listed)

        deleted = payload(await client.call_tool("delete_voice", {"voice_id": voice_id}))
        assert deleted["deleted"] == voice_id


@pytest.mark.asyncio
async def test_clone_voice_rejects_short_clip(tmp_path):
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "short.wav"
    sf.write(audio, np.zeros(int(24_000 * 0.3), dtype=np.float32), 24_000)

    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(
            client, "clone_voice", {"name": "short", "ref_audio_path": str(audio)}
        )
        assert body["error_code"] == "INVALID_REF_AUDIO"


@pytest.mark.asyncio
async def test_design_voice_returns_a_designed_payload_and_saves_it(tmp_path):
    """design_voice talks or describes: no reference clip is supplied at all."""
    instruct = "a warm, low British narrator in his forties, unhurried"
    async with mcp_client(tmp_path) as (client, _app):
        body = payload(
            await client.call_tool("design_voice", {"name": "mcp-designer", "instruct": instruct})
        )
        assert body["source"] == "designed"
        assert body["design_instruct"] == instruct
        assert body["name"] == "mcp-designer"
        assert body["model_alias"] == "mock-design"
        assert body["engine_id"] == "mock"
        assert body["voice_id"], "save_as_clone defaults to true"
        assert Path(body["preview_file_path"]).is_file()
        assert body["duration_sec"] > 0

        # The description is kept as provenance, so a designed voice is auditable.
        voices = payload(await client.call_tool("list_voices", {}))["voices"]
        saved = next(v for v in voices if v["voice_id"] == body["voice_id"])
        assert saved["source"] == "designed"
        assert saved["design_instruct"] == instruct
        assert saved["ref_text"], "the anchor sentence is stored as the transcript"


@pytest.mark.asyncio
async def test_designed_voice_is_speakable_afterwards(tmp_path):
    """'Design once, speak a thousand times': the save is an ordinary voice."""
    async with mcp_client(tmp_path) as (client, _app):
        designed = payload(
            await client.call_tool(
                "design_voice", {"name": "reusable", "instruct": "bright and quick"}
            )
        )
        spoken = payload(
            await client.call_tool(
                "speak", {"text": "Now in the designed voice.", "voice": designed["voice_id"]}
            )
        )
        assert spoken["mode"] == "clone"
        assert spoken["voice_id"] == designed["voice_id"]
        assert Path(spoken["file_path"]).is_file()


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
        bindings = payload(await client.call_tool("list_agent_bindings", {}))["bindings"]
        assert any(b["agent_id"] == "agent-xyz" for b in bindings)

        spoken = payload(await client.call_tool("speak", {"text": "Routed by header."}))
        assert spoken["engine_id"] == "mock"


@pytest.mark.asyncio
async def test_missing_agent_header_still_works(tmp_path):
    """No X-Agent-Id must not break anything: it falls back to server defaults."""
    async with mcp_client(tmp_path) as (client, _app):
        spoken = payload(await client.call_tool("speak", {"text": "No header."}))
        assert spoken["engine_id"] == "mock"


# ---------------------------------------------------------------------------
# Mutation gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_disabled_blocks_register_engine(tmp_path):
    """With the core toolset the mutation tool is absent, not merely refusing.

    v0.2 hides mutation tools unless the operator opts in, so the observable
    contract is "the tool does not exist" rather than an AGENT_MUTATION_DISABLED
    payload.
    """
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        names = {tool.name for tool in await client.list_tools()}
        assert "register_engine" not in names
        # 12 since v0.3.0: design_voice joined core, because "describe a voice and
        # speak it" is the one path that needs neither a clip nor a download, and
        # delete_voice has been core since v0.2.1 so an agent can clean up a bad
        # clone without dropping to the CLI.
        #
        # 16 since v0.5.0: transcribe, dub_prepare, dub_render and start_dub_job.
        # Speech *in* and dubbing are the release, not an operator extra, so they
        # belong on the default surface — and a dubbing agent that had to ask an
        # operator to flip AGENT_VOICE_TOOLSET first would not be agent-native.
        assert len(names) == 16
        assert "delete_voice" in names
        assert "design_voice" in names


@pytest.mark.asyncio
async def test_mutation_disabled_blocks_register_model(tmp_path):
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        names = {tool.name for tool in await client.list_tools()}
        assert "register_model" not in names


@pytest.mark.asyncio
async def test_mutation_refused_when_toolset_full_but_flag_off(tmp_path):
    """`toolset=full` exposes the tools; the flag still refuses them at call time."""
    async with mcp_client(
        tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false", AGENT_VOICE_TOOLSET="full"
    ) as (client, _app):
        body = await call_expecting_error(
            client,
            "register_engine",
            {"engine_id": "neweng", "engine_yaml_content": "backend_name: x\n"},
        )
        assert body["error_code"] == "AGENT_MUTATION_DISABLED"
        assert "ALLOW_AGENT_ENGINE_MUTATION" in body["suggestion"]


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
        assert payload(result)["engine_id"] == "agenteng"

        engines = payload(await client.call_tool("list_engines", {}))["engines"]
        assert any(e["engine_id"] == "agenteng" for e in engines), "must hot-reload"


@pytest.mark.asyncio
async def test_register_engine_rejects_traversal(tmp_path):
    """Path traversal must be refused, and nothing may be written outside engines/."""
    async with mcp_client(
        tmp_path, ALLOW_AGENT_ENGINE_MUTATION="true", AGENT_VOICE_TOOLSET="full"
    ) as (client, _app):
        body = await call_expecting_error(
            client,
            "register_engine",
            {"engine_id": "../../evil", "engine_yaml_content": "backend_name: x\n"},
        )
        assert body["error_code"] in {"INVALID_SLUG", "MANIFEST_INVALID", "BAD_REQUEST"}
        assert not (tmp_path / "evil").exists()


# ---------------------------------------------------------------------------
# v0.3.3: toolset-aware ENGINE_NOT_INSTALLED suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_not_installed_suggestion_omits_the_absent_tool(tmp_path):
    """With mutation off there is no install_engine() to offer, so do not name it."""
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        body = await call_expecting_error(
            client, "speak", {"text": "Hi.", "engine": "qwen3_tts"}
        )
        assert body["error_code"] == "ENGINE_NOT_INSTALLED"
        assert "agent-voice engines install" in body["suggestion"]
        assert "install_engine" not in body["suggestion"]


@pytest.mark.asyncio
async def test_engine_not_installed_suggestion_names_the_tool_when_enabled(tmp_path):
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="true") as (client, _app):
        body = await call_expecting_error(
            client, "speak", {"text": "Hi.", "engine": "qwen3_tts"}
        )
        assert body["error_code"] == "ENGINE_NOT_INSTALLED"
        assert "agent-voice engines install" in body["suggestion"]
        assert "install_engine" in body["suggestion"]


# ---------------------------------------------------------------------------
# MCP_BASE_PATH output relocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_land_inside_mcp_base(tmp_path):
    """The returned path must be readable from inside the agent's sandbox."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    async with mcp_client(tmp_path, MCP_BASE_PATH=str(sandbox)) as (client, _app):
        body = payload(await client.call_tool("speak", {"text": "Sandboxed."}))
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
        body = await call_expecting_error(
            client, "clone_voice", {"name": "escape", "ref_audio_path": str(outside)}
        )
        assert body["error_code"] == "PATH_OUTSIDE_BASE"


# ---------------------------------------------------------------------------
# v0.3.3: inbox confinement for the agent-facing clone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_voice_rejects_outside_path_by_default(tmp_path):
    async with mcp_client(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="false") as (client, _app):
        for probe in ("/etc/passwd", "/proc/self/environ"):
            body = await call_expecting_error(
                client, "clone_voice", {"name": "probe", "ref_audio_path": probe}
            )
            assert body["error_code"] == "PATH_OUTSIDE_BASE"
            assert "inbox" in body["suggestion"].lower()


@pytest.mark.asyncio
async def test_clone_voice_accepts_inbox_clip(tmp_path, ref_wav):
    async with mcp_client(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="false") as (client, app):
        inbox = app.state.ctx.settings.inbox_dir
        target = inbox / "clip.wav"
        target.write_bytes(ref_wav.read_bytes())
        body = payload(
            await client.call_tool(
                "clone_voice", {"name": "inboxvoice", "ref_audio_path": str(target)}
            )
        )
        assert body["name"] == "inboxvoice"


@pytest.mark.asyncio
async def test_clone_voice_allow_any_path_permits_outside_clip(tmp_path, ref_wav):
    async with mcp_client(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="true") as (client, _app):
        body = payload(
            await client.call_tool(
                "clone_voice", {"name": "outsidevoice", "ref_audio_path": str(ref_wav)}
            )
        )
        assert body["name"] == "outsidevoice"


@pytest.mark.asyncio
async def test_clone_voice_rejects_relative_path_when_any_path_allowed(
    tmp_path, ref_wav, monkeypatch
):
    """L2: ALLOW_ANY_PATH loosens *where* a clip may live, not what a path means.

    REST has always refused a relative ``ref_audio_path``; MCP used to hand it to
    the voice store, which resolved it against the daemon's cwd. The clip below
    really does exist relative to cwd, and is still refused, because a relative
    path from an agent says nothing about the daemon's working directory.
    """
    monkeypatch.chdir(ref_wav.parent)
    async with mcp_client(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="true") as (client, _app):
        # The same file is accepted when it is named absolutely...
        absolute = payload(
            await client.call_tool(
                "clone_voice", {"name": "abs", "ref_audio_path": str(ref_wav)}
            )
        )
        assert absolute["name"] == "abs"

        # ...and rejected when it is named relative to cwd.
        body = await call_expecting_error(
            client, "clone_voice", {"name": "rel", "ref_audio_path": ref_wav.name}
        )
        assert body["error_code"] == "BAD_REQUEST"
        assert "relative ref_audio_path" in body["message"], body
        assert "absolute" in body["suggestion"].lower(), body


@pytest.mark.asyncio
async def test_server_info_includes_inbox_dir(tmp_path):
    async with mcp_client(tmp_path) as (client, app):
        info = payload(await client.call_tool("server_info", {}))
        assert info["inbox_dir"] == str(app.state.ctx.settings.inbox_dir)


@pytest.mark.asyncio
async def test_server_info_next_steps_is_empty_when_ready(tmp_path):
    """A ready server has nothing to fix: the mock engine ships preset speakers."""
    async with mcp_client(tmp_path) as (client, _app):
        info = payload(await client.call_tool("server_info", {}))
        assert info["preset_speakers"], "the mock engine advertises preset timbres"
        assert info["next_steps"] == []


@pytest.mark.asyncio
async def test_server_info_next_steps_names_engine_install_when_no_engine(tmp_path):
    async with mcp_client(tmp_path, ENGINES_DIR=tmp_path / "empty-engines") as (client, _app):
        info = payload(await client.call_tool("server_info", {}))
        assert info["installed_engines"] == []
        assert any(
            "agent-voice engines install" in step for step in info["next_steps"]
        ), info["next_steps"]


def test_next_steps_mentions_the_download_for_a_missing_default_weight():
    """An installed default engine with no cached weights is one download away."""
    from types import SimpleNamespace

    from agent_voice.mcp_server import _next_steps
    from agent_voice.schemas import EngineEntry, ModelEntry

    entry = EngineEntry(
        engine_id="qwen3_tts",
        backend_name="qwen-tts",
        backend_version="0.1.1",
        installed=True,
        models=[
            ModelEntry(
                engine_id="qwen3_tts",
                alias="1.7b-base",
                hf_repo="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                quality_tier="high",
                est_vram_gb=4.4,
            )
        ],
    )
    settings = SimpleNamespace(default_engine="qwen3_tts", inbox_dir=Path("/data/inbox"))

    steps = _next_steps([entry], settings, voices_count=1, preset_speakers=["Ryan"])
    assert len(steps) == 1, steps
    assert "agent-voice models download qwen3_tts 1.7b-base" in steps[0]
    assert "agent-voice doctor" in steps[0]


def _clone_only_engine(*, design_model: bool = False):
    """An installed default engine with nothing cloned and no preset speakers."""
    from agent_voice.schemas import EngineEntry, ModelEntry

    models = [
        ModelEntry(
            engine_id="qwen3_tts",
            alias="1.7b-base",
            hf_repo="local/qwen3-base",
            quality_tier="high",
            est_vram_gb=4.4,
            mode="clone",
        )
    ]
    if design_model:
        # Listed in the manifest but not in the local HF cache: a voice_design
        # checkpoint is *known* yet unusable until it is downloaded.
        models.append(
            ModelEntry(
                engine_id="qwen3_tts",
                alias="1.7b-design",
                hf_repo="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                quality_tier="high",
                est_vram_gb=4.4,
                mode="voice_design",
            )
        )
    return EngineEntry(
        engine_id="qwen3_tts",
        backend_name="qwen-tts",
        backend_version="0.1.1",
        installed=True,
        models=models,
    )


def test_next_steps_never_names_design_voice_on_a_clone_only_install():
    """M6: next_steps must not recommend a tool that would answer BAD_REQUEST.

    A clone-only install has no voice_design checkpoint, so ``design_voice()`` is
    exactly the dead end ``next_steps`` exists to avoid. The step must still give
    the agent something that works: clone a clip.
    """
    from types import SimpleNamespace

    from agent_voice.mcp_server import _next_steps

    settings = SimpleNamespace(default_engine="qwen3_tts", inbox_dir=Path("/data/inbox"))

    steps = _next_steps(
        [_clone_only_engine()],
        settings,
        voices_count=0,
        preset_speakers=[],
        voice_design_available=False,
    )
    assert len(steps) == 1, steps
    assert "clone_voice()" in steps[0]
    assert "design_voice()" not in steps[0], steps[0]
    assert "voice design" in steps[0], "say why the other option is missing"

    # A usable design checkpoint is the only difference; then naming it is right.
    available = _next_steps(
        [_clone_only_engine()],
        settings,
        voices_count=0,
        preset_speakers=[],
        voice_design_available=True,
    )
    assert "or call design_voice()" in available[0], available[0]


def test_next_steps_names_the_design_download_when_the_checkpoint_is_missing():
    """The clone-only suggestion must be actionable, not just a refusal."""
    from types import SimpleNamespace

    from agent_voice.mcp_server import _next_steps

    settings = SimpleNamespace(default_engine="qwen3_tts", inbox_dir=Path("/data/inbox"))
    steps = _next_steps(
        [_clone_only_engine(design_model=True)],
        settings,
        voices_count=0,
        preset_speakers=[],
        voice_design_available=False,
    )

    assert len(steps) == 1, steps
    assert "agent-voice models download qwen3_tts 1.7b-design" in steps[0], steps[0]
    assert "design_voice()" not in steps[0], steps[0]


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


# ---------------------------------------------------------------------------
# v0.2.1: tool descriptions must stay compact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_tool_descriptions_stay_within_budget(tmp_path):
    """The core surface is prompt budget an agent pays on every request.

    Detailed guidance belongs in the agent-voice://guide resource; a tool
    description should say what it does and which fields matter, then stop.
    """
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        tools = await client.list_tools()
        total = sum(len(tool.description or "") for tool in tools)
        # ~4 chars per token; 750 tokens is the budget agreed for the core set.
        assert total < 3000, (
            f"core tool descriptions are {total} chars (~{total // 4} tokens); "
            "move detail into skills/agent-voice/SKILL.md"
        )


@pytest.mark.asyncio
async def test_core_tool_schema_weight_stays_compact(tmp_path):
    """The whole core tools/list payload is prompt budget on every request.

    A 10,000-char target (~2,500 tokens) is below the structural floor of these 12
    tools: with every description removed the payload is still ~11,900 chars,
    because each tool carries a typed input schema, an output schema for its
    Pydantic result, and speak_script/start_script_job share the full BeatSpec
    schema twice. Reaching 10,000 would mean deleting required fields or the
    structured output contract, so the assertion pins the achievable weight
    instead, measured with `len(json.dumps(...))` on the core list:

    * 14,388 chars on the v0.3.3 schema set (the original pin, 14,800 ceiling);
    * 14,455 with the v0.4.0 server_info description that now names every key it
      returns;
    * 15,161 once the v0.4.0 ``markup``/``spell_identifiers``/``paragraph_gap_ms``/
      ``project`` fields landed on SpeakRequest and BeatSpec — those travel inside
      the input schemas of speak/speak_script/start_script_job, so they are real
      prompt weight the model pays, not something a shorter docstring can buy
      back;
    * 16,054 after those selectors were exposed on the MCP tool signatures and
      their one-line Args descriptions were added.
    * 20,398 with the four v0.5.0 ASR/dubbing tools (transcribe, dub_prepare,
      dub_render, start_dub_job). They add 4,348 chars — 539 of description and
      the rest input/output schemas — because a dubbing tool carries a segment
      list, a ducking parameter and an overflow report. Speech in and dubbing are
      core by design, so this is real weight the model pays, not something a
      shorter docstring can buy back.

    20,600 keeps a little headroom over the final v0.5.0 contract without
    pretending the payload shrank. The full 2,500-token target remains below the
    structural floor (including output schemas); the description budget above is
    the part that can actually be trimmed, and it still has 286 chars of slack.
    """
    async with mcp_client(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="false") as (client, _app):
        tools = await client.list_tools()
        compact = json.dumps(
            [tool.model_dump(mode="json", exclude_none=True) for tool in tools],
            separators=(",", ":"),
            default=str,
        )
        assert len(compact) <= 20_600, (
            f"core tools/list payload is {len(compact)} chars (~{len(compact) // 4} "
            "tokens); keep docstrings to 1-2 sentences and add detail to "
            "skills/agent-voice/SKILL.md"
        )


@pytest.mark.asyncio
async def test_every_tool_has_a_description(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        for tool in await client.list_tools():
            assert tool.description, f"{tool.name} has no description"


# ---------------------------------------------------------------------------
# v0.2.1: resources and prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guide_and_voices_resources_are_registered(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        uris = {str(resource.uri) for resource in await client.list_resources()}
        assert "agent-voice://voices" in uris
        assert "agent-voice://guide" in uris


@pytest.mark.asyncio
async def test_narrate_video_prompt_is_registered(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        names = {prompt.name for prompt in await client.list_prompts()}
        assert "narrate_video" in names


def test_server_info_presets_are_scoped_to_the_default_engine():
    """Only speakers the default engine can actually voice may be advertised.

    server_info() says these names are accepted by speak(speaker=...); a speaker
    from a non-default engine would make that same call fail with BAD_REQUEST.
    """
    from agent_voice.mcp_server import _preset_speech_surface
    from agent_voice.schemas import EngineEntry, ModelEntry

    def entry(engine_id: str, speaker: str) -> EngineEntry:
        return EngineEntry(
            engine_id=engine_id,
            backend_name=engine_id,
            backend_version="1",
            installed=True,
            models=[
                ModelEntry(
                    engine_id=engine_id,
                    alias=f"{engine_id}-custom",
                    hf_repo=f"local/{engine_id}-custom",
                    quality_tier="fast",
                    est_vram_gb=0.0,
                    mode="custom_voice",
                    preset_speakers=[speaker],
                )
            ],
        )

    speakers, design_available = _preset_speech_surface(
        [entry("mock", "Ryan"), entry("other", "Vivian")], "mock"
    )
    assert speakers == ["Ryan"]
    assert design_available is False


# ---------------------------------------------------------------------------
# v0.3.1: approval default, pronunciations, design candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_engine_stages_when_approval_required(tmp_path):
    async with mcp_client(
        tmp_path,
        ALLOW_AGENT_ENGINE_MUTATION="true",
        AGENT_VOICE_TOOLSET="full",
        REQUIRE_ENGINE_APPROVAL="true",
    ) as (client, _app):
        result = payload(
            await client.call_tool(
                "register_engine",
                {
                    "engine_id": "stagedeng",
                    "engine_yaml_content": (
                        "backend_name: stagedeng\n"
                        "backend_version: '1'\n"
                        "venv_python: null\n"
                        "models: []\n"
                    ),
                    "worker_py_content": "print('x')\n",
                    "install_venv": False,
                },
            )
        )
        assert result["staged"] is True
        assert result["installed"] is False
        assert result["job_id"] is None

        engines = payload(await client.call_tool("list_engines", {}))["engines"]
        assert "stagedeng" not in {entry["engine_id"] for entry in engines}


@pytest.mark.asyncio
async def test_speak_pronunciations_change_the_spoken_text(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        plain = payload(await client.call_tool("speak", {"text": "kubectl pods"}))
        rewritten = payload(
            await client.call_tool(
                "speak",
                {"text": "kubectl pods", "pronunciations": {"kubectl": "cube control"}},
            )
        )
        repeat = payload(
            await client.call_tool(
                "speak",
                {"text": "kubectl pods", "pronunciations": {"kubectl": "cube control"}},
            )
        )

        # "cube control pods" is three spoken words, not two.
        assert plain["duration_sec"] != rewritten["duration_sec"]
        assert rewritten["cached"] is False
        assert repeat["cached"] is True
        assert repeat["file_path"] == rewritten["file_path"]


@pytest.mark.asyncio
async def test_design_voice_candidates_and_save_reuses_the_take(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        result = payload(
            await client.call_tool(
                "design_voice",
                {
                    "name": "audition",
                    "instruct": "warm, low narrator",
                    "candidates": 3,
                },
            )
        )
        assert result["voice_id"] is None, "multi-candidate renders never save"
        candidates = result["candidates"]
        assert [candidate["take"] for candidate in candidates] == [1, 2, 3]
        assert len({candidate["file_path"] for candidate in candidates}) == 3

        chosen = candidates[1]
        saved = payload(
            await client.call_tool(
                "design_voice",
                {
                    "name": "audition",
                    "instruct": "warm, low narrator",
                    "take": chosen["take"],
                    "save_as_clone": True,
                },
            )
        )
        assert saved["voice_id"]
        assert saved["preview_file_path"] == chosen["file_path"], "cache must be reused"

        voices = payload(await client.call_tool("list_voices", {}))["voices"]
        assert any(voice["voice_id"] == saved["voice_id"] for voice in voices)


@pytest.mark.asyncio
async def test_design_voice_rejects_invalid_candidate_count(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(
            client,
            "design_voice",
            {"name": "audition", "instruct": "warm", "candidates": 0},
        )
        assert body["error_code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_speak_rejects_oversized_pronunciation_overrides(tmp_path):
    async with mcp_client(tmp_path) as (client, _app):
        body = await call_expecting_error(
            client,
            "speak",
            {
                "text": "hi",
                "pronunciations": {f"w{i}": "x" for i in range(501)},
            },
        )
        assert body["error_code"] == "BAD_REQUEST"
        assert "500" in body["message"]
