"""v0.6.0 device switching: preferences, resolution, worker device paths.

Four layers are covered, cheapest first:

1. **Preferences** — `AGENT_VOICE_DEVICE`/`AGENT_VOICE_DEVICES` parsing and every
   error case (malformed entry, unknown device, unknown engine, unsupported pair).
2. **Resolution** — `hardware.resolve_device` against the *real* engine manifests
   with synthetic hardware profiles, so the CUDA/CPU/Apple/ROCm matrix is covered
   on any machine, plus one test that follows whatever this host really has.
3. **Workers** — `engines/*/worker.py` loaded by path with a fake `torch` (and a
   fake `qwen_tts`/`faster_qwen3_tts`): the CPU path must ask for
   `device_map="cpu"` + `float32` and never touch `torch.cuda`; the CUDA path must
   report the real (mocked) allocation; `qwen3_tts_fast` must refuse cpu/mps with
   a clear BAD_REQUEST. No real venv is touched and nothing is downloaded.
4. **Plumbing** — `WorkerManager._require_servable_torch` refuses a CPU torch build
   for a cuda request with the reinstall hint, and the synthesis cache key differs
   per device.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_voice.cache import CACHE_KEY_FIELDS, build_cache_key
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRecord, EngineRegistry
from agent_voice.hardware import (
    GpuInfo,
    HardwareProfile,
    detect_hardware,
    pair_level,
    resolve_device,
    torch_build_supports,
    validate_device_preferences,
)
from agent_voice.schemas import AgentVoiceError, ComputeDevice, EngineManifest, ErrorCode
from agent_voice.voice_store import VoiceStore
from agent_voice.worker_manager import WorkerManager

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINES_DIR = REPO_ROOT / "engines"
QWEN_WORKER = ENGINES_DIR / "qwen3_tts" / "worker.py"
FAST_WORKER = ENGINES_DIR / "qwen3_tts_fast" / "worker.py"
KOKORO_WORKER = ENGINES_DIR / "kokoro" / "worker.py"
PHONON_WORKER = ENGINES_DIR / "phonon" / "worker.py"

CPU_TORCH: dict[str, object] = {
    "version": "2.15.0+cpu",
    "cuda": None,
    "hip": None,
    "cuda_available": False,
    "mps": False,
}
CUDA_TORCH: dict[str, object] = {
    "version": "2.11.0+cu128",
    "cuda": "12.8",
    "hip": None,
    "cuda_available": True,
    "mps": False,
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> EngineRegistry:
    """The real registry: the manifests are the contract being resolved."""
    return EngineRegistry(ENGINES_DIR)


@pytest.fixture(scope="module")
def this_host() -> HardwareProfile:
    return detect_hardware()


@pytest.fixture
def load_worker(monkeypatch: pytest.MonkeyPatch):
    """Load an engine worker by path under a unique module name.

    Workers are self-contained scripts, not importable packages, so the only way
    to unit-test them is `spec_from_file_location`. The fake `torch` module is
    installed by the test *before* this runs; every module-level environment read
    (``AGENT_VOICE_RESOLVED_DEVICE``) therefore sees that test's device.
    """
    loaded: list[str] = []

    def _load(name: str, path: Path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loaded.append(name)
        spec.loader.exec_module(module)
        return module

    yield _load
    for name in loaded:
        sys.modules.pop(name, None)


def _settings(**overrides: object) -> Settings:
    """Hermetic settings: no repo `.env`, and no inherited device preference."""
    base: dict[str, object] = {
        "device": "auto",
        "devices": {},
        "DATA_DIR": REPO_ROOT / "data",
        "ENGINES_DIR": ENGINES_DIR,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _cuda_host(vram_gb: float = 16.0) -> HardwareProfile:
    return HardwareProfile(
        os="linux",
        arch="x86_64",
        cpu_cores=16,
        ram_gb=64.0,
        python_version="3.12.12",
        has_uv=True,
        has_ffmpeg=True,
        has_espeak_ng=True,
        gpus=(
            GpuInfo(
                kind="nvidia",
                name="NVIDIA GeForce RTX 4060 Ti",
                vram_gb=vram_gb,
                driver="580.0",
                backend="cuda",
            ),
        ),
        accelerator="cuda",
    )


def _cpu_host() -> HardwareProfile:
    return HardwareProfile(
        os="linux",
        arch="x86_64",
        cpu_cores=8,
        ram_gb=16.0,
        python_version="3.12.12",
        has_uv=True,
        has_ffmpeg=True,
        has_espeak_ng=True,
        gpus=(),
        accelerator="cpu",
    )


def _apple_host() -> HardwareProfile:
    return HardwareProfile(
        os="macos",
        arch="arm64",
        cpu_cores=10,
        ram_gb=32.0,
        python_version="3.12.12",
        has_uv=True,
        has_ffmpeg=True,
        has_espeak_ng=True,
        gpus=(GpuInfo(kind="apple", name="Apple Silicon", backend="mps"),),
        accelerator="mps",
        unified_ram_gb=32.0,
    )


def _rocm_host() -> HardwareProfile:
    return HardwareProfile(
        os="linux",
        arch="x86_64",
        cpu_cores=16,
        ram_gb=64.0,
        python_version="3.12.12",
        has_uv=True,
        has_ffmpeg=True,
        has_espeak_ng=True,
        gpus=(
            GpuInfo(kind="amd", name="AMD Radeon RX 7900 XT", vram_gb=20.0, backend="rocm"),
        ),
        accelerator="rocm",
    )


HOSTS = {
    "cuda": _cuda_host,
    "cpu": _cpu_host,
    "mps": _apple_host,
    "rocm": _rocm_host,
}


# ---------------------------------------------------------------------------
# Preference parsing
# ---------------------------------------------------------------------------


def test_agent_voice_device_accepts_a_valid_global_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICE", "cpu")
    assert Settings(_env_file=None).device == "cpu"


def test_agent_voice_device_rejects_an_unknown_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICE", "tpu")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "AGENT_VOICE_DEVICE" in message
    assert "cuda" in message and "mps" in message and "rocm" in message


def test_agent_voice_devices_parses_the_documented_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICES", "kokoro=cpu,qwen3_tts=cuda")

    settings = Settings(_env_file=None)

    assert settings.devices == {"kokoro": "cpu", "qwen3_tts": "cuda"}


def test_agent_voice_devices_tolerates_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICES", " kokoro = cpu , qwen3_tts = cuda ")

    assert Settings(_env_file=None).devices == {"kokoro": "cpu", "qwen3_tts": "cuda"}


def test_agent_voice_devices_empty_is_no_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICES", "   ")
    assert Settings(_env_file=None).devices == {}


@pytest.mark.parametrize(
    "value",
    ["kokoro", "kokoro=cpu,qwen3_tts", "=cpu", "kokoro="],
    ids=["no-equals", "second-entry-malformed", "missing-engine", "missing-device"],
)
def test_agent_voice_devices_rejects_a_malformed_entry(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A missing `=` is a load-time error, not a silently ignored line.

    ``pydantic.ValidationError`` subclasses ``ValueError``, so this is exactly the
    "ValueError naming the key" the settings contract promises.
    """
    monkeypatch.setenv("AGENT_VOICE_DEVICES", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "AGENT_VOICE_DEVICES" in message
    # The message must say what the entry needs, not just "invalid".
    assert "engine=device" in message or "engine and a device" in message


def test_agent_voice_devices_rejects_an_unknown_device_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_VOICE_DEVICES", "kokoro=tpu")

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    message = str(excinfo.value)
    assert "kokoro" in message
    assert "cpu" in message and "cuda" in message


# ---------------------------------------------------------------------------
# Preference validation against the registry
# ---------------------------------------------------------------------------


def test_validate_device_preferences_accepts_the_shipped_pairs(
    registry: EngineRegistry,
) -> None:
    validate_device_preferences(
        _settings(devices={"kokoro": "cpu", "qwen3_tts": "cuda"}), registry
    )


def test_validate_device_preferences_rejects_an_unknown_engine(
    registry: EngineRegistry,
) -> None:
    settings = _settings(devices={"not_an_engine": "cpu"})

    with pytest.raises(AgentVoiceError) as excinfo:
        validate_device_preferences(settings, registry)

    assert excinfo.value.error_code == ErrorCode.ENGINE_NOT_FOUND
    assert "not_an_engine" in excinfo.value.message
    assert excinfo.value.suggestion


def test_validate_device_preferences_rejects_an_unsupported_pair(
    registry: EngineRegistry,
) -> None:
    settings = _settings(devices={"phonon": "cuda"})

    with pytest.raises(AgentVoiceError) as excinfo:
        validate_device_preferences(settings, registry)

    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert "phonon=cuda" in error.message
    # The supported list is what turns a typo into a fix.
    assert "cpu" in error.message and "mps" in error.message
    assert "AGENT_VOICE_DEVICES" in error.suggestion


# ---------------------------------------------------------------------------
# resolve_device: the matrix
# ---------------------------------------------------------------------------


def _resolve(
    registry: EngineRegistry,
    engine_id: str,
    host: HardwareProfile,
    **settings_overrides: object,
) -> ComputeDevice:
    return resolve_device(
        engine_id,
        registry.get(engine_id).manifest,
        _settings(**settings_overrides),
        host,
    )


@pytest.mark.parametrize(
    ("engine_id", "host", "expected"),
    [
        ("kokoro", "cuda", "cpu"),
        ("kokoro", "cpu", "cpu"),
        ("kokoro", "mps", "cpu"),
        ("phonon", "cuda", "cpu"),
        ("phonon", "cpu", "cpu"),
        ("phonon", "mps", "cpu"),
        ("mock", "cuda", "cpu"),
        ("qwen3_tts", "cuda", "cuda"),
        # On an AMD host the verified cpu pair wins over the experimental rocm one.
        ("qwen3_tts", "rocm", "cpu"),
    ],
)
def test_auto_follows_the_manifest_default(
    registry: EngineRegistry, engine_id: str, host: str, expected: ComputeDevice
) -> None:
    """`auto` picks the manifest default, never an experimental pair.

    Kokoro/phonon declare cuda/mps as *experimental*; `auto` on a GPU host must
    still land on their verified cpu default. Qwen3-TTS declares cuda, and a cuda
    host answers cuda. On a host that cannot serve the declared default (Apple,
    AMD, plain CPU) the fallback prefers a `verified` pair over an `experimental`
    one — so qwen3_tts answers cpu there rather than silently opting into MPS or
    ROCm, both of which are experimental for it.
    """
    assert _resolve(registry, engine_id, HOSTS[host]()) == expected


def test_auto_falls_back_to_cpu_for_qwen_on_a_cpu_host(
    registry: EngineRegistry,
) -> None:
    """`qwen3_tts` default is cuda; on a CPU box the fallback is its cpu path."""
    device = _resolve(registry, "qwen3_tts", _cpu_host())

    assert device == "cpu"
    assert pair_level("qwen3_tts", device) == "verified"


def test_auto_prefers_verified_cpu_over_experimental_mps_on_apple_silicon(
    registry: EngineRegistry,
) -> None:
    """Apple silicon gets the verified cpu path, not the experimental MPS one.

    `auto` must never resolve to an `experimental` pair where a `verified` one is
    available — which is what makes the mps/rocm paths opt-in only.
    """
    device = _resolve(registry, "qwen3_tts", _apple_host())

    assert device == "cpu"
    assert pair_level("qwen3_tts", device) == "verified"
    assert pair_level("qwen3_tts", "mps") == "experimental"


def test_qwen3_tts_explicit_mps_on_apple_is_allowed(
    registry: EngineRegistry,
) -> None:
    device = _resolve(registry, "qwen3_tts", _apple_host(), device="mps")

    assert device == "mps"
    assert pair_level("qwen3_tts", "mps") == "experimental"


def test_qwen3_tts_explicit_rocm_is_allowed(
    registry: EngineRegistry,
) -> None:
    assert _resolve(registry, "qwen3_tts", _rocm_host(), device="rocm") == "rocm"


def test_qwen3_tts_fast_on_a_cpu_host_is_bad_request(
    registry: EngineRegistry,
) -> None:
    """`qwen3_tts_fast` is CUDA-only: no silent cpu fallback, a clear 400."""
    with pytest.raises(AgentVoiceError) as excinfo:
        _resolve(registry, "qwen3_tts_fast", _cpu_host())

    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert "qwen3_tts_fast" in error.message
    assert "cuda" in error.message
    assert "Detected accelerator: cpu" in error.suggestion


def test_qwen3_tts_fast_on_apple_silicon_is_bad_request(
    registry: EngineRegistry,
) -> None:
    with pytest.raises(AgentVoiceError) as excinfo:
        _resolve(registry, "qwen3_tts_fast", _apple_host())

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST


def test_explicit_cuda_on_a_cpu_host_raises(
    registry: EngineRegistry,
) -> None:
    """An explicit request the host cannot serve is an error, not a quiet fallback."""
    with pytest.raises(AgentVoiceError) as excinfo:
        _resolve(registry, "kokoro", _cpu_host(), device="cuda")

    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert "not available on this host" in error.message
    assert "AGENT_VOICE_DEVICE=cpu" in error.suggestion


def test_unsupported_pair_raises(
    registry: EngineRegistry,
) -> None:
    """`phonon=cuda` is not in the manifest's supported_devices at all."""
    with pytest.raises(AgentVoiceError) as excinfo:
        _resolve(registry, "phonon", _cuda_host(), devices={"phonon": "cuda"})

    error = excinfo.value
    assert error.error_code == ErrorCode.BAD_REQUEST
    assert "does not support device 'cuda'" in error.message
    assert "cpu, mps" in error.suggestion


def test_per_engine_override_beats_the_global_preference(
    registry: EngineRegistry,
) -> None:
    assert (
        _resolve(registry, "qwen3_tts", _cuda_host(), devices={"qwen3_tts": "cpu"})
        == "cpu"
    )
    assert _resolve(registry, "kokoro", _cuda_host(), device="cpu") == "cpu"


def test_resolution_follows_this_host(
    registry: EngineRegistry, this_host: HardwareProfile
) -> None:
    """The same matrix, on whatever machine actually runs the suite."""
    qwen = _resolve(registry, "qwen3_tts", this_host)
    if this_host.accelerator == "cuda":
        assert qwen == "cuda"
        assert _resolve(registry, "qwen3_tts_fast", this_host) == "cuda"
    else:
        assert qwen == "cpu"
        with pytest.raises(AgentVoiceError):
            _resolve(registry, "qwen3_tts_fast", this_host)
    # CPU engines never move off cpu under `auto`.
    assert _resolve(registry, "kokoro", this_host) == "cpu"
    assert _resolve(registry, "phonon", this_host) == "cpu"


# ---------------------------------------------------------------------------
# WorkerManager: resolution and the torch-variant gate
# ---------------------------------------------------------------------------


class _NoVram:
    """Stand-in for VRAMProbe: NVML is never initialised in a unit test."""

    def free_gb(self) -> float | None:
        return None

    def used_mb(self) -> int | None:
        return None


def _manager(
    tmp_path: Path,
    registry: object,
    host: HardwareProfile,
) -> WorkerManager:
    from agent_voice.cache import SynthesisCache

    settings = _settings(DATA_DIR=tmp_path / "data", ENGINES_DIR=ENGINES_DIR)
    settings.ensure_dirs()
    return WorkerManager(
        settings=settings,
        registry=registry,  # type: ignore[arg-type]
        cache=SynthesisCache(tmp_path / "cache"),
        voices=VoiceStore(tmp_path / "voices"),
        vram=_NoVram(),  # type: ignore[arg-type]
        hardware=host,
    )


def test_worker_manager_resolves_each_engine(
    tmp_path: Path, registry: EngineRegistry
) -> None:
    cuda_manager = _manager(tmp_path / "cuda", registry, _cuda_host())
    assert cuda_manager.resolved_device("kokoro") == "cpu"
    assert cuda_manager.resolved_device("qwen3_tts") == "cuda"
    assert cuda_manager.resolved_device("qwen3_tts_fast") == "cuda"

    cpu_manager = _manager(tmp_path / "cpu", registry, _cpu_host())
    assert cpu_manager.resolved_device("qwen3_tts") == "cpu"
    with pytest.raises(AgentVoiceError) as excinfo:
        cpu_manager.resolved_device("qwen3_tts_fast")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST


def _record_with_torch(
    registry: EngineRegistry, engine_id: str, torch_info: dict[str, object] | None
) -> EngineRecord:
    base = registry.get(engine_id)
    return EngineRecord(
        engine_id=base.engine_id,
        directory=base.directory,
        manifest=base.manifest,
        installed=True,
        models=list(base.models),
        torch_info=torch_info,
    )


def test_require_servable_torch_refuses_a_cpu_build_for_cuda(
    tmp_path: Path, registry: EngineRegistry
) -> None:
    """A stale CPU variant must be a 409 with the reinstall command, not a crash."""
    manager = _manager(tmp_path, registry, _cuda_host())
    record = _record_with_torch(registry, "qwen3_tts", CPU_TORCH)

    with pytest.raises(AgentVoiceError) as excinfo:
        manager._require_servable_torch(record, "cuda")

    error = excinfo.value
    assert error.error_code == ErrorCode.ENGINE_NOT_INSTALLED
    assert error.http_status == 409
    assert "2.15.0+cpu" in error.message
    assert "does not support it" in error.message
    assert "agent-voice engines install qwen3_tts --device cuda" in error.suggestion


def test_require_servable_torch_accepts_cpu_and_a_matching_build(
    tmp_path: Path, registry: EngineRegistry
) -> None:
    manager = _manager(tmp_path, registry, _cuda_host())
    cpu_record = _record_with_torch(registry, "qwen3_tts", CPU_TORCH)
    cuda_record = _record_with_torch(registry, "qwen3_tts", CUDA_TORCH)

    # Every build serves the CPU, including a CUDA wheel.
    manager._require_servable_torch(cpu_record, "cpu")
    manager._require_servable_torch(cuda_record, "cpu")
    # A CUDA build satisfies a cuda request.
    manager._require_servable_torch(cuda_record, "cuda")
    # rocm needs a HIP build: a plain cu128 wheel must not be mistaken for one.
    with pytest.raises(AgentVoiceError) as excinfo:
        manager._require_servable_torch(cuda_record, "rocm")
    assert excinfo.value.error_code == ErrorCode.ENGINE_NOT_INSTALLED
    assert "qwen3_tts --device rocm" in excinfo.value.suggestion


@pytest.mark.parametrize(
    ("torch_info", "device", "expected"),
    [
        (CPU_TORCH, "cpu", True),
        (CPU_TORCH, "cuda", False),
        (CPU_TORCH, "rocm", False),
        (CPU_TORCH, "mps", False),
        (CUDA_TORCH, "cuda", True),
        (CUDA_TORCH, "rocm", False),
        (CUDA_TORCH, "mps", False),
        ({"cuda": None, "hip": "6.2", "cuda_available": True, "mps": False}, "rocm", True),
        (None, "cuda", False),
        (None, "cpu", True),
    ],
)
def test_torch_build_supports_matrix(
    torch_info: dict[str, object] | None, device: ComputeDevice, expected: bool
) -> None:
    assert torch_build_supports(torch_info, device) is expected


class _StubRegistry:
    """Registry whose only engine is the one record a test is about."""

    def __init__(self, record: EngineRecord) -> None:
        self.record = record

    def get(self, engine_id: str) -> EngineRecord:
        assert engine_id == self.record.engine_id
        return self.record

    def require_installed(
        self, engine_id: str, *, allow_agent_mutation: bool = False
    ) -> EngineRecord:
        return self.get(engine_id)

    def ids(self) -> list[str]:
        return [self.record.engine_id]


async def test_ensure_worker_refuses_a_stale_cpu_variant_before_spawning(
    tmp_path: Path, registry: EngineRegistry
) -> None:
    """`ensure_worker` fails with 409 and starts no process."""
    record = _record_with_torch(registry, "qwen3_tts", CPU_TORCH)
    manager = _manager(tmp_path, _StubRegistry(record), _cuda_host())

    with pytest.raises(AgentVoiceError) as excinfo:
        await manager.ensure_worker("qwen3_tts")

    assert excinfo.value.error_code == ErrorCode.ENGINE_NOT_INSTALLED
    assert "engines install qwen3_tts --device cuda" in excinfo.value.suggestion
    assert manager.spawn_count == 0


# ---------------------------------------------------------------------------
# Worker device paths: fakes only, no venv, no GPU
# ---------------------------------------------------------------------------


class _FakeCuda:
    """Records every `torch.cuda.*` call so "no CUDA call" is provable."""

    def __init__(self, *, available: bool = False, allocated_bytes: int = 0) -> None:
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
    """The narrow slice of torch the workers touch, plus call recording."""

    def __init__(
        self,
        *,
        cuda_available: bool = False,
        allocated_bytes: int = 0,
        mps_available: bool = False,
    ) -> None:
        super().__init__("torch")
        # Unique sentinels: a test asserts `dtype is fake.float32`.
        self.float32 = object()
        self.bfloat16 = object()
        self.cuda = _FakeCuda(available=cuda_available, allocated_bytes=allocated_bytes)
        self.mps = types.SimpleNamespace(
            is_available=lambda: mps_available,
            current_allocated_memory=lambda: 0,
            empty_cache=lambda: None,
        )
        self.backends = types.SimpleNamespace(
            mps=types.SimpleNamespace(is_available=lambda: mps_available)
        )
        self.version = types.SimpleNamespace(
            cuda="12.8" if cuda_available else None, hip=None
        )
        self.seeds: list[int] = []

    def manual_seed(self, seed: int) -> None:
        self.seeds.append(seed)

    def device(self, name: str) -> str:
        return name


class _RecordingConnection:
    """Captures JSON replies; a PCM frame means the test asked for the wrong thing."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def send_json(self, payload: dict) -> None:
        self.sent.append(dict(payload))

    def send_pcm(self, *args: object) -> None:  # pragma: no cover - not expected
        raise AssertionError("no PCM frame expected in a device unit test")


def _install_fake_torch(monkeypatch: pytest.MonkeyPatch, fake: _FakeTorch) -> _FakeTorch:
    monkeypatch.setitem(sys.modules, "torch", fake)
    return fake


def _install_fake_qwen_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict], type]:
    """A `qwen_tts` module whose model records its `from_pretrained` kwargs."""
    calls: list[dict] = []

    class _FakeQwen3TTSModel:
        @classmethod
        def from_pretrained(cls, repo: str, **kwargs: object) -> object:
            calls.append({"repo": repo, **kwargs})
            return object()

    module = types.ModuleType("qwen_tts")
    module.Qwen3TTSModel = _FakeQwen3TTSModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "qwen_tts", module)
    return calls, _FakeQwen3TTSModel


def test_qwen3_tts_cpu_load_uses_cpu_device_map_and_float32(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    """`AGENT_VOICE_RESOLVED_DEVICE=cpu` must reach `from_pretrained` as cpu."""
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cpu")
    fake = _install_fake_torch(monkeypatch, _FakeTorch())
    calls, _ = _install_fake_qwen_tts(monkeypatch)
    worker = load_worker("av_qwen3_tts_cpu", QWEN_WORKER)

    conn = _RecordingConnection()
    engine = worker.QwenEngine(conn, {"0.6b-base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base"})
    assert engine.device == "cpu"

    engine._load("0.6b-base")

    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["repo"] == "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
    assert kwargs["device_map"] == "cpu"
    assert kwargs["dtype"] is fake.float32
    assert kwargs["attn_implementation"] == "sdpa"
    # The whole point of the cpu path: torch's CUDA namespace is never touched.
    assert fake.cuda.calls == []
    # And there is no device pool to measure on cpu.
    assert engine._vram_mb() is None
    assert fake.cuda.calls == []


def test_qwen3_tts_cuda_load_uses_cuda0_and_bfloat16(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cuda")
    fake = _install_fake_torch(
        monkeypatch, _FakeTorch(cuda_available=True, allocated_bytes=1536 * 1024**2)
    )
    calls, _ = _install_fake_qwen_tts(monkeypatch)
    worker = load_worker("av_qwen3_tts_cuda", QWEN_WORKER)

    engine = worker.QwenEngine(_RecordingConnection(), {"0.6b-base": "repo/weights"})
    engine._load("0.6b-base")

    assert calls[0]["device_map"] == "cuda:0"
    assert calls[0]["dtype"] is fake.bfloat16
    assert engine._vram_mb() == 1536


def test_qwen3_tts_hello_and_health_report_the_device_and_real_vram(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cuda")
    _install_fake_torch(
        monkeypatch, _FakeTorch(cuda_available=True, allocated_bytes=2048 * 1024**2)
    )
    worker = load_worker("av_qwen3_tts_hello_cuda", QWEN_WORKER)

    conn = _RecordingConnection()
    engine = worker.QwenEngine(conn, {})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    hello, health = conn.sent
    assert hello["protocol_version"] == 1
    assert hello["engine_id"] == "qwen3_tts"
    assert hello["device"] == "cuda"
    assert hello["gpu_vram_mb"] == 2048
    assert health["device"] == "cuda"
    assert health["gpu_vram_mb"] == 2048


@pytest.mark.parametrize("device", ["cpu", "mps"])
def test_qwen3_tts_fast_refuses_cpu_and_mps_with_a_clear_bad_request(
    monkeypatch: pytest.MonkeyPatch, load_worker, device: str
) -> None:
    """The CUDA-graph backend has no cpu/mps path, and says so as a 400."""
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", device)
    worker = load_worker(f"av_qwen3_tts_fast_{device}", FAST_WORKER)

    conn = _RecordingConnection()
    engine = worker.FastEngine(conn, {"0.6b-base": "repo/weights"})
    engine.load_model({"id": "req-1", "model_alias": "0.6b-base"})

    reply = conn.sent[-1]
    assert reply["error_code"] == "BAD_REQUEST"
    assert worker.UNSUPPORTED_DEVICE_MARKER in reply["message"]
    assert device in reply["message"]
    # The fix points at the sibling engine and at the cuda pin.
    assert "qwen3_tts" in reply["suggestion"]
    assert "qwen3_tts_fast=cuda" in reply["suggestion"]


def test_qwen3_tts_fast_reports_device_and_vram_on_a_mocked_cuda(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cuda")
    _install_fake_torch(
        monkeypatch, _FakeTorch(cuda_available=True, allocated_bytes=3072 * 1024**2)
    )
    worker = load_worker("av_qwen3_tts_fast_cuda", FAST_WORKER)

    conn = _RecordingConnection()
    engine = worker.FastEngine(conn, {})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    hello, health = conn.sent
    assert hello["engine_id"] == "qwen3_tts_fast"
    assert hello["device"] == "cuda"
    assert hello["gpu_vram_mb"] == 3072
    assert hello["cuda_graphs"] is False, "graphs are captured by load_model, not hello"
    assert health["device"] == "cuda"
    assert health["gpu_vram_mb"] == 3072


def test_kokoro_reports_the_injected_cpu_device_and_zero_vram(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cpu")
    # The cpu path empties CUDA_VISIBLE_DEVICES before torch is imported; record
    # the key so teardown restores whatever this machine had.
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    worker = load_worker("av_kokoro_cpu", KOKORO_WORKER)

    assert worker.RESOLVED_DEVICE == "cpu"
    # The cpu path empties the variable so torch cannot see a GPU.
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""
    conn = _RecordingConnection()
    engine = worker.KokoroEngine(conn, {"kokoro-82m": "hexgrad/Kokoro-82M"})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    for reply in conn.sent:
        assert reply["device"] == "cpu"
        assert reply["gpu_vram_mb"] == 0


def test_kokoro_reports_real_vram_on_a_mocked_cuda(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    """The 0 MB claim is only ever for `device=cpu`; cuda is measured."""
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cuda")
    _install_fake_torch(
        monkeypatch, _FakeTorch(cuda_available=True, allocated_bytes=64 * 1024**2)
    )
    worker = load_worker("av_kokoro_cuda", KOKORO_WORKER)
    conn = _RecordingConnection()
    engine = worker.KokoroEngine(conn, {"kokoro-82m": "hexgrad/Kokoro-82M"})

    engine.hello({"id": "hello"})

    reply = conn.sent[-1]
    assert reply["device"] == "cuda"
    assert reply["gpu_vram_mb"] == 64


def test_phonon_reports_the_injected_cpu_device_and_zero_vram(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "cpu")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("FERMION_DEVICE", "")
    worker = load_worker("av_phonon_cpu", PHONON_WORKER)

    assert worker.RESOLVED_DEVICE == "cpu"
    conn = _RecordingConnection()
    engine = worker.PhononEngine(conn, {"phonon-2.5": "Physasso/Phonon-2.5"})
    engine.hello({"id": "hello"})
    engine.health({"id": "health"})

    for reply in conn.sent:
        assert reply["device"] == "cpu"
        assert reply["gpu_vram_mb"] == 0


def test_phonon_mps_maps_to_mlx_and_reports_no_vram_number(
    monkeypatch: pytest.MonkeyPatch, load_worker
) -> None:
    """`mps` is Fermion's MLX path: torch cannot measure it, so it says None."""
    monkeypatch.setenv("AGENT_VOICE_RESOLVED_DEVICE", "mps")
    monkeypatch.setenv("FERMION_DEVICE", "")
    worker = load_worker("av_phonon_mps", PHONON_WORKER)

    conn = _RecordingConnection()
    engine = worker.PhononEngine(conn, {"phonon-2.5": "Physasso/Phonon-2.5"})
    engine.hello({"id": "hello"})

    reply = conn.sent[-1]
    assert reply["device"] == "mps"
    assert reply["gpu_vram_mb"] is None
    assert os.environ["FERMION_DEVICE"] == "mlx"


def test_worker_device_manifests_declare_what_the_workers_can_do(
    registry: EngineRegistry,
) -> None:
    """Guard rail: a resolver change must not outrun the shipped manifests."""
    expected: dict[str, list[str]] = {
        "kokoro": ["cpu", "cuda", "mps"],
        "phonon": ["cpu", "mps"],
        "qwen3_tts": ["cpu", "cuda", "mps", "rocm"],
        "qwen3_tts_fast": ["cuda"],
        "mock": ["cpu"],
        "mock_asr": ["cpu"],
    }
    for engine_id, devices in expected.items():
        manifest: EngineManifest = registry.get(engine_id).manifest
        assert list(manifest.supported_devices) == devices, engine_id
    # CPU-only engines must not claim the accelerator slot any more.
    assert registry.get("kokoro").manifest.device == "cpu"
    assert registry.get("mock").manifest.device == "cpu"


# ---------------------------------------------------------------------------
# The cache key is device-scoped
# ---------------------------------------------------------------------------


def _cache_key(**overrides: object) -> str:
    fields: dict[str, object] = {
        "text": "The same sentence, twice.",
        "voice_id": "",
        "ref_wav_sha256": "",
        "ref_text": "",
        "x_vector_only_mode": False,
        "engine_id": "kokoro",
        "model_alias": "kokoro-82m",
        "backend_name": "kokoro",
        "backend_version": "0.9.4",
        "device": "cpu",
        "language": "Auto",
        "seed": None,
        "speed": 1.0,
        "response_format": "wav",
        "normalize_lufs": -16.0,
        "chunk_chars": 400,
        "take": None,
        "mode": "custom_voice",
        "speaker": "af_heart",
        "instruct": "",
        "hf_repo": "hexgrad/Kokoro-82M",
        "model_revision": "unknown",
    }
    fields.update(overrides)
    return build_cache_key(**fields)


def test_cache_key_includes_the_device() -> None:
    """A CPU render and a CUDA render of the same request are not the same entry."""
    assert "device" in CACHE_KEY_FIELDS

    cpu_key = _cache_key(device="cpu")
    cuda_key = _cache_key(device="cuda")

    assert cpu_key != cuda_key
    assert cpu_key == _cache_key(device="cpu")
    # Same request, same device: still a hit.
    assert cuda_key == _cache_key(device="cuda")


def test_cache_key_requires_the_device() -> None:
    fields = {
        "text": "x",
        "voice_id": "",
        "ref_wav_sha256": "",
        "ref_text": "",
        "x_vector_only_mode": False,
        "engine_id": "mock",
        "model_alias": "mock-tone",
        "backend_name": "mock",
        "backend_version": "1",
        "language": "Auto",
        "seed": None,
        "speed": 1.0,
        "response_format": "wav",
        "normalize_lufs": -16.0,
        "chunk_chars": 400,
        "take": None,
        "mode": "clone",
        "speaker": "",
        "instruct": "",
        "hf_repo": "local/mock-tone",
        "model_revision": "local",
    }

    with pytest.raises(ValueError) as excinfo:
        build_cache_key(**fields)

    assert "device" in str(excinfo.value)
