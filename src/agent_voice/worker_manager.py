"""GPU-aware worker lifecycle: spawn, multiplex, schedule, reap, yield.

Design constraints that shaped this file:

* **Zero VRAM at boot.** Nothing is spawned until a cache miss needs it.
* **Cache before GPU.** ``synthesize`` consults the disk cache first and can
  return without ever creating a worker. A test asserts zero spawns on a hit.
* **One GPU owner.** ``MAX_ACTIVE_ENGINES=1`` means an engine must be unloaded
  before another runs. Queue batching plus residency hysteresis stop two
  engines from ping-ponging on every interleaved request.
* **Honest failure.** A dead worker is ``WORKER_CRASHED`` unless the exit code or
  stderr actually contains an OOM signature.
* **Never buffer unbounded audio.** Each PCM stream has a bounded queue; a
  stalled consumer gets the worker cancelled rather than an ever-growing list.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio_utils import chunk_text, crossfade_stitch
from .cache import CacheEntry, SynthesisCache, build_cache_key
from .clock import Clock, get_clock
from .config import Settings
from .engine_registry import EngineRecord, EngineRegistry, model_revision
from .ipc.transport import (
    IS_WINDOWS,
    AsyncConnection,
    SyncListener,
    accept_async,
    make_transport,
)
from .schemas import AgentVoiceError, ErrorCode, ModelMode, SynthesisResult
from .voice_store import VoiceStore

logger = logging.getLogger(__name__)

_OOM_PATTERN = re.compile(
    r"CUDA out of memory|OutOfMemoryError|torch\.cuda\.OutOfMemoryError|"
    r"CUBLAS_STATUS_ALLOC_FAILED|out of memory",
    re.IGNORECASE,
)


@dataclass
class QueuedJob:
    job_id: str
    engine_id: str
    kind: str
    payload: dict[str, Any]
    future: asyncio.Future[Any]
    enqueued_at: float
    stream_id: uuid.UUID | None = None
    queue: asyncio.Queue[bytes] | None = None
    holds_lock: bool = False
    """True between taking the per-engine lock and releasing it. Makes release
    exactly-once even if a caller tears a job down from two places."""


@dataclass
class WorkerHandle:
    engine_id: str
    record: EngineRecord
    listener: SyncListener
    process: subprocess.Popen[bytes]
    connection: AsyncConnection
    hello: dict[str, Any]
    started_at: float
    last_used_at: float
    loaded_models: list[str] = field(default_factory=list)
    model_last_used: dict[str, float] = field(default_factory=dict)
    """alias -> clock time of the last start/finish of a job using it. Drives LRU
    eviction so a worker never holds more than MAX_RESIDENT_MODELS models."""
    in_flight: set[str] = field(default_factory=set)
    stream_requests: dict[uuid.UUID, str] = field(default_factory=dict)
    """stream_id -> the request id currently generating it. Without this the
    manager cannot tell which in-flight request belongs to which stream, and a
    cancel would have to abort an arbitrary job."""
    stream_task: asyncio.Task[None] | None = None

    @property
    def alive(self) -> bool:
        return self.process.poll() is None and not self.connection.closed

    def stderr_tail(self, limit: int = 2000) -> str:
        if self.process.stderr is None:
            return ""
        try:
            if self.process.poll() is None:
                return ""
            data = self.process.stderr.read() or b""
            return data.decode("utf-8", "replace")[-limit:]
        except (OSError, ValueError):
            return ""


class VRAMProbe:
    """NVML wrapper. Degrades to no-op when NVML is unavailable."""

    def __init__(self) -> None:
        self._nvml = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
        except Exception:
            self._nvml = None

    def free_gb(self) -> float | None:
        if self._nvml is None:
            return None
        try:
            handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
            info = self._nvml.nvmlDeviceGetMemoryInfo(handle)
            return info.free / 1024**3
        except Exception:
            return None

    def used_mb(self) -> int | None:
        if self._nvml is None:
            return None
        try:
            handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
            info = self._nvml.nvmlDeviceGetMemoryInfo(handle)
            return int(info.used / 1024**2)
        except Exception:
            return None


class WorkerManager:
    def __init__(
        self,
        *,
        settings: Settings,
        registry: EngineRegistry,
        cache: SynthesisCache,
        voices: VoiceStore,
        clock: Clock | None = None,
        vram: VRAMProbe | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.cache = cache
        self.voices = voices
        self.clock = clock or get_clock()
        self.vram = vram or VRAMProbe()

        self._workers: dict[str, WorkerHandle] = {}
        self._queues: dict[str, list[QueuedJob]] = {}
        self._engine_locks: dict[str, asyncio.Lock] = {}
        """engine_id -> serialisation lock. A worker process runs one GPU thread per
        request, so two concurrent RPCs against the same engine would put two
        inference loops on one model. Every synthesize/stream acquires this lock
        for the whole load_model + worker-RPC critical section; jobs that arrive
        meanwhile wait in ``_queues`` (the user-approved "wait in queue" policy:
        backpressure is a queue, not an immediate WORKER_BUSY)."""
        self._active_engine: str | None = None
        self._active_since: float = 0.0
        self._gpu_lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._running = False
        self._spawn_count = 0
        self._health_connections: dict[str, AsyncConnection] = {}
        # Optional sync housekeeping callback run on every reaper tick. The app
        # wires JobStore.prune here so job retention does not require importing
        # the job store into the GPU manager.
        self.on_reaper_tick: Callable[[], object] | None = None

    # -- lifecycle of the manager ---------------------------------------
    async def start(self) -> None:
        self._running = True
        self._reaper_task = asyncio.create_task(self._reaper_loop(), name="idle-reaper")

    async def stop(self) -> None:
        self._running = False
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper_task

        # Cancel in-flight stream tasks before killing workers, otherwise a
        # pending request sees "IPC connection lost" and reports a fake crash.
        for handle in list(self._workers.values()):
            task = handle.stream_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

        # Any caller still waiting for an engine lock is about to lose its worker;
        # tell it so rather than letting it block on a lock nobody will release.
        for jobs in self._queues.values():
            for job in list(jobs):
                if not job.future.done():
                    job.future.set_exception(
                        AgentVoiceError(
                            ErrorCode.JOB_CANCELLED,
                            "the daemon stopped before this job could run",
                            "Resubmit once the daemon is running again.",
                        )
                    )
            jobs.clear()

        for engine_id in list(self._workers):
            await self._kill_worker(engine_id, reason="shutdown")

    # -- observability --------------------------------------------------
    @property
    def spawn_count(self) -> int:
        return self._spawn_count

    def active_engine(self) -> str | None:
        return self._active_engine

    def queue_depth(self) -> int:
        """Jobs currently *waiting* for their engine lock.

        A job leaves this count the instant it takes the lock, so a single busy
        engine reports ``queue_depth() == 0`` while it works and ``> 0`` only
        while other callers are genuinely backed up behind it.
        """
        return sum(len(jobs) for jobs in self._queues.values())

    def in_flight_count(self) -> int:
        return sum(len(handle.in_flight) for handle in self._workers.values())

    def loaded_models(self) -> dict[str, list[str]]:
        return {engine_id: list(h.loaded_models) for engine_id, h in self._workers.items()}

    def last_used_at(self) -> float | None:
        if not self._workers:
            return None
        return max(handle.last_used_at for handle in self._workers.values())

    def unload_in_seconds(self) -> float | None:
        last = self.last_used_at()
        if last is None or self.in_flight_count() or self.queue_depth():
            return None
        remaining = self.settings.model_idle_ttl_seconds - (self.clock.now() - last)
        return max(0.0, remaining)

    # -- spawning -------------------------------------------------------
    def _worker_env(self, record: EngineRecord) -> dict[str, str]:
        env = dict(os.environ)
        # Workers cannot read engine.yaml themselves (they may not have PyYAML),
        # so pass the alias -> HF repo map they need to resolve a load request.
        env.update(
            {
                "AGENT_VOICE_ENGINE_ID": record.engine_id,
                "AGENT_VOICE_DATA_DIR": str(self.settings.data_dir.resolve()),
                "AGENT_VOICE_ENGINE_MODELS": json.dumps(
                    {spec.alias: spec.hf_repo for spec in record.manifest.models}
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    @staticmethod
    def _read_stderr(process: subprocess.Popen[bytes], limit: int = 2000) -> str:
        """Drain a finished process's stderr so the real error reaches the caller."""
        if process.stderr is None or process.poll() is None:
            return ""
        try:
            return (process.stderr.read() or b"").decode("utf-8", "replace")[-limit:]
        except (OSError, ValueError):
            return ""

    async def _spawn_worker(self, record: EngineRecord) -> WorkerHandle:
        self._spawn_count += 1
        runtime_dir = self.settings.data_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)

        # AF_UNIX paths are capped at ~108 bytes on Linux, and a deep data dir plus
        # a long engine id blows past that. Hash the identity so the name length
        # is constant and short, and fall back to a short /tmp root if the data
        # directory itself is already too long.
        token = hashlib.sha256(
            f"{record.engine_id}:{os.getpid()}:{uuid.uuid4().hex}".encode()
        ).hexdigest()[:16]
        socket_name = f"av-{token}.sock"
        transport = make_transport(runtime_dir / socket_name)

        max_path = 100  # conservative headroom under the 108-byte kernel limit
        if not IS_WINDOWS and len(str(runtime_dir / socket_name)) > max_path:
            runtime_dir = Path(tempfile.gettempdir()) / f"agent-voice-{os.getpid()}"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            transport = make_transport(runtime_dir / socket_name)

        listener = SyncListener(transport)
        listener.bind()

        env = self._worker_env(record)
        env.update(transport.to_env())

        creationflags = 0
        start_new_session = False
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
        else:
            start_new_session = True

        worker_path = record.directory / "worker.py"
        interpreter = record.venv_python

        try:
            process = subprocess.Popen(
                [str(interpreter), str(worker_path)],
                env=env,
                cwd=str(record.directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
        except OSError as exc:
            listener.close()
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_INSTALLED,
                f"could not start worker for '{record.engine_id}': {exc}",
                f"Verify the interpreter exists: {interpreter}",
            ) from exc

        accept_task = asyncio.create_task(
            accept_async(
                listener,
                stream_queue_factory=lambda: asyncio.Queue(
                    maxsize=self.settings.stream_queue_maxsize
                ),
            )
        )
        # Watch for an early exit so a broken worker fails fast instead of
        # making the caller wait out the full accept timeout.
        try:
            deadline = time.monotonic() + 60.0
            while not accept_task.done():
                if process.poll() is not None:
                    raise AgentVoiceError(
                        ErrorCode.WORKER_CRASHED,
                        f"worker for '{record.engine_id}' exited with code "
                        f"{process.returncode} before connecting",
                        "Check the engine venv with `agent-voice engines install "
                        f"{record.engine_id}`.",
                        details={"stderr_tail": self._read_stderr(process)},
                    )
                if time.monotonic() >= deadline:
                    raise AgentVoiceError(
                        ErrorCode.WORKER_CRASHED,
                        f"worker for '{record.engine_id}' did not connect within 60s",
                        "Check the engine venv and try `agent-voice engines install` again.",
                        details={"stderr_tail": self._read_stderr(process)},
                    )
                await asyncio.sleep(0.05)
            connection = await accept_task
        except BaseException:
            accept_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await accept_task
            process.kill()
            listener.close()
            raise

        hello = await connection.request({"id": uuid.uuid4().hex, "method": "hello"}, timeout=60.0)
        if hello.get("protocol_version") != 1:
            await connection.close()
            process.kill()
            listener.close()
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"worker speaks protocol {hello.get('protocol_version')}, expected 1",
                "Align worker.py with ipc/protocol.py.",
            )

        now = self.clock.now()
        handle = WorkerHandle(
            engine_id=record.engine_id,
            record=record,
            listener=listener,
            process=process,
            connection=connection,
            hello=hello,
            started_at=now,
            last_used_at=now,
            loaded_models=list(hello.get("loaded_models") or []),
        )
        self._workers[record.engine_id] = handle
        return handle

    async def ensure_worker(self, engine_id: str) -> WorkerHandle:
        record = self.registry.require_installed(engine_id)
        handle = self._workers.get(engine_id)
        if handle is not None and handle.alive:
            return handle
        if handle is not None:
            await self._kill_worker(engine_id, reason="dead")
        handle = await self._spawn_worker(record)
        if self._active_engine is None:
            self._active_engine = engine_id
            self._active_since = self.clock.now()
        return handle

    # -- scheduling -----------------------------------------------------
    def _engines_with_work(self) -> list[str]:
        """Engines that are waiting *or* currently running a job.

        A job leaves ``_queues`` as soon as it takes its engine lock, so the
        queue alone would make a busy engine look idle. Counting held locks too
        keeps the residency/thrashing heuristics in :meth:`_acquire_gpu` honest.
        """
        working = {engine_id for engine_id, jobs in self._queues.items() if jobs}
        working |= {engine_id for engine_id, lock in self._engine_locks.items() if lock.locked()}
        return sorted(working)

    async def _acquire_gpu(self, engine_id: str) -> None:
        """Honour MAX_ACTIVE_ENGINES with batching + residency hysteresis."""
        if self.settings.max_active_engines > 1:
            return
        async with self._gpu_lock:
            if self._active_engine is None:
                self._active_engine = engine_id
                self._active_since = self.clock.now()
                return
            if self._active_engine == engine_id:
                return

            active = self._active_engine
            # Batch: the active engine still has queued work => it keeps the GPU.
            if self._queues.get(active):
                raise AgentVoiceError(
                    ErrorCode.WORKER_BUSY,
                    f"engine '{active}' is still draining its queue",
                    "Retry shortly; requests for the active engine are served first.",
                )

            others = self._engines_with_work()
            if active in self._workers:
                residency = self.clock.now() - self._active_since
                if residency < self.settings.min_engine_residency_seconds and len(others) > 1:
                    raise AgentVoiceError(
                        ErrorCode.WORKER_BUSY,
                        f"engine '{active}' is within its minimum residency window",
                        "Retry shortly to avoid GPU thrashing.",
                    )
                await self._kill_worker(active, reason="engine switch")

            self._active_engine = engine_id
            self._active_since = self.clock.now()

    # -- queueing and one-RPC-at-a-time serialisation --------------------
    def _engine_lock(self, engine_id: str) -> asyncio.Lock:
        """Per-engine lock, created lazily so boot holds no event-loop objects."""
        lock = self._engine_locks.get(engine_id)
        if lock is None:
            lock = asyncio.Lock()
            self._engine_locks[engine_id] = lock
        return lock

    def _enqueue(self, *, engine_id: str, kind: str, payload: dict[str, Any]) -> QueuedJob:
        """Admit a job to the engine queue, or refuse it with BACKPRESSURE.

        The depth check is admission control, not a wait: an unbounded queue turns
        one stuck GPU into an ever-growing pile of callers that all time out
        together. A cache hit never reaches here — :meth:`synthesize` answers hits
        before it asks for an engine slot — so only real GPU work is ever refused.
        """
        depth = self.queue_depth()
        limit = self.settings.max_queue_depth
        if depth >= limit:
            # Raised before the QueuedJob is built, so a refused request can never
            # leave a partial entry in ``_queues`` for the reaper to trip over.
            raise AgentVoiceError(
                ErrorCode.BACKPRESSURE,
                f"queue is full ({depth}/{limit} jobs waiting)",
                "Server queue is full; retry shortly or use start_script_job.",
            )
        job = QueuedJob(
            job_id=uuid.uuid4().hex,
            engine_id=engine_id,
            kind=kind,
            payload=payload,
            future=asyncio.get_running_loop().create_future(),
            enqueued_at=self.clock.now(),
        )
        self._queues.setdefault(engine_id, []).append(job)
        return job

    def _dequeue(self, job: QueuedJob) -> None:
        jobs = self._queues.get(job.engine_id)
        if jobs is None:
            return
        with contextlib.suppress(ValueError):
            jobs.remove(job)

    async def _await_engine_lock(self, job: QueuedJob, lock: asyncio.Lock) -> None:
        """Wait for the engine lock, abandoning the wait if we are force-cancelled.

        ``unload_models(force=True)`` resolves the queued job's ``future``, so
        that future is raced against ``lock.acquire()``. Without the race a queued
        caller would sit out the whole current GPU job even though the manager had
        already decided it was cancelled.
        """
        while True:
            self._raise_if_cancelled(job)
            acquire = asyncio.ensure_future(lock.acquire())
            try:
                # Waiting on the lock *and* on cancellation at once means a
                # force-unload never has to wait for the GPU to go idle.
                await asyncio.wait({acquire, job.future}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                # The caller was cancelled (or the loop is shutting down). If our
                # acquire happened to win the lock, give it back before leaving —
                # otherwise the engine could never be used again.
                if not acquire.done():
                    acquire.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await acquire
                elif not acquire.cancelled() and lock.locked():
                    lock.release()
                raise
            acquired = acquire.done() and not acquire.cancelled()
            if job.future.done():
                if acquired:
                    lock.release()
                    acquired = False
                # Raises JOB_CANCELLED: either the exception an unload installed,
                # or ourselves if the future was merely cancelled.
                self._raise_if_cancelled(job)
            if acquired:
                return

    @staticmethod
    def _raise_if_cancelled(job: QueuedJob) -> None:
        """Surface a cancelled wait as JOB_CANCELLED instead of a silent no-op."""
        if not job.future.done():
            return
        if job.future.cancelled():
            raise AgentVoiceError(
                ErrorCode.JOB_CANCELLED,
                "job cancelled while waiting for the engine",
                "Resubmit when the GPU is free.",
            )
        # Re-raises the AgentVoiceError(ErrorCode.JOB_CANCELLED, ...) that
        # unload_models(force=true) or stop() installed on the future.
        job.future.result()
        raise AgentVoiceError(
            ErrorCode.JOB_CANCELLED,
            "job cancelled while waiting for the engine",
            "Resubmit when the GPU is free.",
        )

    @staticmethod
    def _retire_job(job: QueuedJob) -> None:
        """Drop a job's future once nobody will await it any more.

        A queued job that is cancelled during teardown would otherwise log
        "exception was never retrieved", and a job that already left the queue
        must never be resolved by a later force-unload.
        """
        if not job.future.done():
            job.future.cancel()
        elif not job.future.cancelled():
            job.future.exception()  # mark any stored exception as retrieved

    async def _begin_job(
        self,
        *,
        engine_id: str,
        model_alias: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> tuple[QueuedJob, WorkerHandle]:
        """Queue for the engine lock, take it, and prepare the worker.

        While waiting the job sits in ``_queues`` so ``queue_depth()`` reports a
        real waiter and the idle reaper leaves the worker alone. The moment the
        lock is ours the job leaves the queue: from then on it is *running*, not
        waiting, and ``in_flight`` describes it.

        Returns the job (the proof of lock ownership, passed to :meth:`_end_job`)
        and a handle with ``model_alias`` resident.
        """
        job = self._enqueue(engine_id=engine_id, kind=kind, payload=payload or {})
        lock = self._engine_lock(engine_id)
        try:
            await self._await_engine_lock(job, lock)
        except BaseException:
            self._dequeue(job)
            self._retire_job(job)
            raise

        self._dequeue(job)
        try:
            await self._acquire_gpu(engine_id)
            handle = await self.ensure_worker(engine_id)
            await self._ensure_model_loaded(handle, model_alias)
        except BaseException:
            self._retire_job(job)
            lock.release()
            raise
        job.holds_lock = True
        return job, handle

    def _end_job(self, job: QueuedJob) -> None:
        """Release the engine lock taken by :meth:`_begin_job`, exactly once."""
        self._dequeue(job)
        self._retire_job(job)
        if job.holds_lock:
            job.holds_lock = False
            lock = self._engine_lock(job.engine_id)
            if lock.locked():
                lock.release()

    @contextlib.asynccontextmanager
    async def _worker_slot(
        self,
        *,
        engine_id: str,
        model_alias: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> AsyncIterator[WorkerHandle]:
        """Hold the engine lock for the duration of the ``async with`` body.

        The body must contain exactly one worker request: that is what stops two
        GPU threads from running on the same model. The pre-yield work (spawn,
        model residency, LRU eviction) happens under the same lock because it
        mutates the worker's resident set.
        """
        job, handle = await self._begin_job(
            engine_id=engine_id, model_alias=model_alias, kind=kind, payload=payload
        )
        try:
            yield handle
        finally:
            self._end_job(job)

    async def _ensure_model_loaded(self, handle: WorkerHandle, model_alias: str) -> None:
        """Load ``model_alias``, evicting the least recently used model if needed.

        A worker keeps every loaded model resident in VRAM, so without this a
        caller cycling through aliases grows the device footprint until something
        OOMs. Eviction is requested from the worker (``unload_model``) rather than
        merely forgotten here, otherwise the memory would never come back.

        Always called with the engine lock held, so eviction cannot race with a
        request that is about to use the model being evicted.
        """
        if model_alias in handle.loaded_models:
            handle.model_last_used[model_alias] = self.clock.now()
            return

        # Evict LRU residents until there is room for the one we are about to
        # load. One eviction is enough in the steady state, but a worker can
        # report several models in its hello, so loop until the cap holds.
        while len(handle.loaded_models) >= self.settings.max_resident_models:
            # Prefer a model we have a timestamp for. Anything the worker reported
            # at hello predates our bookkeeping, so fall back to the first entry.
            stamped = [a for a in handle.loaded_models if a in handle.model_last_used]
            lru = (
                min(stamped, key=lambda alias: handle.model_last_used[alias])
                if stamped
                else handle.loaded_models[0]
            )
            reply = await handle.connection.request(
                {"id": uuid.uuid4().hex, "method": "unload_model", "model_alias": lru},
                timeout=120.0,
            )
            reported = reply.get("loaded_models")
            handle.loaded_models = (
                list(reported)
                if reported is not None
                else [alias for alias in handle.loaded_models if alias != lru]
            )
            if lru in handle.loaded_models:
                # The worker refused to drop it. Retrying would spin forever, and
                # silently pretending it is gone would understate VRAM use.
                handle.model_last_used[lru] = self.clock.now()
                break
            handle.model_last_used.pop(lru, None)

        reply = await handle.connection.request(
            {"id": uuid.uuid4().hex, "method": "load_model", "model_alias": model_alias},
            timeout=600.0,
        )
        handle.loaded_models = list(reply.get("loaded_models") or [model_alias])
        handle.model_last_used[model_alias] = self.clock.now()

    # -- public synthesis ----------------------------------------------
    @staticmethod
    def effective_seed(seed: int | None, take: int | None) -> int | None:
        """Resolve ``seed`` vs ``take`` into the seed actually sent to the worker.

        Public because ``voice_design_service`` reports the seed behind each
        candidate take: a caller must be able to re-render the take it picked.

        Precedence: an explicit ``seed`` always wins and ``take`` then only affects
        the cache key (so a caller can still ask for a distinct take of a pinned
        seed). Without ``seed``, ``take`` derives ``10007 * take`` — deterministic
        and well separated between takes, which is what makes take=2 both
        reproducible and cache-stable.
        """
        if seed is not None:
            return seed
        if take is not None:
            return 10007 * take
        return None

    @staticmethod
    def _real_time_factor(elapsed_ms: int, duration_sec: float) -> float | None:
        """Speech duration divided by wall clock: >1.0 means faster than real time.

        This is the definition the public contract states, so a render that takes
        10.5 s to produce 9.0 s of audio is 0.86 (slower than real time), and one
        that produces 1.92 s in 0.27 s is 7.1 (about 7x faster).

        ``None`` when the duration is unknown (a non-WAV cache entry written before
        the sidecar existed). On a cache hit ``elapsed_ms`` is only the lookup cost,
        so the factor is enormous — that is the truthful number for "this call",
        and it is why a caller comparing RTFs must also look at ``cached``.
        """
        if duration_sec <= 0 or elapsed_ms <= 0:
            return None
        return duration_sec / (elapsed_ms / 1000.0)

    async def synthesize(
        self,
        *,
        engine_id: str,
        model_alias: str,
        text: str,
        voice_id: str | None = None,
        ref_text: str = "",
        x_vector_only: bool = False,
        mode: ModelMode = "clone",
        speaker: str | None = None,
        instruct: str = "",
        language: str = "Auto",
        seed: int | None = None,
        speed: float = 1.0,
        response_format: str = "wav",
        take: int | None = None,
    ) -> SynthesisResult:
        """Synthesise to a file. Cache is consulted before any worker exists.

        ``take`` requests an alternate delivery of the same line. It is part of the
        cache key, so take=2 gets its own cache entry and never shadows take=1.

        ``mode``/``speaker``/``instruct`` say *how* the timbre is chosen: cloning a
        reference clip, speaking a preset timbre, or designing a voice from a
        description. They reach the cache key too, because the same text in the
        same voice is different audio once the delivery changes.

        A non-clone request carries no reference: the worker is told there is no
        ref audio, no transcript and no prompt cache, whatever the caller passed,
        so a stale ``voice_id`` cannot leak into a preset or design render.
        """
        resolved_seed = self.effective_seed(seed, take)
        started = self.clock.monotonic()
        cache_key = await self._cache_key(
            engine_id=engine_id,
            model_alias=model_alias,
            text=text,
            voice_id=voice_id,
            ref_text=ref_text,
            x_vector_only=x_vector_only,
            mode=mode,
            speaker=speaker,
            instruct=instruct,
            language=language,
            seed=resolved_seed,
            speed=speed,
            response_format=response_format,
            take=take,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            return self._cached_result(
                cached,
                cache_key=cache_key,
                engine_id=engine_id,
                model_alias=model_alias,
                voice_id=voice_id,
                mode=mode,
                speaker=speaker,
                response_format=response_format,
                started=started,
                take=take,
            )

        from .security import resolve_artifact_path

        # Deterministic id: the same request must always resolve to the same
        # artifact, both on a fresh render and on a cache hit.
        artifact_id = cache_key[:16]
        work_token = uuid.uuid4().hex[:8]
        temp_out = self.settings.artifacts_dir / f".work-{artifact_id}-{work_token}.wav"
        temp_out.parent.mkdir(parents=True, exist_ok=True)

        # Anything longer than one chunk is synthesised piecewise and stitched: a
        # single long utterance degrades quality and risks the model's context
        # limit. Short text keeps the original single-request path exactly.
        chunks = (
            chunk_text(text, max_chars=self.settings.chunk_chars)
            if len(text) > self.settings.chunk_chars
            else []
        )
        texts = [text] if not chunks else [chunk.text for chunk in chunks]
        targets = [temp_out] if not chunks else [
            temp_out.with_name(f"{temp_out.stem}-{chunk.index:03d}.wav") for chunk in chunks
        ]
        # Only a clone render has a reference voice. A custom-voice or design
        # render must tell the worker there is none, even if a caller passed a
        # voice_id, ref_text or x_vector_only flag: reusing a cloned prompt there
        # would silently speak the wrong timbre.
        is_clone = mode == "clone"
        prompt_cache_path = (
            str(self.voices.prompt_cache_paths(voice_id, engine_id, model_alias)[0])
            if is_clone and voice_id
            else None
        )
        ref_wav_path = str(self.voices.ref_wav_path(voice_id)) if is_clone and voice_id else ""
        payload_ref_text = ref_text if is_clone else ""
        payload_x_vector_only = x_vector_only if is_clone else False

        segment_paths: list[Path] = []
        replies: list[dict[str, Any]] = []

        # One engine slot for every chunk: the lock is held across the whole loop.
        # Re-acquiring it per chunk would deadlock, and releasing it would let
        # another caller interleave audio from a different job into ours.
        async with self._worker_slot(
            engine_id=engine_id,
            model_alias=model_alias,
            kind="synthesize",
            payload={"chunks": len(texts)},
        ) as handle:
            for chunk_value, chunk_out in zip(texts, targets, strict=True):
                request_id = uuid.uuid4().hex
                handle.in_flight.add(request_id)
                handle.last_used_at = self.clock.now()
                try:
                    reply = await handle.connection.request(
                        {
                            "id": request_id,
                            "method": "synthesize",
                            "model_alias": model_alias,
                            "text": chunk_value,
                            "ref_wav_path": ref_wav_path,
                            "ref_text": payload_ref_text,
                            "x_vector_only_mode": payload_x_vector_only,
                            "prompt_cache_path": prompt_cache_path,
                            "mode": mode,
                            "speaker": speaker,
                            "instruct": instruct,
                            "wav_path": str(chunk_out),
                            "language": language,
                            "seed": resolved_seed,
                            "speed": speed,
                        },
                        timeout=1800.0,
                    )
                except (ConnectionError, OSError) as exc:
                    await self._handle_worker_death(handle, exc)
                    raise
                finally:
                    handle.in_flight.discard(request_id)
                    handle.last_used_at = self.clock.now()

                if reply.get("error_code"):
                    raise AgentVoiceError(
                        ErrorCode(reply["error_code"]),
                        reply.get("message", "synthesis failed"),
                        reply.get("suggestion", ""),
                    )
                segment_paths.append(Path(reply["wav_path"]))
                replies.append(reply)

            # Stamp on completion as well as on start: a long job must not look
            # stale to the LRU while it is the one being used.
            handle.model_last_used[model_alias] = self.clock.now()
            hello = handle.hello

        produced = (
            segment_paths[0]
            if len(segment_paths) == 1
            else temp_out.with_name(f".work-{artifact_id}-{work_token}-stitched.wav")
        )
        reply = replies[0]
        try:
            if len(segment_paths) > 1:
                crossfade_stitch(segment_paths, produced)

            # The stitched master's real length is what the caller must be told:
            # the per-chunk durations overlap by the crossfade, so summing them
            # would drift steadily longer than the file.
            duration_sec = (
                float(reply.get("duration_sec", 0.0))
                if len(segment_paths) == 1
                else self._wav_duration(produced)
            )
            sample_rate = int(reply.get("sample_rate", 24_000))
            chunks_used = len(chunks) if chunks else 1

            final = resolve_artifact_path(
                artifact_id,
                f".{response_format}",
                artifacts_dir=self.settings.artifacts_dir,
                mcp_base_path=self.settings.mcp_base_path,
            )
            if response_format == "wav":
                final.parent.mkdir(parents=True, exist_ok=True)
                final.write_bytes(produced.read_bytes())
            else:
                from .audio_utils import convert_audio

                convert_audio(
                    produced,
                    final,
                    response_format=response_format,
                    normalize_lufs=(
                        self.settings.normalize_lufs if response_format != "pcm" else None
                    ),
                )

            # The cache key covers the ORIGINAL full text, so a repeat of this call
            # is a single hit no matter how many chunks produced the audio.
            self.cache.put(
                cache_key,
                final,
                format=response_format,
                extra_meta={
                    "duration_sec": duration_sec,
                    "sample_rate": sample_rate,
                    "chunks_used": chunks_used,
                },
            )
        finally:
            # Never leave intermediate WAVs behind, whether we succeeded or the
            # stitch/convert step raised.
            for path in {*segment_paths, produced}:
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)

        elapsed_ms = self._elapsed_ms(started)
        return SynthesisResult(
            artifact_id=artifact_id,
            file_path=str(final),
            duration_sec=duration_sec,
            sample_rate=sample_rate,
            format=response_format,
            engine_id=engine_id,
            model_alias=model_alias,
            backend_name=reply.get("backend_name", hello.get("backend_name", "")),
            backend_version=reply.get("backend_version", hello.get("backend_version", "")),
            # A preset or designed render has no reference voice to report, even
            # if the caller passed an ignored voice_id.
            voice_id=voice_id if is_clone else None,
            mode=mode,
            speaker=speaker,
            cached=False,
            download_url=f"/audio/{artifact_id}",
            chunks_used=chunks_used,
            elapsed_ms=elapsed_ms,
            take=take,
            real_time_factor=self._real_time_factor(elapsed_ms, duration_sec),
        )

    def _cached_result(
        self,
        cached: CacheEntry,
        *,
        cache_key: str,
        engine_id: str,
        model_alias: str,
        voice_id: str | None,
        response_format: str,
        started: float,
        take: int | None = None,
        mode: ModelMode = "clone",
        speaker: str | None = None,
    ) -> SynthesisResult:
        """Materialise a cache hit. Never touches the GPU.

        Duration/sample-rate/chunk-count come from the sidecar the producer wrote,
        so an mp3/opus/flac hit does not need ffprobe and never reports 0.0 s. The
        probe is only a fallback for entries written before that metadata existed,
        and it only works for WAV.
        """
        from .security import resolve_artifact_path

        record = self.registry.get(engine_id)
        artifact_id = cache_key[:16]
        target = resolve_artifact_path(
            artifact_id,
            cached.path.suffix,
            artifacts_dir=self.settings.artifacts_dir,
            mcp_base_path=self.settings.mcp_base_path,
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(cached.path.read_bytes())

        meta = cached.extra_meta
        duration_meta = meta.get("duration_sec")
        if duration_meta is not None:
            duration_sec = float(duration_meta)
        elif cached.path.suffix == ".wav":
            duration_sec = self._wav_duration(cached.path)
        else:
            duration_sec = 0.0

        elapsed_ms = self._elapsed_ms(started)
        return SynthesisResult(
            artifact_id=artifact_id,
            file_path=str(target),
            duration_sec=duration_sec,
            sample_rate=int(meta.get("sample_rate") or 24_000),
            format=response_format,
            engine_id=engine_id,
            model_alias=model_alias,
            backend_name=record.manifest.backend_name,
            backend_version=record.manifest.backend_version,
            voice_id=voice_id if mode == "clone" else None,
            mode=mode,
            speaker=speaker,
            cached=True,
            download_url=f"/audio/{artifact_id}",
            chunks_used=int(meta.get("chunks_used") or 1),
            elapsed_ms=elapsed_ms,
            take=take,
            # A hit only paid the lookup, so this factor is huge by construction.
            real_time_factor=self._real_time_factor(elapsed_ms, duration_sec),
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int(round((self.clock.monotonic() - started) * 1000)))

    async def stream(
        self,
        *,
        engine_id: str,
        model_alias: str,
        text: str,
        voice_id: str | None = None,
        ref_text: str = "",
        x_vector_only: bool = False,
        mode: ModelMode = "clone",
        speaker: str | None = None,
        instruct: str = "",
        language: str = "Auto",
        seed: int | None = None,
        speed: float = 1.0,
        take: int | None = None,
    ) -> tuple[uuid.UUID, asyncio.Queue[bytes], str]:
        """Start a PCM stream. Returns (stream_id, queue, streaming_mode).

        The queue is bounded. A consumer that stops draining it eventually hits
        ``BACKPRESSURE`` and the worker is cancelled rather than buffering audio
        without limit.

        ``mode``/``speaker``/``instruct`` mirror :meth:`synthesize` so a preset or
        designed voice streams too.

        ``take`` is accepted for call-site symmetry with :meth:`synthesize`, but a
        stream has no cache entry and no ``SynthesisResult`` to report it on, so it
        cannot select a stored alternate delivery. It still derives the seed (see
        :meth:`effective_seed`), so take=2 does stream different audio.
        """
        resolved_seed = self.effective_seed(seed, take)
        # Hold the engine lock until the streaming RPC has been sent and its
        # request id registered, then hand the lock to the background task, which
        # releases it when generation ends. That keeps "one GPU RPC per engine"
        # true for streams as well as for file synthesis.
        job, handle = await self._begin_job(
            engine_id=engine_id, model_alias=model_alias, kind="stream"
        )

        stream_id = uuid.uuid4()
        queue = handle.connection.open_stream(stream_id)
        request_id = uuid.uuid4().hex
        handle.last_used_at = self.clock.now()

        streaming_mode = (
            "native" if handle.hello.get("capabilities", {}).get("streaming") else "emulated"
        )

        # Same rule as synthesize: only a clone render has a reference voice.
        is_clone = mode == "clone"
        ref_wav_path = str(self.voices.ref_wav_path(voice_id)) if is_clone and voice_id else ""
        payload_ref_text = ref_text if is_clone else ""
        payload_x_vector_only = x_vector_only if is_clone else False

        started = asyncio.Event()

        async def run() -> None:
            try:
                handle.in_flight.add(request_id)
                # Record the mapping before the RPC so cancel_stream() can target
                # this exact request, and so an early consumer cancel never has
                # to guess which in-flight request belongs to which stream.
                handle.stream_requests[stream_id] = request_id
                started.set()
                try:
                    reply = await handle.connection.request(
                        {
                            "id": request_id,
                            "method": "stream_pcm",
                            "stream_id": str(stream_id),
                            "model_alias": model_alias,
                            "text": text,
                            "ref_wav_path": ref_wav_path,
                            "ref_text": payload_ref_text,
                            "x_vector_only_mode": payload_x_vector_only,
                            "prompt_cache_path": None,
                            "mode": mode,
                            "speaker": speaker,
                            "instruct": instruct,
                            "language": language,
                            "seed": resolved_seed,
                            "speed": speed,
                        },
                        timeout=1800.0,
                    )
                    if reply.get("error_code") == ErrorCode.JOB_CANCELLED:
                        return
                except asyncio.CancelledError:
                    raise
                except (ConnectionError, OSError) as exc:
                    await self._handle_worker_death(handle, exc)
                finally:
                    handle.in_flight.discard(request_id)
                    handle.stream_requests.pop(stream_id, None)
                    handle.last_used_at = self.clock.now()
                    handle.model_last_used[model_alias] = self.clock.now()
                    # Push the end-of-stream sentinel so consumers stop immediately
                    # instead of waiting out their read timeout and mistaking a
                    # clean finish for backpressure.
                    self._close_stream(queue)
                    handle.connection.close_stream(stream_id)
            finally:
                self._end_job(job)

        try:
            handle.stream_task = asyncio.create_task(run())
            # Wait until the request is registered so callers can immediately
            # observe it via health/gpu_status, and so unload(force=false) sees it
            # as busy.
            await started.wait()
        except BaseException:
            # Setup failed before the background task owned the job: release the
            # engine lock here so the whole engine does not wedge.
            self._end_job(job)
            raise
        return stream_id, queue, streaming_mode

    async def cancel_stream(self, engine_id: str, stream_id: uuid.UUID) -> bool:
        handle = self._workers.get(engine_id)
        if handle is None or not handle.alive:
            return False
        # Target this stream's own request. Cancelling "whatever is in flight"
        # would abort an unrelated job sharing the engine.
        request_id = handle.stream_requests.get(stream_id, "")
        if not request_id:
            return False
        try:
            reply = await handle.connection.request(
                {
                    "id": uuid.uuid4().hex,
                    "method": "cancel",
                    "target_request_id": request_id,
                },
                timeout=5.0,
            )
        except (ConnectionError, OSError, TimeoutError):
            return False
        if handle.stream_task is not None and handle.stream_requests.get(stream_id) == request_id:
            handle.stream_task.cancel()
        # Wake any consumer blocked on the queue for this stream.
        queue = handle.connection._streams.get(stream_id)
        if queue is not None:
            self._close_stream(queue)
        return bool(reply.get("cancelled"))

    # -- unload / reaping ----------------------------------------------
    async def unload_models(self, *, force: bool = False) -> dict[str, Any]:
        active = self.in_flight_count()
        queued = self.queue_depth()

        if (active or queued) and not force:
            raise AgentVoiceError(
                ErrorCode.WORKER_BUSY,
                f"{active} active and {queued} queued job(s) in progress",
                "Retry with force=true to cancel active jobs and free VRAM immediately.",
            )

        cancelled = 0
        if force:
            for handle in list(self._workers.values()):
                for request_id in list(handle.in_flight):
                    with contextlib.suppress(Exception):
                        await handle.connection.request(
                            {
                                "id": uuid.uuid4().hex,
                                "method": "cancel",
                                "target_request_id": request_id,
                            },
                            timeout=2.0,
                        )
                        cancelled += 1
                task = handle.stream_task
                if task is not None and not task.done():
                    task.cancel()
            # Resolve everyone still waiting for the engine lock with
            # JOB_CANCELLED. Their callers are blocked on this future, so this is
            # what turns a queued job into an error instead of a silent hang.
            for jobs in self._queues.values():
                for job in jobs:
                    if not job.future.done():
                        job.future.set_exception(
                            AgentVoiceError(
                                ErrorCode.JOB_CANCELLED,
                                "job cancelled by unload_models(force=true)",
                                "Resubmit when the GPU is free.",
                            )
                        )
                jobs.clear()

        unloaded = list(self._workers)
        for engine_id in unloaded:
            await self._kill_worker(engine_id, reason="manual unload")
        self._active_engine = None
        return {"unloaded": unloaded, "cancelled_jobs": cancelled}

    async def _kill_worker(self, engine_id: str, *, reason: str) -> None:
        handle = self._workers.pop(engine_id, None)
        if handle is None:
            return

        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                handle.connection.request(
                    {"id": uuid.uuid4().hex, "method": "shutdown"}, timeout=3.0
                ),
                3.0,
            )
        with contextlib.suppress(Exception):
            await handle.connection.close()

        process = handle.process
        if process.poll() is None:
            process.terminate()
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, process.wait, 3.0
                )
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(Exception):
                    await asyncio.get_running_loop().run_in_executor(
                        None, process.wait, 3.0
                    )
        handle.listener.close()
        if reason == "engine switch" and self._active_engine == engine_id:
            self._active_engine = None

    async def _handle_worker_death(
        self, handle: WorkerHandle, exc: BaseException
    ) -> None:
        stderr = handle.stderr_tail()
        await self._kill_worker(handle.engine_id, reason="crash")
        if not self._running:
            # Shutdown races are not crashes; surfacing WORKER_CRASHED here would
            # be a lie that confuses callers during a clean stop.
            raise AgentVoiceError(
                ErrorCode.JOB_CANCELLED,
                "the daemon shut down while this job was running",
                "Resubmit once the daemon is running again.",
            ) from exc
        if _OOM_PATTERN.search(stderr) or _OOM_PATTERN.search(str(exc)):
            raise AgentVoiceError(
                ErrorCode.CUDA_OOM,
                "the worker ran out of GPU memory",
                "Unload other GPU applications, use the 0.6b model, or raise MIN_FREE_VRAM_GB.",
                details={"stderr_tail": stderr},
            ) from exc
        raise AgentVoiceError(
            ErrorCode.WORKER_CRASHED,
            f"worker for '{handle.engine_id}' died: {exc}",
            "Retry; the worker is respawned lazily on the next request.",
            details={"stderr_tail": stderr},
        ) from exc

    async def _reaper_loop(self) -> None:
        interval = max(1.0, min(30.0, self.settings.model_idle_ttl_seconds / 10.0))
        while self._running:
            try:
                await asyncio.sleep(interval)
                if not self._running:
                    break
                # Housekeeping runs on every tick, including while work is in
                # flight — _reap_once is about GPUs and bails when busy, but a
                # stale job record has nothing to do with the GPU.
                if self.on_reaper_tick is not None:
                    try:
                        self.on_reaper_tick()
                    except Exception:
                        logger.warning("reaper housekeeping hook failed", exc_info=True)
                await self._reap_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                continue

    async def _reap_once(self) -> None:
        if self.in_flight_count() or self.queue_depth():
            return
        now = self.clock.now()
        for engine_id, handle in list(self._workers.items()):
            idle_for = now - handle.last_used_at
            if idle_for >= self.settings.model_idle_ttl_seconds:
                await self._kill_worker(engine_id, reason="idle ttl")
                if self._active_engine == engine_id:
                    self._active_engine = None

        if self.settings.enable_vram_pressure_yield and not self.in_flight_count():
            free = self.vram.free_gb()
            if free is not None and free < self.settings.min_free_vram_gb:
                for engine_id in list(self._workers):
                    await self._kill_worker(engine_id, reason="vram pressure")
                self._active_engine = None

    # -- helpers --------------------------------------------------------
    @staticmethod
    def _close_stream(queue: asyncio.Queue[bytes]) -> None:
        """Signal end-of-stream with an empty chunk, without ever blocking.

        An empty chunk is the sentinel consumers use to stop. It must be
        non-blocking: if the queue is already full the consumer is stalled and
        will hit its own backpressure timeout.
        """
        try:
            queue.put_nowait(b"")
        except asyncio.QueueFull:
            pass

    async def _cache_key(
        self,
        *,
        engine_id: str,
        model_alias: str,
        text: str,
        voice_id: str | None,
        ref_text: str,
        x_vector_only: bool,
        language: str,
        seed: int | None,
        speed: float,
        response_format: str,
        take: int | None = None,
        mode: ModelMode = "clone",
        speaker: str | None = None,
        instruct: str = "",
    ) -> str:
        record = self.registry.get(engine_id)
        # Which weights this alias resolves to belongs in the key: re-pointing an
        # alias, or pulling a new snapshot of the same repo, must never serve the
        # old model's audio. An unknown alias is not an error here — the worker
        # reports MODEL_NOT_FOUND with a proper suggestion once it is asked to load
        # it, and the key still has to be computable to get that far.
        try:
            spec = self.registry.model_spec(engine_id, model_alias)
            hf_repo = spec.hf_repo
            revision = model_revision(hf_repo)
        except AgentVoiceError:
            hf_repo = ""
            revision = "unknown"

        # Only a clone render depends on the reference. Normalising the other
        # fields for preset/design modes keeps the key aligned with what is
        # actually sent to the worker, so a stray ref_text cannot fork the cache.
        is_clone = mode == "clone"
        keyed_voice_id = (voice_id or "") if is_clone else ""
        keyed_ref_text = ref_text if is_clone else ""
        keyed_x_vector_only = x_vector_only if is_clone else False
        ref_sha = ""
        if keyed_voice_id:
            from .voice_store import sha256_file

            ref_path = self.voices.ref_wav_path(keyed_voice_id)
            if ref_path.is_file():
                ref_sha = sha256_file(ref_path)
        return build_cache_key(
            text=text,
            voice_id=keyed_voice_id,
            ref_wav_sha256=ref_sha,
            ref_text=keyed_ref_text,
            x_vector_only_mode=keyed_x_vector_only,
            engine_id=engine_id,
            model_alias=model_alias,
            backend_name=record.manifest.backend_name,
            backend_version=record.manifest.backend_version,
            language=language,
            seed=seed,
            speed=speed,
            response_format=response_format,
            normalize_lufs=self.settings.normalize_lufs,
            chunk_chars=self.settings.chunk_chars,
            take=take,
            mode=mode,
            speaker=speaker or "",
            instruct=instruct or "",
            hf_repo=hf_repo,
            model_revision=revision,
        )

    @staticmethod
    def _wav_duration(path: Path) -> float:
        try:
            import soundfile as sf

            info = sf.info(str(path))
            return float(info.frames) / float(info.samplerate)
        except Exception:
            return 0.0

    async def gpu_status(self) -> dict[str, Any]:
        loaded = self.loaded_models()
        return {
            "active_engine": self._active_engine,
            "loaded_models": [f"{k}:{m}" for k, v in loaded.items() for m in v],
            "workers": {
                engine_id: ("alive" if handle.alive else "dead")
                for engine_id, handle in self._workers.items()
            },
            "vram_used_mb": self.vram.used_mb(),
            "vram_free_mb": (
                int(self.vram.free_gb() * 1024) if self.vram.free_gb() is not None else None
            ),
            "queue_depth": self.queue_depth(),
            "in_flight": self.in_flight_count(),
            "unload_in_seconds": self.unload_in_seconds(),
            "last_used_at": self.last_used_at(),
        }
