"""Kokoro engine conformance: manifest, zero VRAM, synthesis, streaming, refusals.

Every engine-backed test here talks to the *real* install under
``engines/kokoro``: its own ``.venv`` interpreter, the 327 MB checkpoint in the
shared HuggingFace cache, and the frozen IPC v1 transport the daemon speaks. A
bare CI runner has neither the venv nor the weights, so those tests skip; the
manifest and cache-layout tests stay runnable so the declared contract is still
checked on a machine that cannot run the engine at all.

Nothing here needs a GPU. Kokoro is CPU-only by construction, and the one test
that consults ``nvidia-smi`` asserts absence of a compute process, never
presence of one.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import uuid
from pathlib import Path

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry, hf_model_downloaded
from agent_voice.hardware import pair_level
from agent_voice.ipc.transport import SyncListener, accept_async, make_transport
from agent_voice.kokoro_support import KOKORO_OPENAI_VOICES, KOKORO_PRESET_SPEAKERS
from agent_voice.schemas import SAMPLE_RATE

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINES_DIR = REPO_ROOT / "engines"
ENGINE_DIR = ENGINES_DIR / "kokoro"
ENGINE_PYTHON = ENGINE_DIR / ".venv" / "bin" / "python"
ENGINE_ID = "kokoro"
MODEL_ALIAS = "kokoro-82m"
HF_REPO = "hexgrad/Kokoro-82M"
CHECKPOINT_FILE = "kokoro-v1_0.pth"
"""The only weight file Kokoro ships: a ``.pth``, not a safetensors blob."""

MISSING_REPO = "hexgrad/definitely-not-a-real-kokoro-repo"
"""A repo id that cannot resolve, used to exercise MODEL_NOT_FOUND offline."""

DOWNLOADS_COMMAND = f"agent-voice models download {ENGINE_ID} {MODEL_ALIAS}"

PRESET_SPEAKERS = (
    "af_heart",
    "af_bella",
    "af_sarah",
    "am_adam",
    "am_michael",
    "bf_emma",
    "bm_george",
    "bm_lewis",
)
"""The eight timbres ``engine.yaml`` advertises, in manifest order."""

TWO_CHUNK_TEXT = "Streaming check, one two.\nAnd a second chunk after the cancel."
"""Measured on this host: two KPipeline chunks of ~2 s each, ~1 s to render.

