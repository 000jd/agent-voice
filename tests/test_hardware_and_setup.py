"""v0.6.0 hardware detection, device resolution, the setup wizard, the ``.env``
writer and the doctor severity model.

Hermetic by construction: the five hardware profiles are built from
``HardwareProfile``/``GpuInfo`` (or from injected probes), ``plan_setup`` and
``apply_setup`` are driven through an injected runner and downloader, and every
path a test writes lives under ``tmp_path``. No test here reads the developer's
checkout ``.env``, creates an engine venv or runs ``uv``. The real-checkout
``doctor`` release gate stays in ``tests/test_cli_doctor.py``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

import agent_voice.cli as cli
import agent_voice.engine_registry as engine_registry
from agent_voice import envfile, hardware, setup_wizard
from agent_voice.cli import app
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry
from agent_voice.hardware import HardwareProfile
from agent_voice.schemas import (
    AgentVoiceError,
    EngineEntry,
    EngineManifest,
    ErrorCode,
    ModelEntry,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINES_SRC = REPO_ROOT / "engines"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
runner = CliRunner()

# ---------------------------------------------------------------------------
# The five reference hosts
# ---------------------------------------------------------------------------

NVIDIA_GPU = hardware.GpuInfo(
    kind="nvidia",
    name="NVIDIA GeForce RTX 4060 Ti",
    vram_gb=16.0,
    driver="580.178.04",
    backend="cuda",
)
AMD_GPU = hardware.GpuInfo(
    kind="amd", name="AMD Radeon RX 7900 XTX", vram_gb=24.0, backend="rocm"
)
APPLE_GPU = hardware.GpuInfo(kind="apple", name="Apple Silicon", backend="mps")


def _hardware(
    *,
    os_name: str = "linux",
    arch: str = "x86_64",
    gpus: tuple[hardware.GpuInfo, ...] = (),
    accelerator: hardware.Accelerator = "cpu",
    ram_gb: float | None = 32.0,
    unified_ram_gb: float | None = None,
) -> HardwareProfile:
    return HardwareProfile(
        os=os_name,
        arch=arch,
        cpu_cores=16,
        ram_gb=ram_gb,
        python_version="3.12.12",
        has_uv=True,
        has_ffmpeg=True,
        has_espeak_ng=True,
        gpus=gpus,
        accelerator=accelerator,
        unified_ram_gb=unified_ram_gb,
    )


NVIDIA_16GB = _hardware(gpus=(NVIDIA_GPU,), accelerator="cuda")
NVIDIA_6GB = _hardware(
    gpus=(
        hardware.GpuInfo(
            kind="nvidia",
            name="NVIDIA GeForce RTX 4060",
            vram_gb=6.0,
            driver="580.178.04",
            backend="cuda",
        ),
    ),
    accelerator="cuda",
)
APPLE_SILICON = _hardware(
    os_name="macos",
    arch="arm64",
    gpus=(APPLE_GPU,),
    accelerator="mps",
    unified_ram_gb=32.0,
)
AMD_ROCM = _hardware(gpus=(AMD_GPU,), accelerator="rocm")
CPU_ONLY = _hardware(gpus=(), accelerator="cpu", ram_gb=16.0)

PROFILES: dict[str, HardwareProfile] = {
    "nvidia-16gb": NVIDIA_16GB,
    "nvidia-6gb": NVIDIA_6GB,
    "apple-silicon": APPLE_SILICON,
    "amd-rocm": AMD_ROCM,
    "cpu-only": CPU_ONLY,
}

# (wizard profile label, default (engine, device) pairs) per host.
EXPECTED_DEFAULTS: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {
    "nvidia-16gb": (
        "nvidia-gpu",
        (("qwen3_tts_fast", "cuda"), ("kokoro", "cpu"), ("phonon", "cpu")),
    ),
    "nvidia-6gb": (
        "nvidia-gpu",
        (("qwen3_tts_fast", "cuda"), ("kokoro", "cpu"), ("phonon", "cpu")),
    ),
    "apple-silicon": ("apple-silicon", (("kokoro", "cpu"), ("phonon", "cpu"))),
    "amd-rocm": ("cpu-only", (("kokoro", "cpu"), ("phonon", "cpu"))),
    "cpu-only": ("cpu-only", (("kokoro", "cpu"), ("phonon", "cpu"))),
}

# What the canonical (non-variant) project of each shipped engine locks for.
CANONICAL_CHANNEL = {"cpu": "cpu", "cuda": "cu128", "rocm": "rocm6.2", "mps": "cpu"}

_ASSIGNMENT = re.compile(r"^[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")
_COMMENTED_ASSIGNMENT = re.compile(r"^[ \t]*#[ \t]*(?P<key>[A-Za-z_][A-Za-z0-9_]*)=")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _config(**overrides: object) -> Settings:
    """A Settings that reads no `.env`; tests add the fields they care about."""
    return Settings(_env_file=None, **overrides)


def _engine_tree(root: Path) -> Path:
    """A hermetic ``engines/`` tree mirroring the shipped layout.

    The manifests are the real ``engines/<id>/engine.yaml`` files; each engine
    that declares a venv also gets a canonical project whose
    ``[tool.agent-voice] device`` and ``uv.lock`` index agree with the manifest,
    which is exactly what ``plan_engine_install`` inspects. Nothing is copied
    that a plan would write to, and the repo's own engine venvs are never read.
    """
    root.mkdir(parents=True, exist_ok=True)
    for source in sorted(ENGINES_SRC.iterdir()):
        manifest_path = source / "engine.yaml"
        if not manifest_path.is_file():
            continue
        target = root / source.name
        target.mkdir()
        (target / "engine.yaml").write_bytes(manifest_path.read_bytes())
        raw = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
        if raw.get("venv_python") is None:
            # mock / mock_asr: dependency-free, no project at all.
            continue
        device = raw.get("default_device") or "cpu"
        (target / "pyproject.toml").write_text(
            f'[project]\nname = "{source.name}"\n\n[tool.agent-voice]\ndevice = "{device}"\n',
            encoding="utf-8",
        )
        (target / "uv.lock").write_text(
            f"# resolved for {device}\nsource = "
            f'"https://download.pytorch.org/whl/{CANONICAL_CHANNEL[device]}"\n',
            encoding="utf-8",
        )
    return root


def _registry(tmp_path: Path) -> EngineRegistry:
    """The real registry over a copy of the shipped manifests (no venv probing)."""
    return EngineRegistry(_engine_tree(tmp_path / "engines"))


class _FakeRunner:
    """Records every ``uv`` invocation; never runs anything."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str], Path]] = []
        self.returncode = returncode

    def __call__(self, argv, *, env, cwd) -> int:
        self.calls.append((tuple(argv), dict(env), Path(cwd)))
        return self.returncode


