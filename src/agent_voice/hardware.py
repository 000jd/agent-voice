"""Hardware detection, device policy and setup recommendations.

Zero torch, zero engine imports: this module runs inside the core daemon and the
CLI, where importing torch is forbidden. Every probe is a plain subprocess or a
stdlib read, and every OS/GPU branch is injectable so the unit tests can cover
Apple, ROCm and NVIDIA-6GB without owning that hardware.

The ``DEVICE_SUPPORT`` table is the honest support matrix. ``verified`` means
"installed and actually run on the reference Linux + RTX 4060 Ti host"; anything
else is ``experimental`` (wired and unit-tested, not run here) or
``unsupported`` (the package metadata does not claim it). See
``docs/phase0-devices-report.md`` for the evidence behind every row.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .schemas import (
    AgentVoiceError,
    ComputeDevice,
    EngineManifest,
    ErrorCode,
)

SupportLevel = Literal["verified", "experimental", "unsupported"]
Accelerator = Literal["cuda", "mps", "rocm", "cpu"]
GpuKind = Literal["nvidia", "amd", "apple"]

# The single support matrix. Update it only with a line in the Phase 0 report.
DEVICE_SUPPORT: dict[str, dict[ComputeDevice, SupportLevel]] = {
    "kokoro": {"cpu": "verified", "cuda": "experimental", "mps": "experimental"},
    "phonon": {"cpu": "verified", "mps": "experimental"},
    "qwen3_tts": {
        # Wave 0 ran both the model path and the shipped worker over real IPC on
        # CPU (float32): 0.196x realtime, 5.55 GiB RSS, valid 24 kHz WAV.
        "cpu": "verified",
        "cuda": "verified",
        "mps": "experimental",
        "rocm": "experimental",
    },
    "qwen3_tts_fast": {"cuda": "verified"},
    "mock": {"cpu": "verified"},
    "mock_asr": {"cpu": "verified"},
}

# Approximate download sizes in GB, for the setup prompt only. Deliberately
# coarse: the exact size is in the model card and the doctor prints the cache.
MODEL_DOWNLOAD_GB: dict[str, float] = {
    "kokoro-82m": 0.33,
    "phonon-2.5": 0.16,
    "phonon-2.5-en": 0.16,
    "phonon-2": 0.17,
    "1.7b-base": 3.5,
    "1.7b-custom": 3.5,
    "1.7b-design": 3.5,
    "0.6b-base": 1.4,
    "0.6b-custom": 1.4,
}

VERIFIED = "verified"
EXPERIMENTAL = "experimental"
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class GpuInfo:
    kind: GpuKind
    name: str
    vram_gb: float | None = None
    driver: str = ""
    backend: str = ""


@dataclass(frozen=True)
class HardwareProfile:
    os: str
    arch: str
    cpu_cores: int
    ram_gb: float | None
    python_version: str
    has_uv: bool
    has_ffmpeg: bool
    has_espeak_ng: bool
    gpus: tuple[GpuInfo, ...] = ()
    accelerator: Accelerator = "cpu"
    unified_ram_gb: float | None = None

    @property
    def primary_gpu(self) -> GpuInfo | None:
        return self.gpus[0] if self.gpus else None

    @property
    def gpu_vram_gb(self) -> float | None:
        gpu = self.primary_gpu
        return gpu.vram_gb if gpu else None

    def summary(self) -> str:
        bits = [f"{self.os}/{self.arch}", f"{self.cpu_cores} cores"]
        if self.ram_gb is not None:
            bits.append(f"{self.ram_gb:.0f} GB RAM")
        gpu = self.primary_gpu
        if gpu is not None:
            vram = f" {gpu.vram_gb:.1f} GB" if gpu.vram_gb else ""
            bits.append(f"{gpu.name}{vram}")
        else:
            bits.append("CPU only")
        return ", ".join(bits)


@dataclass(frozen=True)
class DevicePair:
    engine_id: str
    device: ComputeDevice
    level: SupportLevel
    reason: str = ""


@dataclass(frozen=True)
class EngineRecommendation:
    engine_id: str
    device: ComputeDevice
    models: tuple[str, ...] = ()
    download_gb: float = 0.0
    level: SupportLevel = VERIFIED
    note: str = ""


@dataclass(frozen=True)
class SetupRecommendation:
    profile: str
    engines: tuple[EngineRecommendation, ...]
    optional: tuple[EngineRecommendation, ...] = ()
    notes: tuple[str, ...] = ()
    pairs: tuple[DevicePair, ...] = ()

    @property
    def download_gb(self) -> float:
        return sum(engine.download_gb for engine in self.engines)


def pair_level(engine_id: str, device: ComputeDevice) -> SupportLevel:
    return DEVICE_SUPPORT.get(engine_id, {}).get(device, UNSUPPORTED)


def all_pairs(engine_ids: list[str] | None = None) -> tuple[DevicePair, ...]:
    pairs: list[DevicePair] = []
    for engine_id, devices in DEVICE_SUPPORT.items():
        if engine_ids is not None and engine_id not in engine_ids:
            continue
        for device, level in devices.items():
            pairs.append(DevicePair(engine_id=engine_id, device=device, level=level))
    return tuple(pairs)


def _which(name: str) -> bool:
    return shutil.which(name) is not None


def _run(argv: list[str], *, timeout: float = 10.0) -> str:
    """Run an argv list, never a shell string, and never raise."""
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            check=False,
            shell=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    stdout = result.stdout or ""
    if isinstance(stdout, bytes):  # defensive: every call passes text=True today
        stdout = stdout.decode("utf-8", "replace")
    return stdout.strip() if result.returncode == 0 else ""


def _read_ram_gb() -> float | None:
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/meminfo").read_text("utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / 1024 / 1024, 1)
        except (OSError, ValueError):
            return None
        return None
    if sys.platform == "darwin":
        value = _run(["sysctl", "-n", "hw.memsize"])
        try:
            return round(int(value) / 1024**3, 1)
        except (TypeError, ValueError):
            return None
    if sys.platform == "win32":
        try:
            import ctypes

            class _MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatus()
            status.dwLength = ctypes.sizeof(_MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return round(status.ullTotalPhys / 1024**3, 1)
        except Exception:
            return None
    return None


def probe_nvidia() -> tuple[GpuInfo, ...]:
    """Parse nvidia-smi, if it exists. Never required for a supported setup."""
    if not _which("nvidia-smi"):
        return ()
    output = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    gpus: list[GpuInfo] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        try:
            vram = round(float(parts[1]) / 1024, 1)
        except ValueError:
            vram = None
        gpus.append(
            GpuInfo(
                kind="nvidia",
                name=parts[0],
                vram_gb=vram,
                driver=parts[2] if len(parts) > 2 else "",
                backend="cuda",
            )
        )
    return tuple(gpus)


def probe_rocm() -> tuple[GpuInfo, ...]:
    """Parse rocm-smi/rocminfo on Linux. A missing tool is not an error."""
    if not sys.platform.startswith("linux"):
        return ()
    if _which("rocm-smi"):
        raw = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        gpus: list[GpuInfo] = []
        for card, values in parsed.items():
            if not isinstance(values, dict):
                continue
            name = str(
                values.get("Card series") or values.get("Card SKU") or card
            ).strip()
            total = values.get("VRAM Total Memory (B)")
            try:
                vram = round(int(total) / 1024**3, 1) if total is not None else None
            except (TypeError, ValueError):
                vram = None
            gpus.append(GpuInfo(kind="amd", name=name, vram_gb=vram, backend="rocm"))
        if gpus:
            return tuple(gpus)
    if _which("rocminfo"):
        text = _run(["rocminfo"])
        names = re.findall(r"Marketing Name:\s*(.+)", text)
        sizes = re.findall(r"Size:\s*(\d+)\s*KB", text)
        gpus = []
        for index, name in enumerate(names):
            vram = None
            if index < len(sizes):
                try:
                    vram = round(int(sizes[index]) / 1024 / 1024, 1)
                except ValueError:
                    vram = None
            gpus.append(GpuInfo(kind="amd", name=name.strip(), vram_gb=vram, backend="rocm"))
        return tuple(gpus)
    return ()


def probe_apple(ram_gb: float | None) -> tuple[GpuInfo, ...]:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return ()
    return (
        GpuInfo(
            kind="apple",
            name="Apple Silicon",
            vram_gb=None,
            backend="mps",
        ),
    )


def detect_hardware() -> HardwareProfile:
    """Probe the host once. Pure stdlib + argv subprocesses, no torch."""
    ram = _read_ram_gb()
    nvidia = probe_nvidia()
    apple = probe_apple(ram)
    rocm = probe_rocm() if not nvidia else ()
    gpus = nvidia or rocm or apple
    if nvidia:
        accelerator: Accelerator = "cuda"
    elif rocm:
        accelerator = "rocm"
    elif apple:
        accelerator = "mps"
    else:
        accelerator = "cpu"

    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    if sys.platform.startswith("linux"):
        os_name = "linux"
    elif sys.platform == "darwin":
        os_name = "macos"
    else:
        os_name = "windows"

    return HardwareProfile(
        os=os_name,
        arch=arch,
        cpu_cores=os.cpu_count() or 1,
        ram_gb=ram,
        python_version=platform.python_version(),
        has_uv=_which("uv"),
        has_ffmpeg=_which("ffmpeg"),
        has_espeak_ng=_which("espeak-ng"),
        gpus=gpus,
        accelerator=accelerator,
        unified_ram_gb=ram if apple else None,
    )


def _engine(
    engine_id: str,
    device: ComputeDevice,
    models: tuple[str, ...],
    note: str = "",
    level: SupportLevel = VERIFIED,
) -> EngineRecommendation:
    return EngineRecommendation(
        engine_id=engine_id,
        device=device,
        models=models,
        download_gb=round(sum(MODEL_DOWNLOAD_GB.get(alias, 0.0) for alias in models), 2),
        level=level,
        note=note,
    )


def recommend_profile(hw: HardwareProfile) -> SetupRecommendation:
    """Verified-only defaults; experimental pairs are offered, never chosen."""
    notes: list[str] = []
    optional: list[EngineRecommendation] = []

    if hw.accelerator == "cuda" and (hw.gpu_vram_gb or 0) >= 8:
        engines = [
            _engine("qwen3_tts_fast", "cuda", ("1.7b-base", "1.7b-custom")),
            _engine("kokoro", "cpu", ("kokoro-82m",)),
            _engine("phonon", "cpu", ("phonon-2.5",)),
        ]
        profile = "nvidia-gpu"
    elif hw.accelerator == "cuda":
        engines = [
            _engine(
                "qwen3_tts_fast",
                "cuda",
                ("0.6b-base", "0.6b-custom"),
                note="4-7 GB VRAM class; the 1.7B aliases may not fit",
            ),
            _engine("kokoro", "cpu", ("kokoro-82m",)),
            _engine("phonon", "cpu", ("phonon-2.5",)),
        ]
        profile = "nvidia-gpu"
        notes.append("Small-VRAM NVIDIA profile: prefer the 0.6B Qwen aliases.")
    elif hw.accelerator == "mps":
        engines = [
            _engine("kokoro", "cpu", ("kokoro-82m",)),
            _engine("phonon", "cpu", ("phonon-2.5",)),
        ]
        profile = "apple-silicon"
        optional.extend(
            [
                _engine("kokoro", "mps", ("kokoro-82m",), level=EXPERIMENTAL),
                _engine("phonon", "mps", ("phonon-2.5",), level=EXPERIMENTAL),
                _engine("qwen3_tts", "mps", ("0.6b-base",), level=EXPERIMENTAL),
            ]
        )
        notes.append("MPS/MLX is experimental: mocked probes only, not run here.")
    elif hw.accelerator == "rocm":
        engines = [
            _engine("kokoro", "cpu", ("kokoro-82m",)),
            _engine("phonon", "cpu", ("phonon-2.5",)),
        ]
        profile = "cpu-only"
        optional.append(
            _engine("qwen3_tts", "rocm", ("0.6b-base",), level=EXPERIMENTAL)
        )
        notes.append(
            "ROCm is experimental; the CPU engines are the verified default on AMD."
        )
    else:
        engines = [
            _engine("kokoro", "cpu", ("kokoro-82m",)),
            _engine("phonon", "cpu", ("phonon-2.5",)),
        ]
        profile = "cpu-only"
        optional.append(
            _engine(
                "qwen3_tts",
                "cpu",
                ("0.6b-base",),
                note="Qwen3-TTS on CPU is slow; use it only when its voice matters",
                level=pair_level("qwen3_tts", "cpu"),
            )
        )
        notes.append("No accelerator detected: the CPU engines are the defaults.")

    return SetupRecommendation(
        profile=profile,
        engines=tuple(engines),
        optional=tuple(optional),
        notes=tuple(notes),
        pairs=all_pairs(),
    )


def host_supports(hw: HardwareProfile, device: ComputeDevice) -> bool:
    if device == "cpu":
        return True
    if device == "cuda":
        return hw.accelerator == "cuda"
    if device == "rocm":
        return hw.accelerator == "rocm"
    return hw.accelerator == "mps"


def torch_build_supports(torch_info: dict[str, Any] | None, device: ComputeDevice) -> bool:
    """Whether an installed torch build can actually serve ``device``."""
    if device == "cpu":
        return True
    info = torch_info or {}
    if device in {"cuda", "rocm"}:
        if not info.get("cuda_available"):
            return False
        if device == "rocm":
            return bool(info.get("hip"))
        return info.get("cuda") is not None
    if device == "mps":
        return bool(info.get("mps"))
    return False


def describe_torch(torch_info: dict[str, Any] | None) -> str:
    if not torch_info:
        return "torch not probed"
    if torch_info.get("error"):
        return f"torch unavailable ({torch_info['error']})"
    bits = [str(torch_info.get("version", "torch"))]
    if torch_info.get("cuda"):
        bits.append(f"cuda {torch_info['cuda']}")
    if torch_info.get("hip"):
        bits.append(f"hip {torch_info['hip']}")
    if torch_info.get("mps"):
        bits.append("mps")
    return ", ".join(bits)


def _supports(manifest: EngineManifest, device: ComputeDevice) -> bool:
    return device in manifest.supported_devices


def _best_auto_device(
    engine_id: str,
    manifest: EngineManifest,
    hw: HardwareProfile,
    current: str,
) -> ComputeDevice | None:
    """The best available device for ``auto``, verified before experimental.

    A user who asked for a specific device is never overridden; this only runs
    when the manifest default or the host default was already implicit. Prefer
    ``verified`` over ``experimental`` so an Apple host does not silently choose
    the experimental MPS path when the verified CPU path exists.
    """
    rank = {"verified": 0, "experimental": 1, "unsupported": 2}
    candidates = [
        device
        for device in manifest.supported_devices
        if device != current and host_supports(hw, device)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda device: (
            rank.get(pair_level(engine_id, device), 3),
            0 if device == "cpu" else 1,
        )
    )
    return candidates[0]


def resolve_device(
    engine_id: str,
    manifest: EngineManifest,
    settings: Any,
    hw: HardwareProfile,
) -> ComputeDevice:
    """Resolve one engine's runtime device.

    Precedence: explicit per-engine override, then a non-``auto`` global
    preference, then the manifest's default, then the host. A host default that
    the engine does not support falls back to CPU only when no explicit override
    asked for it; otherwise the caller gets an actionable error.
    """
    explicit = settings.devices.get(engine_id)
    if explicit is not None:
        requested: str = explicit
    elif settings.device != "auto":
        requested = settings.device
    elif manifest.default_device is not None:
        requested = manifest.default_device
    else:
        requested = hw.accelerator

    if not _supports(manifest, requested):
        auto = explicit is None and settings.device == "auto"
        candidate = _best_auto_device(engine_id, manifest, hw, requested) if auto else None
        if candidate is None:
            supported = ", ".join(manifest.supported_devices) or "(none)"
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"engine '{engine_id}' does not support device '{requested}'",
                f"Supported devices: {supported}. Set AGENT_VOICE_DEVICE or "
                f"AGENT_VOICE_DEVICES={engine_id}=<device>.",
            )
        requested = candidate

    if not host_supports(hw, requested):  # type: ignore[arg-type]
        # `auto` follows the manifest default, but a host without that device
        # must land on the best available supported backend — verified before
        # experimental. On Apple, qwen3_tts therefore uses the verified CPU path
        # rather than silently opting into experimental MPS.
        if explicit is None and settings.device == "auto":
            candidate = _best_auto_device(engine_id, manifest, hw, requested)
            if candidate is not None:
                requested = candidate
        if not host_supports(hw, requested):  # type: ignore[arg-type]
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"device '{requested}' is not available on this host for '{engine_id}'",
                f"Detected accelerator: {hw.accelerator}. Set AGENT_VOICE_DEVICE=cpu, "
                "or pick an engine that supports this host.",
            )
    return requested  # type: ignore[return-value]


def validate_device_preferences(settings: Any, registry: Any) -> None:
    """Fail at startup on a typo, not on the first request.

    Unknown engine ids and devices the manifest does not declare are caller
    mistakes; the message names the supported list and the exact setting.
    """
    for engine_id, device in settings.devices.items():
        record = registry.get(engine_id)  # ENGINE_NOT_FOUND for a typo
        if device not in record.manifest.supported_devices:
            supported = ", ".join(record.manifest.supported_devices) or "(none)"
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"AGENT_VOICE_DEVICES sets {engine_id}={device}, but that engine "
                f"supports: {supported}",
                "Fix AGENT_VOICE_DEVICES, or remove the override and use auto.",
            )
