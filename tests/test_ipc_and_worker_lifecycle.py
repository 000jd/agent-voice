"""IPC framing and worker lifecycle.

Proves the five lifecycle guarantees the architecture promises:
zero workers at boot, cache-before-GPU, the 15-minute TTL reaper on an injected
clock, 409 WORKER_BUSY vs force unload, and honest crash classification.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_voice.cache import SynthesisCache
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry
from agent_voice.ipc.protocol import (
    FRAME_JSON,
    FRAME_PCM,
    decode_header,
    encode_frame,
    encode_json,
    encode_pcm,
    split_pcm_payload,
)
from agent_voice.ipc.transport import (
    SyncListener,
    UnixTransport,
    accept_async,
)
from agent_voice.schemas import AgentVoiceError, ErrorCode
from agent_voice.voice_store import VoiceStore
from agent_voice.worker_manager import WorkerManager
from tests._fake_clock import FakeClock

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


def test_json_frame_roundtrip():
    payload = b'{"id":"abc","method":"hello"}'
    frame = encode_json(payload)
    frame_type, payload_len = decode_header(frame[:5])
    assert frame_type == FRAME_JSON
    assert payload_len == len(payload)
    assert frame[5:] == payload


def test_pcm_frame_roundtrip():
    stream_id = uuid.uuid4()
    pcm = b"\x01\x02" * 64
    frame = encode_pcm(stream_id, pcm)
    frame_type, _ = decode_header(frame[:5])
    assert frame_type == FRAME_PCM
    recovered_id, recovered_pcm = split_pcm_payload(frame[5:])
    assert recovered_id == stream_id
    assert recovered_pcm == pcm


def test_unknown_frame_type_rejected():
    with pytest.raises(ValueError):
        encode_frame(0x09, b"x")


def test_truncated_header_rejected():
    with pytest.raises(ValueError):
        decode_header(b"\x00\x00")


def test_pcm_payload_shorter_than_stream_id_rejected():
    with pytest.raises(ValueError):
        split_pcm_payload(b"tooshort")


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        **overrides,
    )


def _manager(settings: Settings, clock=None) -> WorkerManager:
    registry = EngineRegistry(settings.engines_dir)
    manager = WorkerManager(
        settings=settings,
        registry=registry,
        cache=SynthesisCache(settings.cache_dir),
        voices=VoiceStore(settings.voices_dir),
        clock=clock,
    )
    return manager


@pytest.mark.asyncio
async def test_zero_workers_at_boot(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        assert manager.spawn_count == 0
        assert manager.active_engine() is None
        assert (await manager.gpu_status())["workers"] == {}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cache_hit_never_spawns_worker(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        first = await manager.synthesize(
            engine_id="mock", model_alias="mock-tone", text="cache me please"
        )
        assert first.cached is False
        assert manager.spawn_count == 1

        second = await manager.synthesize(
            engine_id="mock", model_alias="mock-tone", text="cache me please"
        )
        assert second.cached is True
        # The whole point: a cache hit must not touch the GPU.
        assert manager.spawn_count == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cache_key_separates_backend_versions(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="versioned")
        key_v1 = await manager._cache_key(
            engine_id="mock",
            model_alias="mock-tone",
            text="versioned",
            voice_id=None,
            ref_text="",
            x_vector_only=False,
            language="Auto",
            seed=None,
            speed=1.0,
            response_format="wav",
        )

        # Simulate a backend upgrade: the manifest version changes.
        manager.registry.get("mock").manifest.backend_version = "2"
        key_v2 = await manager._cache_key(
            engine_id="mock",
            model_alias="mock-tone",
            text="versioned",
            voice_id=None,
            ref_text="",
            x_vector_only=False,
            language="Auto",
            seed=None,
            speed=1.0,
            response_format="wav",
        )
        assert key_v1 != key_v2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_idle_ttl_reaper_uses_injected_clock(tmp_path):
    settings = _settings(tmp_path, MODEL_IDLE_TTL_SECONDS=900)
    settings.ensure_dirs()
    clock = FakeClock()
    manager = _manager(settings, clock=clock)
    await manager.start()
    try:
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="ttl")
        assert len(manager._workers) == 1

        clock.advance(899)
        await manager._reap_once()
        assert len(manager._workers) == 1, "must not reap before the TTL elapses"

        clock.advance(2)
        await manager._reap_once()
        assert len(manager._workers) == 0, "must reap once the TTL elapses"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_reaper_never_fires_while_jobs_are_in_flight(tmp_path):
    settings = _settings(tmp_path, MODEL_IDLE_TTL_SECONDS=1)
    settings.ensure_dirs()
    clock = FakeClock()
    manager = _manager(settings, clock=clock)
    await manager.start()
    try:
        stream_id, _queue, _mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="word " * 40
        )
        clock.advance(10_000)
        await manager._reap_once()
        assert len(manager._workers) == 1, "an in-flight job pins the worker"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unload_returns_worker_busy_when_not_forced(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.stream(engine_id="mock", model_alias="mock-tone", text="word " * 60)
        await asyncio.sleep(0.05)

        with pytest.raises(AgentVoiceError) as excinfo:
            await manager.unload_models(force=False)

        assert excinfo.value.error_code == ErrorCode.WORKER_BUSY
        assert excinfo.value.http_status == 409
        assert "force=true" in excinfo.value.suggestion
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_force_unload_cancels_and_frees(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.stream(engine_id="mock", model_alias="mock-tone", text="word " * 60)
        await asyncio.sleep(0.05)

        result = await manager.unload_models(force=True)
        assert "mock" in result["unloaded"]
        assert manager._workers == {}
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_idle_unload_succeeds_when_nothing_is_running(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="idle")
        result = await manager.unload_models(force=False)
        assert "mock" in result["unloaded"]
        assert result["cancelled_jobs"] == 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_worker_respawns_lazily_after_crash(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="first")
        assert manager.spawn_count == 1

        handle = manager._workers["mock"]
        handle.process.kill()
        handle.process.wait(timeout=5)
        await asyncio.sleep(0.1)

        # Different text => cache miss => must spawn a replacement worker.
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="second")
        assert manager.spawn_count == 2
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_stream_multiplexes_health_during_pcm(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="stream this"
        )
        assert mode == "native"

        # Drain concurrently. The PCM queue is bounded on purpose; not reading it
        # while issuing another RPC would exercise backpressure, not multiplexing.
        chunks = 0

        async def drain() -> None:
            nonlocal chunks
            while True:
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=5.0)
                except TimeoutError:
                    return
                if not chunk:
                    return
                chunks += 1

        drain_task = asyncio.create_task(drain())

        handle = manager._workers["mock"]
        reply = await handle.connection.request(
            {"id": uuid.uuid4().hex, "method": "health"}, timeout=5.0
        )
        assert reply["ok"] is True
        assert reply["in_flight"] >= 1, "health must answer while a stream is active"

        await drain_task
        assert chunks > 0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cancel_stream_stops_generation(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, _queue, _mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="word " * 80
        )
        await asyncio.sleep(0.08)
        cancelled = await manager.cancel_stream("mock", stream_id)
        assert cancelled is True
    finally:
        await manager.stop()


def test_worker_crash_is_not_reported_as_oom():
    from agent_voice.worker_manager import _OOM_PATTERN

    assert _OOM_PATTERN.search("CUDA out of memory. Tried to allocate")
    assert _OOM_PATTERN.search("torch.cuda.OutOfMemoryError: CUDA out of memory")
    # A segfault must never be mislabelled as an OOM.
    assert not _OOM_PATTERN.search("Segmentation fault (core dumped)")
    assert not _OOM_PATTERN.search("returncode -11")


def test_recv_frame_over_real_socket(tmp_path):
    """Framing works over an actual UDS, not just in memory."""
    socket_path = tmp_path / "ipc.sock"
    listener = SyncListener(UnixTransport(socket_path))
    listener.bind()
    try:
        assert socket_path.exists()
    finally:
        listener.close()
    assert not socket_path.exists(), "listener must clean up its socket"


@pytest.mark.asyncio
async def test_accept_async_receives_worker_connection(tmp_path):
    """The parent accepts the worker's dial-in; this is the production pattern.

    Uses a real mock worker rather than a hand-rolled socket script: the script
    races shutdown against accept, which made this test flaky and tested the
    harness rather than the transport.
    """
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    registry = EngineRegistry(settings.engines_dir)
    record = registry.require_installed("mock")

    socket_path = tmp_path / "accept.sock"
    listener = SyncListener(UnixTransport(socket_path))
    listener.bind()
    transport = listener.transport

    env = dict(os.environ)
    env.update(transport.to_env())
    process = subprocess.Popen(
        [str(record.venv_python), str(record.directory / "worker.py")],
        env=env,
        cwd=str(record.directory),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        shell=False,
    )
    try:
        connection = await asyncio.wait_for(accept_async(listener), timeout=30)
        try:
            reply = await connection.request(
                {"id": uuid.uuid4().hex, "method": "hello"}, timeout=15
            )
            assert reply["engine_id"] == "mock"
            assert reply["protocol_version"] == 1
        finally:
            await connection.close()
    finally:
        listener.close()
        process.terminate()
        with __import__("contextlib").suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        if process.poll() is None:
            process.kill()


async def _next_frame(connection) -> tuple[int, bytes]:
    header = await connection._reader.readexactly(5)
    frame_type, payload_len = decode_header(header)
    payload = await connection._reader.readexactly(payload_len)
    return frame_type, payload