class _FakeDownloader:
    """Records every weight download request; never touches the network."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, engine_id: str, alias: str) -> str:
        self.calls.append((engine_id, alias))
        return f"fake {engine_id}/{alias}"


def _entry(
    engine_id: str,
    *,
    installed: bool,
    kind: str = "tts",
    models: tuple[ModelEntry, ...] = (),
    why_unavailable: str | None = "no venv",
) -> EngineEntry:
    return EngineEntry(
        engine_id=engine_id,
        backend_name=f"{engine_id}-backend",
        backend_version="1",
        installed=installed,
        kind=kind,
        why_unavailable=None if installed else why_unavailable,
        models=list(models),
    )


# ---------------------------------------------------------------------------
# hardware probes
# ---------------------------------------------------------------------------


def test_probe_nvidia_parses_the_smi_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real query shape: `name, memory.total(MiB), driver_version`."""
    monkeypatch.setattr(hardware, "_which", lambda name: True)
    monkeypatch.setattr(
        hardware,
        "_run",
        lambda argv, timeout=10.0: (
            "NVIDIA GeForce RTX 4060 Ti, 16380, 580.178.04\n"
            "NVIDIA GeForce RTX 3060, 12288, 550.54.14"
        ),
    )

    gpus = hardware.probe_nvidia()

    assert [gpu.name for gpu in gpus] == [
        "NVIDIA GeForce RTX 4060 Ti",
        "NVIDIA GeForce RTX 3060",
    ]
    assert gpus[0].vram_gb == 16.0  # 16380 MiB, rounded to one decimal
    assert gpus[0].kind == "nvidia"
    assert gpus[0].backend == "cuda"
    assert gpus[0].driver == "580.178.04"


def test_probe_nvidia_without_the_tool_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CPU-only box is a supported setup, not an error."""
    monkeypatch.setattr(hardware, "_which", lambda name: False)

    assert hardware.probe_nvidia() == ()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="probe_rocm only runs on Linux"
)
def test_probe_rocm_parses_the_smi_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hardware, "_which", lambda name: name == "rocm-smi")
    monkeypatch.setattr(
        hardware,
        "_run",
        lambda argv, timeout=10.0: (
            '{"card0": {"Card series": "Radeon RX 7900 XTX",'
            ' "VRAM Total Memory (B)": "25753026560"}}'
        ),
    )

    gpus = hardware.probe_rocm()

    assert len(gpus) == 1
    assert gpus[0].kind == "amd"
    assert gpus[0].name == "Radeon RX 7900 XTX"
    assert gpus[0].vram_gb == 24.0
    assert gpus[0].backend == "rocm"


DETECT_CASES: dict[str, tuple[bool, bool, bool, str, str | None, float | None]] = {
    # case: (nvidia, rocm, apple, accelerator, primary kind, unified_ram_gb)
    "nvidia": (True, True, False, "cuda", "nvidia", None),
    "rocm": (False, True, False, "rocm", "amd", None),
    "apple": (False, False, True, "mps", "apple", 32.0),
    "cpu": (False, False, False, "cpu", None, None),
}


@pytest.mark.parametrize("case", sorted(DETECT_CASES))
def test_detect_hardware_assembles_the_injected_probes(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Every OS/GPU branch is testable without owning that hardware."""
    nvidia, rocm, apple, accelerator, kind, unified = DETECT_CASES[case]
    monkeypatch.setattr(hardware, "probe_nvidia", lambda: (NVIDIA_GPU,) if nvidia else ())
    monkeypatch.setattr(hardware, "probe_rocm", lambda: (AMD_GPU,) if rocm else ())
    monkeypatch.setattr(hardware, "probe_apple", lambda ram: (APPLE_GPU,) if apple else ())
    monkeypatch.setattr(hardware, "_read_ram_gb", lambda: 32.0)

    profile = hardware.detect_hardware()

    assert profile.accelerator == accelerator
    assert profile.ram_gb == 32.0
    assert profile.unified_ram_gb == unified
    if kind is None:
        assert profile.gpus == ()
        assert profile.primary_gpu is None
        assert "CPU only" in profile.summary()
    else:
        assert profile.primary_gpu is not None
        assert profile.primary_gpu.kind == kind
    # NVIDIA wins over a ROCm installation when both answer: CUDA is verified.
    if case == "nvidia":
        assert AMD_GPU not in profile.gpus
        assert profile.gpu_vram_gb == 16.0


