"""v0.3.1 parallel-agent load: concurrency, cache isolation and queue admission.

The guarantees under test:
* many agents may hit the daemon at once; reads and cache hits do not wait behind
  the GPU lock, and uncached jobs queue safely (never two decodes on one engine);
* a cache hit stays fast while another request is synthesising on the GPU;
* a full wait queue returns a clean 429 instead of growing without bound.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from agent_voice.api.app import create_app
from agent_voice.config import Settings
from agent_voice.mcp_server import build_mcp_server
from agent_voice.schemas import AgentVoiceError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]
LONG_TEXT = "word " * 200


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
        MAX_QUEUE_DEPTH=64,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_twenty_concurrent_mixed_requests_are_clean(tmp_path):
    from fastmcp import Client

    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx
        mcp = build_mcp_server(ctx)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            warm = await client.post("/v1/agent/speak", json={"text": "warm cache"})
            assert warm.status_code == 200

            peak = 0
            stop = asyncio.Event()

            async def monitor() -> None:
                nonlocal peak
                while not stop.is_set():
                    peak = max(peak, ctx.workers.in_flight_count())
                    await asyncio.sleep(0.005)

            async def http_call(index: int) -> httpx.Response:
                if index % 4 == 0:
                    return await client.post(
                        "/v1/agent/speak", json={"text": "warm cache"}
                    )
                if index % 4 == 1:
                    return await client.post(
                        "/v1/agent/speak", json={"text": f"uncached line {index}"}
                    )
                if index % 4 == 2:
                    return await client.get("/v1/gpu")
                return await client.get("/v1/voices")

            await asyncio.sleep(0)
            monitor_task = asyncio.create_task(monitor())
            try:
                async with Client(mcp) as mcp_client:
                    results = await asyncio.gather(
                        *(http_call(i) for i in range(18)),
                        mcp_client.call_tool("gpu_status", {}),
                        mcp_client.call_tool("list_voices", {}),
                    )
            finally:
                stop.set()
                await monitor_task

            http_results = results[:18]
            mcp_results = results[18:]
            assert all(response.status_code == 200 for response in http_results)
            assert all(
                not getattr(result, "is_error", False) for result in mcp_results
            )
            assert peak <= 1, "one engine must never run two decodes at once"
            assert ctx.workers.in_flight_count() == 0
            assert ctx.workers.queue_depth() == 0


@pytest.mark.asyncio
async def test_cache_hit_is_fast_while_the_gpu_is_busy(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx

        warm = await ctx.workers.synthesize(
            engine_id="mock", model_alias="mock-tone", text="cached line"
        )
        assert warm.cached is False

        busy = asyncio.create_task(
            ctx.workers.synthesize(
                engine_id="mock", model_alias="mock-tone", text=LONG_TEXT
            )
        )
        try:
            for _ in range(200):
                if ctx.workers.in_flight_count() == 1:
                    break
                await asyncio.sleep(0.01)
            assert ctx.workers.in_flight_count() == 1

            started = time.perf_counter()
            hit = await ctx.workers.synthesize(
                engine_id="mock", model_alias="mock-tone", text="cached line"
            )
            elapsed_ms = (time.perf_counter() - started) * 1000

            assert hit.cached is True
            assert elapsed_ms < 50, (
                f"cache hit took {elapsed_ms:.1f} ms while the GPU was busy; "
                "the lookup must not wait behind the engine lock"
            )
        finally:
            await busy

        assert ctx.workers.in_flight_count() == 0


@pytest.mark.asyncio
async def test_full_queue_returns_backpressure_without_corruption(tmp_path):
    settings = _settings(tmp_path, MAX_QUEUE_DEPTH=1)
    app = create_app(settings=settings, mount_mcp=False)
    async with app.router.lifespan_context(app):
        ctx = app.state.ctx

        running = asyncio.create_task(
            ctx.workers.synthesize(
                engine_id="mock", model_alias="mock-tone", text=LONG_TEXT
            )
        )
        queued: asyncio.Task | None = None
        try:
            for _ in range(200):
                if ctx.workers.in_flight_count() == 1:
                    break
                await asyncio.sleep(0.01)
            assert ctx.workers.in_flight_count() == 1

            queued = asyncio.create_task(
                ctx.workers.synthesize(
                    engine_id="mock", model_alias="mock-tone", text="second job"
                )
            )
            for _ in range(200):
                if ctx.workers.queue_depth() == 1:
                    break
                await asyncio.sleep(0.01)
            assert ctx.workers.queue_depth() == 1

            with pytest.raises(AgentVoiceError) as excinfo:
                await ctx.workers.synthesize(
                    engine_id="mock", model_alias="mock-tone", text="third job"
                )
            assert excinfo.value.error_code == ErrorCode.BACKPRESSURE
            assert excinfo.value.http_status == 429
            assert "queue is full" in excinfo.value.message.lower()
        finally:
            await running
            if queued is not None:
                await queued

        assert ctx.workers.queue_depth() == 0
        assert ctx.workers.in_flight_count() == 0