Two chunks is what makes the cancel test meaningful — the audio is still being
produced when the cancel arrives, and the worker has a chunk boundary to stop
at. The newline is Kokoro's own split point, not a trick: ``KPipeline`` splits
its input on newlines, so this is exactly the "two chunk" request v1 describes.
"""

SAMPLE_TEXT = "Hello from Kokoro."
IPC_TIMEOUT = 180.0
"""Per-request ceiling. Loading the checkpoint takes ~2.5 s on a warm cache."""

_ENGINE_READY = ENGINE_PYTHON.is_file() and (ENGINE_DIR / "worker.py").is_file()
_WEIGHTS_READY = hf_model_downloaded(HF_REPO)

requires_engine = pytest.mark.skipif(
    not _ENGINE_READY,
    reason=f"kokoro engine venv is not installed ({ENGINE_PYTHON} missing)",
)
requires_kokoro = pytest.mark.skipif(
    not (_ENGINE_READY and _WEIGHTS_READY),
    reason=(
        f"kokoro engine is not installed ({ENGINE_PYTHON}) or {HF_REPO} is not in "
        "the local HuggingFace cache"
    ),
)


# ---------------------------------------------------------------------------
# Direct IPC driver
# ---------------------------------------------------------------------------


class KokoroWorker:
    """A kokoro worker process driven over IPC v1, without the daemon manager.

    ``WorkerManager`` layers scheduling, caching and residency on top of exactly
    this protocol. Speaking it directly is what lets these tests assert on
    per-chunk PCM, on the ``JOB_CANCELLED`` reply, and on a worker whose model
    map deliberately points at a repo that is not on this machine — none of
    which the manager exposes.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        thread: threading.Thread,
        connection,
        listener: SyncListener,
        process: subprocess.Popen,
        hello: dict,
        workdir: Path,
        stderr_path: Path,
        stderr_file,
    ) -> None:
        self.loop = loop
        self.thread = thread
        self.connection = connection
        self.listener = listener
        self.process = process
        self.hello = hello
        self.workdir = workdir
        self.stderr_path = stderr_path
        self._stderr_file = stderr_file
        self.load_reply: dict | None = None
        """The `load_model` reply the module fixture captured at startup."""

    # -- spawning ------------------------------------------------------
    @classmethod
    def spawn(
        cls,
        *,
        models: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> KokoroWorker:
        """Bind a socket, start this engine's interpreter, and complete hello.

        The socket lives in a short temp directory: AF_UNIX paths are capped at
        ~108 bytes and a pytest ``tmp_path`` is long enough to blow that limit.
        """
        workdir = Path(tempfile.mkdtemp(prefix="av-kokoro-"))
        transport = make_transport(workdir / "ipc.sock")
        listener = SyncListener(transport)
        listener.bind()

        env = dict(os.environ)
        env.update(transport.to_env())
        # Workers cannot read engine.yaml, so the daemon hands them the map.
        env["AGENT_VOICE_ENGINE_MODELS"] = json.dumps(models or {MODEL_ALIAS: HF_REPO})
        env["PYTHONUNBUFFERED"] = "1"
        env.update(extra_env or {})

        stderr_path = workdir / "worker.stderr.log"
        stderr_file = stderr_path.open("wb")
        process = subprocess.Popen(
            [str(ENGINE_PYTHON), str(ENGINE_DIR / "worker.py")],
            cwd=str(ENGINE_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            shell=False,
            start_new_session=True,
        )

        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, name="kokoro-ipc", daemon=True)
        thread.start()
        try:
            connection = asyncio.run_coroutine_threadsafe(
                accept_async(listener, stream_queue_factory=lambda: asyncio.Queue()),
                loop,
            ).result(60.0)
            hello = cls._call(
                connection, {"id": uuid.uuid4().hex, "method": "hello"}, loop, 60.0
            )
        except BaseException:
            listener.close()
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10.0)
            stderr_file.close()
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10.0)
            loop.close()
            shutil.rmtree(workdir, ignore_errors=True)
            raise

        return cls(
            loop=loop,
            thread=thread,
            connection=connection,
            listener=listener,
            process=process,
            hello=hello,
            workdir=workdir,
            stderr_path=stderr_path,
            stderr_file=stderr_file,
        )

    # -- talking to it --------------------------------------------------
    @staticmethod
    def _call(connection, message: dict, loop, timeout: float) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            connection.request(message, timeout=timeout), loop
        )
        return future.result(timeout + 30.0)

    def request(self, payload: dict, *, timeout: float = IPC_TIMEOUT) -> dict:
        """Send one JSON-RPC frame and return its reply (errors included)."""
        return self._call(
            self.connection, {"id": uuid.uuid4().hex, **payload}, self.loop, timeout
        )

    def run(self, coroutine, *, timeout: float = IPC_TIMEOUT + 30.0):
        """Run a caller-written coroutine on this worker's event loop."""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    def stream(self, payload: dict, *, timeout: float = IPC_TIMEOUT):
        """Run ``stream_pcm`` to completion; return ``(pcm_chunks, reply)``.

        Every PCM frame is dispatched by the reader task before the terminal
        JSON frame resolves the request future — same socket, same order — so by
        the time the reply is in hand the queue holds all of the audio.
        """

        async def exchange():
            stream_id = uuid.uuid4()
            queue = self.connection.open_stream(stream_id)
            try:
                reply = await self.connection.request(
                    {
                        "id": uuid.uuid4().hex,
                        "method": "stream_pcm",
                        "stream_id": str(stream_id),
                        **payload,
                    },
                    timeout=timeout,
                )
                chunks = []
                while True:
                    try:
                        chunks.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                return chunks, reply
            finally:
                self.connection.close_stream(stream_id)

        return self.run(exchange(), timeout=timeout + 30.0)

    def stderr_tail(self, limit: int = 4000) -> str:
        """The worker's stderr so far: the crash report when something dies."""
        try:
            return self.stderr_path.read_text("utf-8", errors="replace")[-limit:]
        except OSError:
            return ""

    # -- teardown ------------------------------------------------------
    async def _drain_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in asyncio.all_tasks() if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def close(self) -> None:
        """Shut the worker down politely, then reclaim everything it owned."""
        with contextlib.suppress(Exception):
            self.request({"method": "shutdown"}, timeout=30.0)
        try:
            self.process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            self.process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10.0)
        with contextlib.suppress(Exception):
            self.run(self.connection.close(), timeout=30.0)
        with contextlib.suppress(Exception):
            self.run(self._drain_tasks(), timeout=30.0)
        self.listener.close()
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10.0)
        with contextlib.suppress(Exception):
            self.loop.close()
        self._stderr_file.close()
        shutil.rmtree(self.workdir, ignore_errors=True)


