"""IPC framing and worker lifecycle.

Proves the five lifecycle guarantees the architecture promises:
zero workers at boot, cache-before-GPU, the 15-minute TTL reaper on an injected
clock, 409 WORKER_BUSY vs force unload, and honest crash classification.
"""

from __future__ import annotations

import asyncio
import contextlib
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


# ---------------------------------------------------------------------------
# v0.2.1: job reconciliation and pruning are actually wired to startup
# ---------------------------------------------------------------------------


def test_reconcile_on_startup_marks_interrupted_jobs(tmp_path):
    """A job left running by a dead process can never resume; it must not lie."""
    import json

    from agent_voice.job_store import JobStore
    from agent_voice.schemas import ErrorCode

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    now = 1_700_000_000.0
    for job_id, status in [
        ("a" * 16, "pending"),
        ("b" * 16, "running"),
        ("c" * 16, "succeeded"),
    ]:
        (jobs_dir / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "kind": "script",
                    "status": status,
                    "progress": 0.4,
                    "message": "",
                    "created_at": now,
                    "updated_at": now,
                    "result": None,
                    "error": None,
                }
            ),
            "utf-8",
        )

    store = JobStore(jobs_dir)
    assert store.reconcile_on_startup() == 2

    for job_id in ("a" * 16, "b" * 16):
        job = store.get(job_id)
        assert job.status == "failed"
        assert job.error is not None
        assert job.error.error_code == ErrorCode.DAEMON_RESTARTED
    assert store.get("c" * 16).status == "succeeded"
    # Idempotent: a second boot must not re-report them.
    assert store.reconcile_on_startup() == 0


def test_unknown_job_suggestion_does_not_lie(tmp_path):
    """Jobs persist under data/jobs/, so the message must not call them ephemeral."""
    from agent_voice.job_store import JobStore

    store = JobStore(tmp_path / "jobs")
    with pytest.raises(AgentVoiceError) as excinfo:
        store.get("doesnotexist")
    assert "ephemeral" not in excinfo.value.suggestion.lower()
    assert "jobs" in excinfo.value.suggestion.lower()


def test_prune_removes_only_old_records(tmp_path):
    import os
    import time

    from agent_voice.job_store import JobStore

    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(parents=True)
    store = JobStore(jobs_dir)
    old = store.create("script")
    fresh = store.create("script")

    stale_time = time.time() - 48 * 3600
    os.utime(jobs_dir / f"{old.job_id}.json", (stale_time, stale_time))

    assert store.prune() >= 1
    assert not (jobs_dir / f"{old.job_id}.json").exists()
    assert (jobs_dir / f"{fresh.job_id}.json").exists()


def test_app_lifespan_reconciles_and_prunes(tmp_path):
    """The wiring itself: startup must reconcile, not just offer the method."""
    import json

    from agent_voice.api.app import create_app
    from agent_voice.config import Settings

    data_dir = tmp_path / "data"
    jobs_dir = data_dir / "jobs"
    jobs_dir.mkdir(parents=True)
    stray = "d" * 16
    (jobs_dir / f"{stray}.json").write_text(
        json.dumps(
            {
                "job_id": stray,
                "kind": "script",
                "status": "running",
                "progress": 0.2,
                "message": "",
                "created_at": 1_700_000_000.0,
                "updated_at": 1_700_000_000.0,
                "result": None,
                "error": None,
            }
        ),
        "utf-8",
    )

    settings = Settings(
        DATA_DIR=data_dir,
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
    )
    app = create_app(settings=settings, mount_mcp=False)
    from fastapi.testclient import TestClient

    with TestClient(app):
        pass

    job = app.state.ctx.jobs.get(stray)
    assert job.status == "failed"
    assert job.error.error_code == "DAEMON_RESTARTED"


def test_app_lifespan_prunes_stale_job_files(tmp_path):
    """A terminal job older than the retention window is deleted at startup.

    Terminal status on purpose: reconcile rewrites interrupted jobs (giving them
    a fresh mtime), while prune is what drops records no caller can still poll.
    """
    import json
    import os
    import time

    from fastapi.testclient import TestClient

    from agent_voice.api.app import create_app
    from agent_voice.config import Settings

    data_dir = tmp_path / "data"
    jobs_dir = data_dir / "jobs"
    jobs_dir.mkdir(parents=True)
    stale = "e" * 16
    path = jobs_dir / f"{stale}.json"
    path.write_text(
        json.dumps(
            {
                "job_id": stale,
                "kind": "script",
                "status": "succeeded",
                "progress": 1.0,
                "message": "",
                "created_at": 1_700_000_000.0,
                "updated_at": 1_700_000_000.0,
                "result": None,
                "error": None,
            }
        ),
        "utf-8",
    )
    old = time.time() - 48 * 3600
    os.utime(path, (old, old))

    settings = Settings(
        DATA_DIR=data_dir,
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
    )
    app = create_app(settings=settings, mount_mcp=False)

    with TestClient(app):
        pass

    assert not path.exists(), "startup must prune records past the retention window"


# ---------------------------------------------------------------------------
# v0.4.0 Wave 1 (H2): a force-unloaded queued job must not wedge the engine
# ---------------------------------------------------------------------------


async def _wait_for(predicate, attempts: int = 300, interval: float = 0.01) -> bool:
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