# ---------------------------------------------------------------------------
# recommend_profile: verified defaults only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_recommend_profile_selects_the_verified_defaults(name: str) -> None:
    profile = PROFILES[name]
    expected_profile, expected_pairs = EXPECTED_DEFAULTS[name]

    recommendation = hardware.recommend_profile(profile)

    assert recommendation.profile == expected_profile
    assert [(entry.engine_id, entry.device) for entry in recommendation.engines] == list(
        expected_pairs
    )
    # Only verified pairs are ever recommended for install.
    assert {entry.level for entry in recommendation.engines} == {hardware.VERIFIED}
    assert all(entry.models for entry in recommendation.engines)
    assert recommendation.download_gb > 0


@pytest.mark.parametrize("name", sorted(PROFILES))
def test_recommend_profile_never_defaults_an_experimental_pair(name: str) -> None:
    recommendation = hardware.recommend_profile(PROFILES[name])

    defaults = {(entry.engine_id, entry.device) for entry in recommendation.engines}
    offered = {(entry.engine_id, entry.device) for entry in recommendation.optional}
    assert defaults.isdisjoint(offered)
    for engine_id, device in defaults:
        assert hardware.pair_level(engine_id, device) == hardware.VERIFIED
    # Every offered pair carries its honest label from the support matrix, and a
    # verified pair is never merely "offered" while already being a default.
    for entry in recommendation.optional:
        assert entry.level == hardware.pair_level(entry.engine_id, entry.device)
        if entry.level == hardware.VERIFIED:
            assert (entry.engine_id, entry.device) not in defaults


def test_recommend_profile_small_vram_prefers_the_0_6b_aliases() -> None:
    recommendation = hardware.recommend_profile(NVIDIA_6GB)

    fast = next(entry for entry in recommendation.engines if entry.engine_id == "qwen3_tts_fast")
    assert fast.device == "cuda"
    assert fast.models == ("0.6b-base", "0.6b-custom")
    assert "4-7 GB" in fast.note
    assert any("Small-VRAM" in note for note in recommendation.notes)

    big = hardware.recommend_profile(NVIDIA_16GB)
    fast_big = next(entry for entry in big.engines if entry.engine_id == "qwen3_tts_fast")
    assert fast_big.models == ("1.7b-base", "1.7b-custom")


def test_recommend_profile_offers_mps_only_as_experimental() -> None:
    recommendation = hardware.recommend_profile(APPLE_SILICON)

    assert [entry.engine_id for entry in recommendation.engines] == ["kokoro", "phonon"]
    assert {entry.device for entry in recommendation.engines} == {"cpu"}
    offered = {(entry.engine_id, entry.device, entry.level) for entry in recommendation.optional}
    assert ("kokoro", "mps", hardware.EXPERIMENTAL) in offered
    assert ("phonon", "mps", hardware.EXPERIMENTAL) in offered
    assert ("qwen3_tts", "mps", hardware.EXPERIMENTAL) in offered
    assert any("experimental" in note.lower() for note in recommendation.notes)


def test_recommend_profile_offers_rocm_only_as_experimental() -> None:
    recommendation = hardware.recommend_profile(AMD_ROCM)

    # On AMD the verified default is the CPU engines; ROCm is an opt-in.
    assert {entry.device for entry in recommendation.engines} == {"cpu"}
    assert [
        (entry.engine_id, entry.device, entry.level) for entry in recommendation.optional
    ] == [("qwen3_tts", "rocm", hardware.EXPERIMENTAL)]
    assert any("ROCm" in note for note in recommendation.notes)


def test_recommend_profile_marks_qwen3_tts_cpu_optional_but_verified() -> None:
    """Wave 0 ran the CPU path over real IPC, so the pair is verified — but the
    CPU engines stay the default, with an honest slowness note."""
    recommendation = hardware.recommend_profile(CPU_ONLY)

    assert [entry.engine_id for entry in recommendation.engines] == ["kokoro", "phonon"]
    optional = {entry.engine_id: entry for entry in recommendation.optional}
    assert optional["qwen3_tts"].device == "cpu"
    assert optional["qwen3_tts"].level == hardware.VERIFIED
    assert "slow" in optional["qwen3_tts"].note.lower()
    assert any("No accelerator" in note for note in recommendation.notes)