@pytest.fixture(scope="module")
def kokoro_worker():
    """One warm worker for the whole module: spawn once, load the model once."""
    if not (_ENGINE_READY and _WEIGHTS_READY):
        pytest.skip("kokoro engine venv or weights are not installed on this host")
    worker = KokoroWorker.spawn()
    try:
        reply = worker.request(
            {"method": "load_model", "model_alias": MODEL_ALIAS}, timeout=300.0
        )
        assert reply.get("error_code") is None, f"{reply}\n{worker.stderr_tail()}"
        worker.load_reply = reply
        yield worker
    finally:
        worker.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=ENGINES_DIR,
        AGENT_VOICE_DEFAULT_ENGINE=ENGINE_ID,
        MCP_BASE_PATH="",
    )
    base.update(overrides)
    return Settings(**base)


def _speech_payload(**overrides) -> dict:
    """A complete synthesize/stream_pcm body: exactly what routing sends."""
    payload = {
        "model_alias": MODEL_ALIAS,
        "text": SAMPLE_TEXT,
        "ref_wav_path": "",
        "ref_text": "",
        "x_vector_only_mode": False,
        "prompt_cache_path": None,
        "mode": "custom_voice",
        "speaker": "af_heart",
        "instruct": "",
        "language": "Auto",
        "seed": None,
        "speed": 1.0,
    }
    payload.update(overrides)
    return payload


def _assert_pcm16_wav(path: Path) -> float:
    """Assert a 24 kHz mono PCM16 WAV with audio in it; return its duration."""
    assert path.is_file(), f"no audio written at {path}"
    info = sf.info(str(path))
    assert info.samplerate == SAMPLE_RATE
    assert info.channels == 1
    assert info.subtype == "PCM_16"
    assert info.frames > 0, "WAV has no frames"
    data, _rate = sf.read(str(path), dtype="int16", always_2d=False)
    assert bool(data.any()), "WAV is pure digital silence"
    return info.frames / SAMPLE_RATE