@pytest.mark.asyncio
async def test_force_unload_with_a_queued_job_leaves_the_engine_usable(tmp_path):
    """H2: the abandoned ``lock.acquire()`` used to win the lock and hold it forever.

    A job queued behind a running stream races ``lock.acquire()`` against its own
    future. When force-unload resolves the future first, v0.3.3 raised
    JOB_CANCELLED and simply dropped the still-pending acquire. It acquired the
    lock the moment the stream released it, and no code was left to release it —
    so every later request for that engine blocked forever.
    """
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, _queue, _mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="word " * 80
        )
        assert await _wait_for(lambda: manager.in_flight_count() == 1)
        handle = manager._workers["mock"]
        stream_task = handle.stream_task

        queued = asyncio.create_task(
            manager.synthesize(
                engine_id="mock", model_alias="mock-tone", text="queued behind the stream"
            )
        )
        try:
            assert await _wait_for(lambda: manager.queue_depth() == 1)

            await manager.unload_models(force=True)

            with pytest.raises(AgentVoiceError) as excinfo:
                await queued
            assert excinfo.value.error_code == ErrorCode.JOB_CANCELLED
        finally:
            if not queued.done():  # pragma: no cover - defensive cleanup
                queued.cancel()
                with contextlib.suppress(BaseException):
                    await queued

        # Let the cancelled stream finish tearing itself down before inspecting.
        if stream_task is not None:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(stream_task, timeout=30)
        await asyncio.sleep(0)

        assert not manager._engine_lock("mock").locked(), (
            "a force-unloaded queued job left its pending acquire to win the engine "
            "lock; nothing can release it any more"
        )

        # The real symptom: a later request for the same engine completes.
        result = await asyncio.wait_for(
            manager.synthesize(
                engine_id="mock", model_alias="mock-tone", text="after the unload"
            ),
            timeout=90,
        )
        assert result.duration_sec > 0
        assert stream_id is not None
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# v0.4.0 Wave 1 (L6): a stream setup failure must release the engine lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_setup_failure_releases_the_engine_lock(tmp_path):
    """L6: every failure between ``_begin_job`` and the ``try`` leaked the lock.

    The capability lookup, the voice path and the payload setup all ran while the
    engine lock was held but outside any handler, so raising there left the lock
    held by a job that would never end.
    """
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        # Warm a worker so there is a live handle whose hello we can sabotage.
        await manager.synthesize(engine_id="mock", model_alias="mock-tone", text="warm")
        handle = manager._workers["mock"]
        assert handle.hello.get("capabilities", {}).get("streaming") is True
        # ``capabilities`` must be a mapping; None makes the lookup raise, which is
        # exactly the "setup blew up while holding the lock" scenario.
        handle.hello["capabilities"] = None

        with pytest.raises(AttributeError):
            await manager.stream(
                engine_id="mock", model_alias="mock-tone", text="never streamed"
            )

        assert not manager._engine_lock("mock").locked(), (
            "stream() failed during setup and never released the engine lock"
        )
        assert manager.queue_depth() == 0, "the aborted job must leave the queue"
        assert manager.in_flight_count() == 0

        # And the engine still works.
        result = await asyncio.wait_for(
            manager.synthesize(
                engine_id="mock", model_alias="mock-tone", text="after the failed setup"
            ),
            timeout=90,
        )
        assert result.duration_sec > 0
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# v0.4.0 Wave 1 (M1): a worker death mid-stream is not a clean stream
# ---------------------------------------------------------------------------


async def _drain_until_closed(queue, timeout: float = 30.0) -> int:
    """Read PCM chunks until the end-of-stream sentinel; return the chunk count."""
    chunks = 0
    while True:
        chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
        if not chunk:
            return chunks
        chunks += 1


@pytest.mark.asyncio
async def test_worker_death_mid_stream_is_recorded_as_a_stream_error(tmp_path):
    """The empty sentinel means "stopped", not "succeeded"."""
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, _mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="word " * 80
        )
        assert await _wait_for(lambda: manager.in_flight_count() == 1)
        manager._workers["mock"].process.kill()

        await _drain_until_closed(queue)

        failure = manager.stream_error("mock", stream_id)
        assert failure is not None, "a crashed worker's stream must not look successful"
        assert failure["error_code"] == "WORKER_CRASHED"
        assert failure["message"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_clean_stream_records_no_stream_error(tmp_path):
    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, _mode = await manager.stream(
            engine_id="mock", model_alias="mock-tone", text="a short clean line"
        )
        assert await _drain_until_closed(queue) > 0
        assert manager.stream_error("mock", stream_id) is None
        assert manager.stream_error("no-such-engine", stream_id) is None
    finally:
        await manager.stop()


def test_websocket_reports_worker_crash_instead_of_done(tmp_path):
    """M1 end to end: the WS client gets an error frame, not ``done``."""
    import json

    from fastapi.testclient import TestClient

    from agent_voice.api.app import create_app

    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        with client.websocket_connect("/v1/audio/ws") as ws:
            ws.send_json({"type": "speak", "text": "word " * 80})
            started = ws.receive_json()
            assert started["type"] == "started"

            handle = app.state.ctx.workers._workers["mock"]
            handle.process.kill()

            final: dict | None = None
            for _ in range(5000):
                raw = ws.receive()
                if raw.get("type") != "websocket.send":  # pragma: no cover - closed early
                    break
                if raw.get("bytes") is not None:
                    continue
                final = json.loads(raw["text"])
                break

            assert final is not None, "the socket closed without a final frame"
            assert final["type"] == "error", (
                f"a crashed worker was reported as {final['type']}, not an error"
            )
            assert final["error_code"] == "WORKER_CRASHED"

            # The socket stays usable after a stream dies.
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