# ---------------------------------------------------------------------------
# resolve_device
# ---------------------------------------------------------------------------


def test_resolve_device_explicit_per_engine_override_wins(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    settings = _config(device="cuda", devices={"qwen3_tts": "cpu"})

    resolved = hardware.resolve_device(
        "qwen3_tts", registry.get("qwen3_tts").manifest, settings, NVIDIA_16GB
    )

    assert resolved == "cpu"


def test_resolve_device_non_auto_global_preference_wins(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    # qwen3_tts defaults to cuda; the operator asked for cpu globally.
    settings = _config(device="cpu")

    resolved = hardware.resolve_device(
        "qwen3_tts", registry.get("qwen3_tts").manifest, settings, NVIDIA_16GB
    )

    assert resolved == "cpu"


def test_resolve_device_falls_back_to_the_manifest_default(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    settings = _config()  # auto

    kokoro = hardware.resolve_device(
        "kokoro", registry.get("kokoro").manifest, settings, NVIDIA_16GB
    )
    fast = hardware.resolve_device(
        "qwen3_tts_fast", registry.get("qwen3_tts_fast").manifest, settings, NVIDIA_16GB
    )

    # kokoro's manifest default is cpu even on a CUDA host: it holds 0 MB.
    assert kokoro == "cpu"
    assert fast == "cuda"


def test_resolve_device_auto_falls_back_to_cpu_on_a_cpu_host(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    resolved = hardware.resolve_device(
        "qwen3_tts", registry.get("qwen3_tts").manifest, _config(), CPU_ONLY
    )

    assert resolved == "cpu"


@pytest.mark.parametrize(
    ("settings", "profile"),
    [
        (lambda: _config(), CPU_ONLY),
        (lambda: _config(device="cpu"), CPU_ONLY),
    ],
    ids=["auto-on-cpu-host", "global-cpu"],
)
def test_resolve_device_refuses_qwen3_tts_fast_off_cuda(tmp_path: Path, settings, profile) -> None:
    """faster-qwen3-tts has no CPU/MPS/ROCm path at all; the error says so."""
    registry = _registry(tmp_path)

    with pytest.raises(AgentVoiceError) as excinfo:
        hardware.resolve_device(
            "qwen3_tts_fast", registry.get("qwen3_tts_fast").manifest, settings(), profile
        )

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "qwen3_tts_fast" in excinfo.value.message
    assert excinfo.value.suggestion


def test_resolve_device_refuses_an_explicit_cuda_on_a_cpu_host(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(AgentVoiceError) as excinfo:
        hardware.resolve_device(
            "kokoro", registry.get("kokoro").manifest, _config(device="cuda"), CPU_ONLY
        )

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "not available on this host" in excinfo.value.message
    assert "cpu" in excinfo.value.suggestion


def test_resolve_device_refuses_an_unsupported_pair(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    with pytest.raises(AgentVoiceError) as excinfo:
        hardware.resolve_device(
            "phonon",
            registry.get("phonon").manifest,
            _config(devices={"phonon": "cuda"}),
            NVIDIA_16GB,
        )

    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "does not support device 'cuda'" in excinfo.value.message
    # The suggestion names every device the manifest does declare.
    assert "cpu" in excinfo.value.suggestion
    assert "mps" in excinfo.value.suggestion


# ---------------------------------------------------------------------------
# setup --dry-run through the CLI
# ---------------------------------------------------------------------------


def _setup_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Point the CLI at a temp home and a temp engines tree, with NVIDIA mocked."""
    home = tmp_path / "home"
    home.mkdir()
    engines = _engine_tree(tmp_path / "engines")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.setenv("DATA_DIR", str(home / "data"))
    monkeypatch.setenv("ENGINES_DIR", str(engines))
    monkeypatch.delenv("AGENT_VOICE_DEVICE", raising=False)
    monkeypatch.delenv("AGENT_VOICE_DEVICES", raising=False)
    monkeypatch.setattr(hardware, "detect_hardware", lambda: NVIDIA_16GB)
    return home, engines, home / "data"


def test_setup_dry_run_prints_the_plan_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, engines, data = _setup_env(tmp_path, monkeypatch)
    before = sorted(path.name for path in engines.iterdir())

    result = runner.invoke(app, ["setup", "--dry-run"])

    assert result.exit_code == 0, result.output
    output = result.output
    # The detected host and the recommendation it drives.
    assert "hardware" in output
    assert "accelerator" in output
    assert "cuda" in output
    assert "recommended (profile nvidia-gpu" in output
    assert "verified pairs only" in output
    assert "qwen3_tts_fast" in output
    assert "1.7b-base" in output
    # The exact `.env` diff, without writing it.
    assert ".env" in output
    assert f"AGENT_VOICE_HOME={home}" in output
    assert "dry run: nothing was installed, downloaded or written" in output

    assert not (home / ".env").exists()
    assert not data.exists()
    assert not (engines / "kokoro" / ".venv").exists()
    assert not (engines / "qwen3_tts_fast" / ".venv").exists()
    assert sorted(path.name for path in engines.iterdir()) == before


def test_setup_dry_run_does_not_read_or_add_a_repo_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run reports "add" for every key: the temp home has no `.env`."""
    home, _engines, _data = _setup_env(tmp_path, monkeypatch)

    result = runner.invoke(app, ["setup", "--dry-run", "--profile", "cpu-only"])

    assert result.exit_code == 0, result.output
    assert f".env: {home / '.env'}" in result.output
    assert "add    DATA_DIR=" in result.output
    # cpu-only pins the CPU engines even on this NVIDIA host.
    assert "kokoro" in result.output
    assert "qwen3_tts_fast" not in result.output.split("selected for install")[1]


# ---------------------------------------------------------------------------
# --yes never downloads without --download
# ---------------------------------------------------------------------------


def _plan(
    tmp_path: Path, *, download: bool, dry_run: bool = False
) -> setup_wizard.SetupPlan:
    settings = _config(
        agent_home=tmp_path / "home",
        data_dir=tmp_path / "data",
        engines_dir=_engine_tree(tmp_path / "engines"),
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return setup_wizard.plan_setup(
        settings, NVIDIA_16GB, download=download, dry_run=dry_run
    )


def test_yes_without_download_never_fetches_weights(tmp_path: Path) -> None:
    """`--yes` installs engines and writes `.env`; it never downloads weights."""
    plan = _plan(tmp_path, download=False)
    assert plan.selected  # there is something that *could* be downloaded
    uv_runner = _FakeRunner()
    downloader = _FakeDownloader()

    result = setup_wizard.apply_setup(
        plan, runner=uv_runner, downloader=downloader, yes=True
    )

    assert downloader.calls == []
    assert len(uv_runner.calls) == len(plan.installs)
    assert all("--locked" in argv for argv, _env, _cwd in uv_runner.calls)
    assert all(argv[:2] == ("uv", "sync") for argv, _env, _cwd in uv_runner.calls)
    assert any("no --download" in step.detail for step in result.steps)
    assert result.env_written is True
    assert (tmp_path / "home" / ".env").is_file()
    assert result.ok is True


def test_download_flag_fetches_every_selected_alias(tmp_path: Path) -> None:
    plan = _plan(tmp_path, download=True)
    uv_runner = _FakeRunner()
    downloader = _FakeDownloader()

    setup_wizard.apply_setup(plan, runner=uv_runner, downloader=downloader, yes=True)

    expected = [
        (choice.engine_id, alias) for choice in plan.selected for alias in choice.models
    ]
    assert downloader.calls == expected
    assert expected  # the fixtures really declare weights


def test_dry_run_plan_applies_nothing(tmp_path: Path) -> None:
    """`plan.dry_run` returns before the first injected call, by construction."""
    plan = _plan(tmp_path, download=True, dry_run=True)
    uv_runner = _FakeRunner()
    downloader = _FakeDownloader()

    result = setup_wizard.apply_setup(
        plan, runner=uv_runner, downloader=downloader, yes=True
    )

    assert uv_runner.calls == []
    assert downloader.calls == []
    assert result.env_written is False
    assert not (tmp_path / "home" / ".env").exists()
    assert not (tmp_path / "data").exists()
    assert [step.name for step in result.steps] == ["plan"]
    assert "nothing installed" in result.steps[0].detail


# ---------------------------------------------------------------------------
# .env writer
# ---------------------------------------------------------------------------


def test_env_writer_backs_up_to_dot_bak(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = "# a hand-written comment\nDATA_DIR=./data\nMY_UNKNOWN_KEY=1\n"
    path.write_text(original, encoding="utf-8")

    result = envfile.write_env(path, {"DATA_DIR": str(tmp_path / "abs-data")})

    assert result.written is True
    assert result.backup == tmp_path / ".env.bak"
    assert result.backup.read_text("utf-8") == original
    body = path.read_text("utf-8")
    assert "MY_UNKNOWN_KEY=1" in body
    assert "# a hand-written comment" in body
    assert f"DATA_DIR={tmp_path / 'abs-data'}" in body
    assert f"update DATA_DIR: ./data -> {tmp_path / 'abs-data'}" in result.diff.lines()


def test_env_writer_never_overwrites_an_existing_backup(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("DATA_DIR=./data\n", encoding="utf-8")
    operator_copy = tmp_path / ".env.bak"
    operator_copy.write_text("the operator's only copy\n", encoding="utf-8")
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    assert envfile.backup_path(path, now=now) == tmp_path / ".env.20260102T030405Z.bak"

    result = envfile.write_env(path, {"DATA_DIR": "/srv/agent-voice-data"}, now=now)

    assert operator_copy.read_text("utf-8") == "the operator's only copy\n"
    assert result.backup == tmp_path / ".env.20260102T030405Z.bak"
    assert result.backup.read_text("utf-8") == "DATA_DIR=./data\n"


def test_env_writer_keeps_every_other_line_in_order(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    lines = [
        "# --- Defaults ---",
        "AGENT_VOICE_DEFAULT_ENGINE=kokoro",
        "",
        "# --- Paths ---",
        f"DATA_DIR={tmp_path / 'old'}",
        "ENGINES_DIR=./engines",
        "# trailing comment",
        "AGENT_VOICE_ALLOWED_HOSTS=testserver",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    envfile.write_env(path, {"DATA_DIR": str(tmp_path / "new")})

    after = path.read_text("utf-8").splitlines()
    expected = [
        f"DATA_DIR={tmp_path / 'new'}" if line.startswith("DATA_DIR=") else line
        for line in lines
    ]
    assert after == expected


def test_env_writer_promotes_a_commented_assignment_in_place(tmp_path: Path) -> None:
    """The v0.5.0 `.env.example` ships `# DATA_DIR=./data`; setup fills it in."""
    path = tmp_path / ".env"
    path.write_text("# --- Paths ---\n# DATA_DIR=./data\n", encoding="utf-8")

    result = envfile.write_env(path, {"DATA_DIR": "/abs/data"})

    body = path.read_text("utf-8")
    assert "DATA_DIR=/abs/data" in body
    assert "# DATA_DIR=./data" not in body
    assert body.splitlines()[0] == "# --- Paths ---"
    change = result.diff.changes[0]
    assert change.from_comment is True
    assert change.old == "./data"
    assert change.pending is True


def test_env_writer_mode_is_0600(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("DATA_DIR=./data\n", encoding="utf-8")
    path.chmod(0o644)

    result = envfile.write_env(path, {"DATA_DIR": "/abs/data"})

    assert path.stat().st_mode & 0o777 == 0o600
    assert result.backup is not None
    assert result.backup.stat().st_mode & 0o777 == 0o600


def test_env_writer_only_tightens_the_mode_when_values_match(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("DATA_DIR=/abs/data\n", encoding="utf-8")
    path.chmod(0o644)

    result = envfile.write_env(path, {"DATA_DIR": "/abs/data"})

    assert result.written is False
    assert result.mode_fixed is True
    assert result.backup is None
    assert path.stat().st_mode & 0o777 == 0o600


def test_env_writer_migrates_the_shipped_env_example(tmp_path: Path) -> None:
    """Copying `.env.example` to `.env` and running setup must lose nothing.

    The example ships `# DATA_DIR=./data` / `# ENGINES_DIR=./engines` and
    `AGENT_VOICE_DEVICE=auto`; setup promotes/replaces exactly those and leaves
    every other comment byte for byte.
    """
    source = (REPO_ROOT / ".env.example").read_text("utf-8")
    path = tmp_path / ".env"
    path.write_text(source, encoding="utf-8")
    updated_keys = {
        "AGENT_VOICE_HOME",
        "DATA_DIR",
        "ENGINES_DIR",
        "AGENT_VOICE_DEVICE",
    }
    updates = {
        "AGENT_VOICE_HOME": str(tmp_path / "home"),
        "DATA_DIR": str(tmp_path / "data"),
        "ENGINES_DIR": str(tmp_path / "engines"),
        "AGENT_VOICE_DEVICE": "cpu",
    }

    result = envfile.write_env(path, updates)

    body = path.read_text("utf-8")
    before = source.splitlines()
    after = body.splitlines()

    def key_of(line: str) -> str | None:
        for pattern in (_ASSIGNMENT, _COMMENTED_ASSIGNMENT):
            match = pattern.match(line)
            if match is not None:
                return match.group("key")
        return None

    lost = [line for line in before if line.strip() and line not in after]
    # A line may only disappear when it is an assignment (or a commented-out
    # assignment) for a key setup just rewrote.
    assert all(key_of(line) in updated_keys for line in lost), lost
    assert any("# --- Paths" in line for line in after)

    # Exactly one active assignment per key: promoting never duplicates.
    active = [
        key_of(line)
        for line in after
        if key_of(line) is not None and not line.lstrip().startswith("#")
    ]
    assert sorted(active) == sorted(set(active))
    for key, value in updates.items():
        assert f"{key}={value}" in after
    promoted = [change for change in result.diff.changes if change.key != "AGENT_VOICE_DEVICE"]
    assert promoted and all(change.from_comment for change in promoted)
    commented_keys = {
        key_of(line) for line in before if line.lstrip().startswith("#") and key_of(line)
    }
    for change in result.diff.changes:
        assert change.from_comment is (change.key in commented_keys), change.key
    assert path.stat().st_mode & 0o777 == 0o600
    assert result.backup is not None
    assert result.backup.read_text("utf-8") == source
    assert body.endswith("\n")


def test_env_diff_masks_the_api_key(tmp_path: Path) -> None:
    secret = "sk-live-0123456789"
    diff = envfile.plan_env_update(
        tmp_path / ".env", {"AGENT_VOICE_API_KEY": secret, "DATA_DIR": "/abs/data"}
    )

    rendered = "\n".join(diff.lines())
    assert envfile.MASK in rendered
    assert f"AGENT_VOICE_API_KEY={envfile.MASK}" in rendered
    assert secret not in rendered
    assert "/abs/data" in rendered


def test_plan_env_updates_are_absolute_and_name_the_defaults(tmp_path: Path) -> None:
    plan = _plan(tmp_path, download=False)

    updates = dict(plan.env_updates)
    for key in ("AGENT_VOICE_HOME", "DATA_DIR", "ENGINES_DIR"):
        assert Path(updates[key]).is_absolute(), key
    assert updates["AGENT_VOICE_HOME"] == str(tmp_path / "home")
    assert plan.env.path == tmp_path / "home" / ".env"
    assert plan.env.has_changes is True
    # A preset-voice engine wins over the CUDA one: a fresh install must be able
    # to speak without cloning a reference clip first.
    assert updates["AGENT_VOICE_DEFAULT_ENGINE"] == "kokoro"
    assert updates["AGENT_VOICE_DEFAULT_ASR_ENGINE"] == "phonon"


def test_plan_setup_selects_the_now_verified_qwen3_tts_cpu_pair(tmp_path: Path) -> None:
    settings = _config(
        agent_home=tmp_path / "home",
        data_dir=tmp_path / "data",
        engines_dir=_engine_tree(tmp_path / "engines"),
    )
    (tmp_path / "home").mkdir()

    plan = setup_wizard.plan_setup(
        settings, CPU_ONLY, engines="qwen3_tts", device="cpu"
    )

    assert [(choice.engine_id, choice.device, choice.level) for choice in plan.selected] == [
        ("qwen3_tts", "cpu", hardware.VERIFIED)
    ]
    assert plan.refusal == ""
    # The canonical qwen3_tts project locks cu128, so the CPU install can only be
    # the variants/cpu project — and this tree has no such variant. The plan says
    # so instead of inventing one.
    install = plan.install_for("qwen3_tts")
    assert install is not None
    assert install.installable is False
    assert "no 'cpu' variant" in install.problem
    assert "cuda" in install.problem
    assert any("no 'cpu' variant" in warning for warning in plan.warnings)


def test_plan_setup_never_selects_an_experimental_pair(tmp_path: Path) -> None:
    """Asking for an experimental pair is refused with the deliberate-install hint."""
    settings = _config(
        agent_home=tmp_path / "home",
        data_dir=tmp_path / "data",
        engines_dir=_engine_tree(tmp_path / "engines"),
    )
    (tmp_path / "home").mkdir()

    plan = setup_wizard.plan_setup(
        settings, APPLE_SILICON, engines="qwen3_tts", device="mps"
    )

    assert plan.selected == ()
    assert plan.installs == ()
    assert plan.refusal
    assert any(
        "qwen3_tts@mps is experimental" in warning for warning in plan.warnings
    )
    assert any("--experimental" in warning for warning in plan.warnings)


# ---------------------------------------------------------------------------
# doctor severity
# ---------------------------------------------------------------------------


def test_engine_checks_calls_an_unselected_engine_optional(tmp_path: Path) -> None:
    entries = [
        _entry("mock", installed=True),
        _entry("phonon", installed=False),
        _entry("qwen3_tts", installed=False),
    ]

    checks = cli._engine_checks(entries, tmp_path, {"qwen3_tts"})

    by_name = {check.name: check for check in checks}
    assert by_name["engines: mock"].status == "PASS"
    assert by_name["engines: qwen3_tts"].status == "WARN"
    assert by_name["engines: phonon"].status == "INFO"
    assert "optional" in by_name["engines: phonon"].detail
    assert "engines install phonon" in by_name["engines: phonon"].fix
    assert "engines install qwen3_tts" in by_name["engines: qwen3_tts"].fix


def test_model_checks_separates_optional_weights_from_required_ones() -> None:
    def model(engine_id: str, alias: str, hf_repo: str) -> ModelEntry:
        return ModelEntry(
            engine_id=engine_id,
            alias=alias,
            hf_repo=hf_repo,
            quality_tier="high",
            est_vram_gb=4.4,
        )

    entries = [
        _entry(
            "mock",
            installed=True,
            models=(model("mock", "mock-tone", "local/mock-tone"),),
        ),
        _entry(
            "qwen3_tts",
            installed=True,
            models=(model("qwen3_tts", "1.7b-base", "Qwen/Qwen3-TTS-12Hz-1.7B-Base"),),
        ),
        _entry(
            "phonon",
            installed=False,
            kind="asr",
            models=(model("phonon", "phonon-2.5", "FermionResearch/Phonon-2.5"),),
        ),
    ]

    checks = cli._model_checks(entries, {"mock", "qwen3_tts"}, {"mock", "qwen3_tts"})

    assert [check.name for check in checks] == ["models: qwen3_tts/1.7b-base", "models"]
    assert checks[0].status == "WARN"
    assert checks[1].status == "INFO"
    assert "optional" in checks[1].detail
    assert "phonon/phonon-2.5" in checks[1].detail

    # Naming phonon as installed/selected promotes its weights to a WARN row.
    promoted = cli._model_checks(entries, {"mock", "phonon"}, {"phonon"})
    assert {check.status for check in promoted if check.name.startswith("models:")} == {
        "WARN"
    }


def test_probe_gpu_without_an_accelerator_is_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CPU box is a supported setup: INFO plus a nudge, never WARN/FAIL."""
    monkeypatch.setattr(shutil, "which", lambda name: None)

    check = cli._probe_gpu()

    assert check.status == "INFO"
    assert "no nvidia-smi" in check.detail
    assert "install the NVIDIA driver" in check.fix


def test_info_is_a_first_class_doctor_status() -> None:
    assert cli._STATUS_RANK == {"PASS": 0, "INFO": 1, "WARN": 2, "FAIL": 3}
    assert cli._CHECK_COLOUR["INFO"] == cli.typer.colors.CYAN
    assert cli._Check("engines: phonon", "INFO", "optional, not installed").status == "INFO"


def test_print_checks_shows_an_info_row_and_its_fix(capsys: pytest.CaptureFixture[str]) -> None:
    cli._print_checks(
        [
            cli._Check("engines: mock", "PASS", "mock 1"),
            cli._Check("engines: phonon", "INFO", "optional, not installed", "install it"),
            cli._Check("gpu", "WARN", "absent", "install a driver"),
        ]
    )

    out = capsys.readouterr().out
    assert "INFO" in out
    assert "fix: install it" in out
    assert "fix: install a driver" in out
    # PASS rows never carry a fix line.
    assert out.count("fix:") == 2


def test_doctor_summary_counts_optional_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary is `N checks, X optional, Y warning(s)`; only FAIL exits 1."""
    home = tmp_path / "home"
    home.mkdir()
    engines = _engine_tree(tmp_path / "engines")
    monkeypatch.setenv("AGENT_VOICE_HOME", str(home))
    monkeypatch.setenv("ENGINES_DIR", str(engines))
    monkeypatch.setenv("DATA_DIR", str(home / "data"))
    monkeypatch.setenv("AGENT_VOICE_DEFAULT_ENGINE", "mock")
    monkeypatch.setenv("AGENT_VOICE_DEFAULT_ASR_ENGINE", "mock_asr")

    entries = [
        _entry(
            "mock",
            installed=True,
            models=(
                ModelEntry(
                    engine_id="mock",
                    alias="mock-tone",
                    hf_repo="local/mock-tone",
                    quality_tier="fast",
                    est_vram_gb=0.0,
                ),
            ),
        ),
        _entry("phonon", installed=False, kind="asr"),
    ]
    manifests = {
        "mock": EngineManifest(
            engine_id="mock",
            backend_name="mock",
            supported_devices=["cpu"],
            default_device="cpu",
            device="cpu",
        ),
        "phonon": EngineManifest(
            engine_id="phonon",
            backend_name="fermion-research",
            kind="asr",
            supported_devices=["cpu", "mps"],
            default_device="cpu",
            device="cpu",
        ),
    }

    class _Registry:
        def __init__(self, engines_dir: Path) -> None:
            self.engines_dir = Path(engines_dir)

        def ids(self) -> list[str]:
            return sorted(manifests)

        def get(self, engine_id: str) -> SimpleNamespace:
            return SimpleNamespace(manifest=manifests[engine_id])

        def list_entries(self, **_kwargs: object) -> list[EngineEntry]:
            return list(entries)

    monkeypatch.setattr(engine_registry, "EngineRegistry", _Registry)
    monkeypatch.setattr(hardware, "detect_hardware", lambda: CPU_ONLY)
    # Tool rows must not depend on the host this suite runs on.
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: real_which(name) or sys.executable)
    monkeypatch.setattr(cli, "_probe_gpu", lambda: cli._Check("gpu", "INFO", "no gpu", "nudge"))
    monkeypatch.setattr(cli, "_dub_filters_check", lambda: cli._Check("dub filters", "PASS"))
    monkeypatch.setattr(cli, "_espeak_check", lambda entries: None)
    monkeypatch.setattr(cli, "_check_port", lambda *args, **kwargs: cli._Check("port", "PASS"))

    captured: list[cli._Check] = []
    real_print = cli._print_checks

    def capture(checks: list[cli._Check]) -> None:
        captured.extend(checks)
        real_print(checks)

    monkeypatch.setattr(cli, "_print_checks", capture)

    result = runner.invoke(app, ["doctor"])

    output = result.output + (getattr(result, "stderr", "") or "")
    assert result.exit_code == 0, output
    assert "no failures" in output
    optional = [check for check in captured if check.status == "INFO"]
    warnings = [check for check in captured if check.status == "WARN"]
    assert not [check for check in captured if check.status == "FAIL"]
    assert optional, "the INFO row for the uninstalled, unselected ASR engine is missing"
    expected = (
        f"doctor: {len(captured)} checks, {len(optional)} optional, "
        f"{len(warnings)} warning(s), no failures"
    )
    assert expected in output


# ---------------------------------------------------------------------------
# installer shell syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", ["sh", "bash"])
def test_install_sh_is_valid_posix_and_bash(shell: str) -> None:
    binary = shutil.which(shell)
    if binary is None:
        pytest.skip(f"{shell} is not on PATH")
    assert INSTALL_SH.is_file()

    result = subprocess.run(
        [binary, "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        shell=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    # It must stay `#!/bin/sh`: `curl | sh` runs it under dash/ash/macOS sh.
    assert INSTALL_SH.read_text("utf-8").startswith("#!/bin/sh\n")