def _compute_apps() -> list[str] | None:
    """Processes holding GPU memory, or None when nvidia-smi cannot answer."""
    if shutil.which("nvidia-smi") is None:
        return None
    result = subprocess.run(
        [
            "nvidia-smi",
            "--query-compute-apps=pid,process_name,used_memory",
            "--format=csv,noheader",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


def test_manifest_registers_kokoro_with_the_declared_capabilities():
    """The manifest contract, readable without the engine installed."""
    registry = EngineRegistry(ENGINES_DIR)
    assert ENGINE_ID in registry.ids()

    entry = next(item for item in registry.list_entries() if item.engine_id == ENGINE_ID)
    assert entry.display_name.startswith("Kokoro")
    assert entry.backend_name == "kokoro"
    assert entry.streaming is True
    assert entry.pcm_native is True
    assert entry.custom_voice is True
    assert entry.voice_design is False
    assert entry.instruct_control is False
    # EngineEntry has no clone flag: cloning is a manifest capability, and this
    # engine has neither a reference encoder nor a prompt cache.
    assert registry.get(ENGINE_ID).manifest.capabilities.voice_clone is False

    spec = registry.model_spec(ENGINE_ID, MODEL_ALIAS)
    assert spec.hf_repo == HF_REPO
    assert spec.mode == "custom_voice"
    assert spec.supports_instruct is False
    assert spec.preset_speakers == list(PRESET_SPEAKERS)
    assert len(spec.preset_speakers) == 8, "exactly the eight advertised timbres"
    assert len(set(spec.preset_speakers)) == 8, "no duplicated speaker names"

    # The OpenAI compatibility map must stay inside the advertised presets, or a
    # builtin `voice=` name would resolve to a timbre this engine cannot speak.
    assert set(KOKORO_OPENAI_VOICES.values()) <= set(PRESET_SPEAKERS)
    assert set(KOKORO_PRESET_SPEAKERS) == set(PRESET_SPEAKERS)


def test_hf_model_downloaded_recognises_a_pth_checkpoint(tmp_path, monkeypatch):
    """The `.pth` fix, isolated from this host's cache.

    Kokoro ships one 327 MB ``.pth`` and no safetensors, so a weights check that
    only looked for ``.safetensors``/``.bin`` reported a fully working install as
    "not downloaded" — which in turn refused the OpenAI preset fallback.
    """
    snapshot = (
        tmp_path
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--hexgrad--Kokoro-82M"
        / "snapshots"
        / ("0" * 40)
    )
    snapshot.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert hf_model_downloaded(HF_REPO) is False, "an empty snapshot is not a download"
    (snapshot / CHECKPOINT_FILE).write_bytes(b"\0" * 4096)
    assert hf_model_downloaded(HF_REPO) is True


@requires_kokoro
def test_manifest_reports_installed_engine_and_downloaded_weights():
    registry = EngineRegistry(ENGINES_DIR)
    entry = next(item for item in registry.list_entries() if item.engine_id == ENGINE_ID)

    assert entry.installed is True, entry.why_unavailable
    assert entry.why_unavailable is None
    assert entry.venv_python is not None
    assert Path(entry.venv_python) == ENGINE_PYTHON.absolute()
    assert Path(entry.venv_python).is_file()
    # Deliberately not resolved: uv makes .venv/bin/python a symlink to the base
    # interpreter, and spawning the target would bypass the venv (no torch).
    assert ".venv" in str(entry.venv_python)

    model = next(item for item in entry.models if item.alias == MODEL_ALIAS)
    assert model.downloaded_locally is True
    assert hf_model_downloaded(HF_REPO) is True

    snapshots = (
        Path.home() / ".cache" / "huggingface" / "hub" / "models--hexgrad--Kokoro-82M"
    ) / "snapshots"
    checkpoints = list(snapshots.glob(f"*/{CHECKPOINT_FILE}"))
    assert checkpoints, f"no {CHECKPOINT_FILE} under {snapshots}"
    assert checkpoints[0].stat().st_size > 0


# ---------------------------------------------------------------------------
# CPU-only: zero VRAM
# ---------------------------------------------------------------------------


def test_manifest_declares_the_device_matrix():
    """The manifest is the resolver's contract, readable without the engine.

    v0.6.0 turned the runtime device into a real choice, but Kokoro's *verified*
    pair is cpu: `device: cpu` is the scheduling class (its absence was the
    v0.5.0 bug that let a 0 MB engine take the single-accelerator slot), `cpu` is
    the default, and cuda/mps are declared as experimental.
    """
    manifest = EngineRegistry(ENGINES_DIR).get(ENGINE_ID).manifest

    assert manifest.device == "cpu"
    assert manifest.default_device == "cpu"
    assert list(manifest.supported_devices) == ["cpu", "cuda", "mps"]
    assert pair_level(ENGINE_ID, "cpu") == "verified"
    assert pair_level(ENGINE_ID, "cuda") == "experimental"
    assert pair_level(ENGINE_ID, "mps") == "experimental"


@requires_engine
def test_engine_venv_torch_sees_no_cuda():
    script = (
        "import json, torch;"
        "print(json.dumps({'available': torch.cuda.is_available(),"
        " 'cuda': torch.version.cuda,"
        " 'devices': torch.cuda.device_count()}))"
    )
    result = subprocess.run(
        [str(ENGINE_PYTHON), "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        shell=False,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {"available": False, "cuda": None, "devices": 0}


@requires_kokoro
def test_worker_hello_declares_cpu_and_zero_vram(kokoro_worker):
    hello = kokoro_worker.hello
    assert hello["protocol_version"] == 1
    assert hello["engine_id"] == ENGINE_ID
    assert hello["backend_name"] == "kokoro"
    assert hello["backend_version"] == "0.9.4"
    assert hello["gpu_vram_mb"] == 0
    assert hello["device"] == "cpu"
    assert hello["sample_rate"] == SAMPLE_RATE
    assert hello["mode"] == "custom_voice"
    assert hello["preset_speakers"] == list(PRESET_SPEAKERS)
    assert hello["capabilities"] == {
        "streaming": True,
        "pcm_native": True,
        "voice_clone": False,
        "custom_voice": True,
        "voice_design": False,
        "instruct_control": False,
    }


@requires_kokoro
def test_load_model_reports_zero_vram(kokoro_worker):
    reply = kokoro_worker.load_reply
    assert reply["model_alias"] == MODEL_ALIAS
    assert reply["loaded_models"] == [MODEL_ALIAS]
    assert reply["gpu_vram_mb"] == 0
    assert reply["device"] == "cpu"


# -- the non-zero path, mocked: never against the real CPU venv --------------


class _FakeCuda:
    """Records every `torch.cuda.*` call so the cpu path can prove it made none."""

    def __init__(self, *, available: bool, allocated_bytes: int) -> None:
        self.available = available
        self.allocated_bytes = allocated_bytes
        self.calls: list[str] = []

    def is_available(self) -> bool:
        self.calls.append("is_available")
        return self.available

    def memory_allocated(self) -> int:
        self.calls.append("memory_allocated")
        return self.allocated_bytes

    def empty_cache(self) -> None:
        self.calls.append("empty_cache")


class _FakeTorch(types.ModuleType):
    def __init__(self, *, cuda_available: bool, allocated_bytes: int) -> None:
        super().__init__("torch")
        self.cuda = _FakeCuda(available=cuda_available, allocated_bytes=allocated_bytes)
        self.float32 = object()
        self.bfloat16 = object()


class _RecordingConnection:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_json(self, payload: dict) -> None:
        self.sent.append(dict(payload))

    def send_pcm(self, *args: object) -> dict:  # pragma: no cover - not expected
        raise AssertionError("no PCM frame expected here")


@pytest.fixture
def kokoro_worker_module(monkeypatch: pytest.MonkeyPatch):
    """Load `engines/kokoro/worker.py` by path under a test-owned env.

    The worker reads ``AGENT_VOICE_RESOLVED_DEVICE`` once, at import, so each
    variant needs its own module object — and the real venv is never involved.
    """
    loaded: list[str] = []

    def _load(device: str):
        monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", device)
        # The cpu path empties this before torch is imported; record the key so
        # teardown restores whatever this machine had.
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
        name = f"av_kokoro_worker_{device}"
        spec = importlib.util.spec_from_file_location(name, ENGINE_DIR / "worker.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded.append(name)
        spec.loader.exec_module(module)
        return module

    yield _load
    for name in loaded:
        sys.modules.pop(name, None)


def test_mocked_cuda_reports_a_real_vram_number(kokoro_worker_module, monkeypatch):
    """`gpu_vram_mb` must be *measured* off the cpu path, not hard-coded to 0.

    Only the allocator is mocked: the worker's own device plumbing, its hello and
    its health reply are the shipped code, so a regression in `_vram_mb()`'s cuda
    branch fails here instead of on a GPU somebody has to own.
    """
    fake = _FakeTorch(cuda_available=True, allocated_bytes=64 * 1024**2)
    monkeypatch.setitem(sys.modules, "torch", fake)
    worker = kokoro_worker_module("cuda")

    assert worker.RESOLVED_DEVICE == "cuda"
    conn = _RecordingConnection()
    engine = worker.KokoroEngine(conn, {MODEL_ALIAS: HF_REPO})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    hello, health = conn.sent
    assert hello["device"] == "cuda"
    assert hello["gpu_vram_mb"] == 64
    assert health["device"] == "cuda"
    assert health["gpu_vram_mb"] == 64
    assert fake.cuda.calls == ["is_available", "memory_allocated"] * 2


def test_mocked_cpu_reports_zero_and_never_touches_torch_cuda(
    kokoro_worker_module, monkeypatch
):
    """The real CPU assertions above, restated as a mock that can catch a leak."""
    fake = _FakeTorch(cuda_available=False, allocated_bytes=0)
    monkeypatch.setitem(sys.modules, "torch", fake)
    worker = kokoro_worker_module("cpu")

    assert worker.RESOLVED_DEVICE == "cpu"
    # The cpu path empties CUDA_VISIBLE_DEVICES before torch could read it.
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    conn = _RecordingConnection()
    engine = worker.KokoroEngine(conn, {MODEL_ALIAS: HF_REPO})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    assert [reply["gpu_vram_mb"] for reply in conn.sent] == [0, 0]
    assert [reply["device"] for reply in conn.sent] == ["cpu", "cpu"]
    assert fake.cuda.calls == [], "a cpu worker must never probe CUDA"


@requires_kokoro
def test_no_compute_process_on_the_gpu_during_a_render(kokoro_worker, tmp_path):
    """A live render must not put the worker on a CUDA context.

    Sampled, not assumed: the render runs on a thread while ``nvidia-smi`` is
    polled, which is the only way to observe the process *while* it computes.
    A host with no nvidia-smi (or one that cannot answer) skips: this asserts an
    absence, so it must never become a false failure on a CPU-only box.
    """
    baseline = _compute_apps()
    if baseline is None:
        pytest.skip("nvidia-smi is unavailable, so GPU residency cannot be observed")

    wav = tmp_path / "no-gpu.wav"
    outcome: dict = {}

    def render() -> None:
        outcome["reply"] = kokoro_worker.request(
            {"method": "synthesize", "wav_path": str(wav), **_speech_payload()}
        )

    worker_thread = threading.Thread(target=render, name="kokoro-render")
    worker_thread.start()
    samples: list[list[str]] = []
    while worker_thread.is_alive() and len(samples) < 20:
        samples.extend([_compute_apps() or []])
        time.sleep(0.05)
    worker_thread.join(timeout=120.0)
    assert not worker_thread.is_alive(), "the render never finished"

    reply = outcome["reply"]
    assert reply.get("error_code") is None, f"{reply}\n{kokoro_worker.stderr_tail()}"
    assert reply["gpu_vram_mb"] == 0

    pid = str(kokoro_worker.process.pid)
    for sample in samples:
        assert not any(pid == line.split(",")[0].strip() for line in sample), (
            f"worker pid {pid} appeared as a GPU compute process: {sample}"
        )
    if not baseline:
        # Nothing held GPU memory before the render either, so every sample of a
        # CPU-only engine must also find the compute-application list empty.
        assert all(sample == [] for sample in samples), samples


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


@requires_kokoro
def test_synthesize_writes_24khz_mono_pcm16(kokoro_worker, tmp_path):
    wav = tmp_path / "hello.wav"
    reply = kokoro_worker.request(
        {"method": "synthesize", "wav_path": str(wav), **_speech_payload()}
    )
    assert reply.get("error_code") is None, f"{reply}\n{kokoro_worker.stderr_tail()}"

    assert reply["speaker"] == "af_heart"
    assert reply["mode"] == "custom_voice"
    assert reply["model_alias"] == MODEL_ALIAS
    assert reply["backend_name"] == "kokoro"
    assert reply["backend_version"] == "0.9.4"
    assert reply["sample_rate"] == SAMPLE_RATE
    assert reply["gpu_vram_mb"] == 0
    assert reply["duration_sec"] > 0.0
    assert reply["chunks_used"] >= 1

    duration = _assert_pcm16_wav(wav)
    # duration_sec is produced_frames / 24000, so the file and the report agree.
    assert duration == pytest.approx(reply["duration_sec"], abs=1e-6)
    assert duration > 0.2


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@requires_kokoro
def test_stream_pcm_delivers_native_chunks(kokoro_worker):
    chunks, reply = kokoro_worker.stream(_speech_payload(text=TWO_CHUNK_TEXT))
    assert reply.get("error_code") is None, f"{reply}\n{kokoro_worker.stderr_tail()}"

    assert len(chunks) == 2, "one PCM frame per KPipeline chunk"
    assert reply["chunks_used"] == len(chunks)
    assert reply["streaming_mode"] == "native"
    assert reply["sample_rate"] == SAMPLE_RATE
    assert reply["speaker"] == "af_heart"
    assert reply["mode"] == "custom_voice"
    assert reply["gpu_vram_mb"] == 0
    assert all(len(chunk) % 2 == 0 for chunk in chunks), "int16 frames are 2 bytes"

    total_bytes = sum(len(chunk) for chunk in chunks)
    assert total_bytes > 0
    assert reply["duration_sec"] == pytest.approx(total_bytes / 2 / SAMPLE_RATE, abs=1e-6)
    assert reply["duration_sec"] > 0.5
    assert any(any(chunk) for chunk in chunks), "the stream carried no audio"


async def _stream_then_cancel(worker: KokoroWorker, text: str):
    """Start a stream, cancel as soon as the first chunk lands, observe the tail."""
    connection = worker.connection
    stream_id = uuid.uuid4()
    queue = connection.open_stream(stream_id)
    request = {
        "id": uuid.uuid4().hex,
        "method": "stream_pcm",
        "stream_id": str(stream_id),
        **_speech_payload(text=text),
    }
    try:
        reply_task = asyncio.ensure_future(connection.request(request, timeout=IPC_TIMEOUT))
        first = await asyncio.wait_for(queue.get(), timeout=120.0)
        cancel_reply = await connection.request(
            {"id": uuid.uuid4().hex, "method": "cancel", "target_request_id": request["id"]},
            timeout=60.0,
        )
        reply = await asyncio.wait_for(reply_task, timeout=IPC_TIMEOUT)

        # Everything queued before the terminal reply was produced pre-cancel.
        before = [first]
        while True:
            try:
                before.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Anything arriving now would be audio the caller already abandoned.
        await asyncio.sleep(0.3)
        after = []
        while True:
            try:
                after.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return before, after, cancel_reply, reply
    finally:
        connection.close_stream(stream_id)


@requires_kokoro
def test_cancel_mid_stream_yields_job_cancelled_and_no_further_audio(kokoro_worker):
    before, after, cancel_reply, reply = kokoro_worker.run(
        _stream_then_cancel(kokoro_worker, TWO_CHUNK_TEXT)
    )

    assert cancel_reply["cancelled"] is True
    assert cancel_reply["target_request_id"] == reply["id"]
    assert reply["error_code"] == "JOB_CANCELLED"
    assert reply["suggestion"]
    # The cancel has to land while audio is still being produced: chunk 1 was
    # already on the wire (the same text streams 2 chunks when not cancelled).
    assert len(before) == 1, f"expected only the pre-cancel chunk, got {len(before)}"
    assert after == [], f"{len(after)} PCM frame(s) arrived after JOB_CANCELLED"


# ---------------------------------------------------------------------------
# Refusals: everything Kokoro cannot honour is a BAD_REQUEST
# ---------------------------------------------------------------------------

REJECTIONS: tuple[tuple[str, dict, str], ...] = (
    ("missing speaker", {"speaker": None}, "requires a speaker"),
    ("unknown speaker", {"speaker": "zz_nobody"}, "unknown kokoro speaker"),
    (
        "clone mode with a reference clip",
        {
            "speaker": None,
            "mode": "clone",
            "ref_wav_path": "/tmp/ref.wav",
            "ref_text": "hello",
        },
        "does not support mode='clone'",
    ),
    (
        "reference audio on a preset request",
        {"ref_wav_path": "/tmp/ref.wav", "ref_text": "hello"},
        "cannot clone a voice",
    ),
    ("voice design mode", {"mode": "voice_design"}, "does not support mode"),
    ("instruct", {"instruct": "say it angrily"}, "does not accept instruct"),
    (
        "unsupported language",
        {"language": "Japanese"},
        "does not support language='Japanese'",
    ),
)


@requires_kokoro
@pytest.mark.parametrize(
    ("label", "overrides", "fragment"),
    REJECTIONS,
    ids=[case[0] for case in REJECTIONS],
)
def test_unhonourable_request_is_bad_request(kokoro_worker, label, overrides, fragment):
    reply = kokoro_worker.request(
        {"method": "synthesize", **_speech_payload(**overrides)}
    )
    assert reply["error_code"] == "BAD_REQUEST", f"{label}: {reply}"
    assert fragment in reply["message"], f"{label}: {reply['message']}"
    assert reply["suggestion"], f"{label} refused without telling the caller what to do"


@requires_kokoro
def test_stream_pcm_validates_the_same_way(kokoro_worker):
    """Both entry points share `_speech_request`, so both must refuse alike."""
    chunks, reply = kokoro_worker.stream(_speech_payload(speaker="zz_nobody"))
    assert chunks == []
    assert reply["error_code"] == "BAD_REQUEST"
    assert "unknown kokoro speaker" in reply["message"]


@requires_kokoro
def test_missing_weights_report_model_not_found_with_the_download_command():
    """A repo that is not on this machine fails as MODEL_NOT_FOUND, not a crash.

    ``HF_HUB_OFFLINE=1`` is what makes this test free and deterministic: the
    worker's cache lookup misses and the download attempt fails immediately with
    no network traffic, which is exactly the shape a user sees after pulling the
    plug. Nothing is downloaded into the shared cache.
    """
    worker = KokoroWorker.spawn(
        models={MODEL_ALIAS: MISSING_REPO}, extra_env={"HF_HUB_OFFLINE": "1"}
    )
    try:
        reply = worker.request({"method": "load_model", "model_alias": MODEL_ALIAS})
        assert reply["error_code"] == "MODEL_NOT_FOUND", reply
        assert MISSING_REPO in reply["message"]
        assert DOWNLOADS_COMMAND in reply["suggestion"]
    finally:
        worker.close()

    cache_root = Path.home() / ".cache" / "huggingface" / "hub"
    assert not (cache_root / "models--hexgrad--definitely-not-a-real-kokoro-repo").exists()


# ---------------------------------------------------------------------------
# OpenAI compatibility: `voice="alloy"` resolves to a Kokoro preset
# ---------------------------------------------------------------------------


@requires_kokoro
def test_openai_alloy_resolves_to_af_heart_and_returns_audio(tmp_path):
    """The canonical OpenAI endpoint is the gate for builtin-name fallback."""
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    assert app.state.ctx.settings.default_engine == ENGINE_ID
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "voice": "alloy", "input": "Hello from the OpenAI endpoint."},
        )
    assert response.status_code == 200, response.text

    assert response.headers["x-agent-voice-engine"] == ENGINE_ID
    assert response.headers["x-agent-voice-model"] == MODEL_ALIAS
    assert response.headers["x-agent-voice-speaker"] == "af_heart"
    assert response.headers["content-type"].startswith("audio/wav")

    wav = tmp_path / "openai.wav"
    wav.write_bytes(response.content)
    assert _assert_pcm16_wav(wav) > 0.2


@requires_kokoro
def test_openai_unknown_voice_is_not_silently_remapped(tmp_path):
    """Only the six builtin names are substituted: a typo must not speak."""
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "voice": "not-a-real-voice", "input": "Hello."},
        )
    assert response.status_code == 404, response.text
    body = response.json()
    # Kokoro declares no clone model, so an unresolvable voice surfaces as either
    # "no such voice" or "no clone model" — never as audio in someone else's timbre.
    assert body["error_code"] in {"VOICE_NOT_FOUND", "MODEL_NOT_FOUND"}
    assert body["suggestion"]


# ---------------------------------------------------------------------------
# The daemon's own end-to-end gate
# ---------------------------------------------------------------------------


@requires_kokoro
def test_engines_test_kokoro_passes_all_seven_steps():
    """`agent-voice engines test kokoro` is 7/7 on CPU, through the real CLI."""
    result = subprocess.run(
        [sys.executable, "-m", "agent_voice.cli", "engines", "test", ENGINE_ID],
        capture_output=True,
        text=True,
        timeout=280,
        shell=False,
        check=False,
        env=dict(os.environ),
    )
    output = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, output
    assert "IPC v1 conformance passed" in output

    for step in (
        "spawn+connect",
        "hello",
        "load_model",
        "synthesize",
        "cancel",
        "health",
        "shutdown",
    ):
        rows = [line for line in result.stdout.splitlines() if line.startswith(step)]
        assert rows, f"no '{step}' row in:\n{output}"
        assert "PASS" in rows[0], rows[0]
        assert "FAIL" not in rows[0] and "SKIP" not in rows[0], rows[0]
