"""Setup planning and application for ``agent-voice setup``.

Two layers, deliberately separated:

* :func:`plan_setup` is **pure**. It reads the host, the engine manifests and the
  existing ``.env``, and returns a :class:`SetupPlan` describing what would
  happen. It never writes, never downloads, never prompts, and never imports
  typer. That is what makes ``--dry-run`` structurally safe: a dry run is just a
  plan that is printed instead of applied.
* :func:`apply_setup` performs the plan with **injected** callables
  (``runner`` for ``uv``, ``downloader`` for weights), so a test drives the whole
  apply path without touching uv, the network or the real home.

:func:`plan_engine_install` is the single definition of "how one engine gets
installed for one device". The CLI's ``engines install``, the REST/MCP install job
and the setup wizard all call it, so there cannot be three drifting argv shapes.

The module stays torch-free and typer-free: torch lives in the engine venvs, and
``uv``/Real Work only happens inside the injected callables.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import yaml

from . import envfile
from . import hardware as hwlib
from .config import reset_settings_cache, settings_env_file
from .schemas import (
    AgentVoiceError,
    ComputeDevice,
    DevicePreference,
    EngineManifest,
    ErrorCode,
)

SetupProfile = Literal["auto", "cpu-only", "nvidia-gpu", "apple-silicon"]
"""``--profile``: ``auto`` reads the host, the rest pin the recommendation."""

MANIFEST_NAME = "engine.yaml"
VENV_DIR = ".venv"
VARIANT_DIR = "variants"

_UV_ENV_STRIP = (
    # Wave 0 experiment 3: none of these can select a device, and `--index` on
    # qwen3_tts silently downgrades torch. They may also be set in the operator's
    # shell for an unrelated project, so they never reach this `uv sync`.
    "UV_INDEX",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "UV_NO_INDEX",
    "UV_INDEX_STRATEGY",
    # Not a uv setting: it only makes uv warn "VIRTUAL_ENV ... does not match the
    # project environment path". UV_PROJECT_ENVIRONMENT is what actually decides.
    "VIRTUAL_ENV",
)


class SetupError(AgentVoiceError):
    """A setup-specific refusal, still carrying the shared error taxonomy."""

    def __init__(self, message: str, suggestion: str = "") -> None:
        super().__init__(ErrorCode.BAD_REQUEST, message, suggestion)


# ---------------------------------------------------------------------------
# Manifests (read-only: no registry, no torch probe, no mkdir)
# ---------------------------------------------------------------------------


def load_manifests(engines_dir: Path | None) -> dict[str, EngineManifest]:
    """Read every ``engines/<id>/engine.yaml`` without the registry's side effects.

    ``EngineRegistry`` creates the directory and probes torch in every installed
    venv. Neither is acceptable in a planner, and ``--dry-run`` must not leave a
    directory behind, so this reads the manifests directly and skips a broken one
    the same way discovery does.
    """
    manifests: dict[str, EngineManifest] = {}
    if engines_dir is None or not engines_dir.is_dir():
        return manifests
    for directory in sorted(engines_dir.iterdir()):
        manifest_path = directory / MANIFEST_NAME
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            raw = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
            manifests[directory.name] = EngineManifest(**raw)
        except Exception:  # noqa: BLE001 - a broken plugin must not stop a plan
            continue
    return manifests


# ---------------------------------------------------------------------------
# Engine install plans (shared by `engines install`, the REST job and setup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineInstallPlan:
    """Exactly how one engine's venv is synced for one device."""

    engine_id: str
    device: ComputeDevice
    level: hwlib.SupportLevel
    project_dir: Path | None = None
    venv_dir: Path | None = None
    argv: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    variant: str = "canonical"
    note: str = ""
    problem: str = ""
    """Non-empty when the pair cannot be installed as is; ``argv`` is then empty."""

    @property
    def installable(self) -> bool:
        return bool(self.argv) and not self.problem

    @property
    def dependency_free(self) -> bool:
        """True when the engine needs no venv at all (``mock``, ``mock_asr``)."""
        return not self.argv and not self.problem

    def env_delta(self) -> dict[str, str]:
        """The variables this install actually adds or overrides.

        Printed by ``--dry-run`` instead of the whole environment: the rest is
        the operator's shell, which may hold secrets.
        """
        delta = {}
        for key, value in self.env.items():
            if os.environ.get(key) != value:
                delta[key] = value
        return delta


def project_device(pyproject: Path) -> ComputeDevice | None:
    """The device a project's ``[tool.agent-voice] device`` declares, if any."""
    try:
        raw = pyproject.read_text("utf-8")
    except OSError:
        return None
    try:
        parsed = tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    table = parsed.get("tool", {}).get("agent-voice")
    if not isinstance(table, dict):
        return None
    device = table.get("device")
    if device in {"cpu", "cuda", "mps", "rocm"}:
        return device  # type: ignore[return-value]
    return None


_WHL_CHANNEL = re.compile(r"download\.pytorch\.org/whl/(?P<channel>[a-z0-9.]+)")
_VERSION_TAG = re.compile(r'version = "[\d.]+(?P<tag>\+(?:cpu|cu\d+|rocm[\d.]*))"')


def _channel_device(channel: str) -> ComputeDevice | None:
    channel = channel.lower()
    if channel.startswith("cpu"):
        return "cpu"
    if channel.startswith("cu"):
        return "cuda"
    if channel.startswith("rocm"):
        return "rocm"
    return None


def lock_device_hint(lockfile: Path) -> ComputeDevice | None:
    """Which device the committed lock was resolved for, when it says so.

    Pre-``[tool.agent-voice]`` engine pyprojects have no declaration, so the torch
    wheel index baked into ``uv.lock`` is the only honest evidence of which
    variant the canonical project is (Wave 0 experiment 1: the index is written
    into every torch entry). A lock with no torch pin — ``mock`` — returns None.
    """
    try:
        handle = lockfile.open("r", encoding="utf-8")
    except OSError:
        return None
    try:
        with handle:
            for line in handle:
                match = _WHL_CHANNEL.search(line)
                if match is not None:
                    device = _channel_device(match.group("channel"))
                    if device is not None:
                        return device
                match = _VERSION_TAG.search(line)
                if match is not None:
                    device = _channel_device(match.group("tag").lstrip("+"))
                    if device is not None:
                        return device
    except OSError:
        return None
    return None


def _uv_env(venv_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _UV_ENV_STRIP}
    # The one venv stays at the canonical path even for a variant project, which
    # is what keeps `engine.yaml:venv_python` valid across a device switch.
    env["UV_PROJECT_ENVIRONMENT"] = str(venv_dir)
    return env


def _variant_projects(directory: Path) -> list[str]:
    root = directory / VARIANT_DIR
    if not root.is_dir():
        return []
    return sorted(
        child.name for child in root.iterdir() if (child / "pyproject.toml").is_file()
    )


def plan_engine_install(
    *,
    engine_id: str,
    directory: Path,
    manifest: EngineManifest,
    device: DevicePreference = "auto",
    allow_experimental: bool = False,
    uv: str = "uv",
) -> EngineInstallPlan:
    """Resolve the project root, argv and environment for one engine + device.

    Refusals are :class:`AgentVoiceError` so the CLI, the REST job and the MCP tool
    print the same sentence. A dependency-free engine (no ``pyproject.toml``
    anywhere) returns a plan with an empty ``argv`` — the caller decides whether
    that is a no-op or an error.
    """
    requested: ComputeDevice = (
        device if device != "auto" else (manifest.default_device or "cpu")
    )

    supported = list(manifest.supported_devices)
    if requested not in supported:
        raise SetupError(
            f"engine '{engine_id}' does not support device '{requested}'",
            f"Supported devices: {', '.join(supported) or '(none)'}. "
            "Pick one of those with --device.",
        )

    level = hwlib.pair_level(engine_id, requested)
    if level == hwlib.UNSUPPORTED:
        raise SetupError(
            f"({engine_id}, {requested}) is unsupported",
            "That engine has no code path for this device; the package metadata "
            "does not claim it. See docs/phase0-devices-report.md.",
        )
    if level == hwlib.EXPERIMENTAL and not allow_experimental:
        verified = [
            name
            for name, device_level in hwlib.DEVICE_SUPPORT.get(engine_id, {}).items()
            if device_level == hwlib.VERIFIED
        ]
        raise SetupError(
            f"({engine_id}, {requested}) is experimental: wired and unit-tested, "
            "not run on the reference host",
            "Re-run with --experimental to install it anyway, or use a verified "
            f"device: {', '.join(verified) or '(none)'}.",
        )

    venv_dir = directory / VENV_DIR
    canonical = directory / "pyproject.toml"
    declared = project_device(canonical)
    canonical_device = (
        declared
        or lock_device_hint(directory / "uv.lock")
        or manifest.default_device
        or "cpu"
    )

    if canonical.is_file() and canonical_device == requested:
        project = canonical
        variant = "canonical"
    else:
        variant_project = directory / VARIANT_DIR / requested / "pyproject.toml"
        if variant_project.is_file():
            project = variant_project
            variant = f"{VARIANT_DIR}/{requested}"
        elif not canonical.is_file() and not (directory / VARIANT_DIR).is_dir():
            return EngineInstallPlan(
                engine_id=engine_id,
                device=requested,
                level=level,
                venv_dir=venv_dir,
                variant="",
                note="dependency-free engine: no pyproject.toml, nothing to install",
            )
        else:
            available = _variant_projects(directory)
            raise SetupError(
                f"engine '{engine_id}' has no '{requested}' variant "
                f"(canonical project is '{canonical_device}')",
                f"Available variants: {', '.join(available) or '(none)'}. "
                f"Add engines/{engine_id}/{VARIANT_DIR}/{requested}/pyproject.toml "
                "+ uv.lock to publish one.",
            )

    lockfile = project.parent / "uv.lock"
    if not lockfile.is_file():
        raise SetupError(
            f"engine '{engine_id}' variant '{variant}' has no uv.lock: "
            "`--locked` would have to create one",
            f"Generate it with `uv lock --project {project.parent}` and commit it; "
            "`engines install` never rewrites a lock.",
        )

    env = _uv_env(venv_dir)
    argv = (uv, "sync", "--project", str(project), "--locked")
    note = f"{variant} project ({canonical_device} canonical)"
    return EngineInstallPlan(
        engine_id=engine_id,
        device=requested,
        level=level,
        project_dir=project.parent,
        venv_dir=venv_dir,
        argv=argv,
        env=env,
        variant=variant,
        note=note,
    )


def variant_stamp(device: ComputeDevice, torch_description: str) -> str:
    """Body of the variant stamp: which device this venv currently holds."""
    return (
        f"device={device}\n"
        f"torch={torch_description}\n"
        "# Written by `agent-voice engines install`; safe to delete.\n"
    )


def write_variant_stamp(
    directory: Path, device: ComputeDevice, torch_description: str
) -> Path:
    """Record the installed variant inside the engine venv.

    Deliberately ``engines/<id>/.venv/.variant`` rather than beside the venv:
    ``engines/*/.venv/`` is already gitignored, so a real install leaves
    ``git status`` clean, and deleting the venv deletes the claim with it — a
    stamp that outlived its venv would report a device that is no longer installed.
    """
    path = directory / VENV_DIR / ".variant"
    envfile.atomic_write_text(
        path, variant_stamp(device, torch_description), mode=0o644
    )
    return path


# ---------------------------------------------------------------------------
# Setup plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineChoice:
    """One engine as the plan shows it: which device, which models, how proven."""

    engine_id: str
    device: ComputeDevice
    models: tuple[str, ...] = ()
    download_gb: float = 0.0
    level: hwlib.SupportLevel = hwlib.VERIFIED
    note: str = ""
    kind: str = "tts"
    preset: bool = False
    """True for an engine that speaks preset timbres and needs no reference clip."""

    @property
    def label(self) -> str:
        return f"{self.engine_id}@{self.device}"


@dataclass(frozen=True)
class SetupPlan:
    """Everything ``setup`` would do, fully resolved and side-effect free."""

    hardware: hwlib.HardwareProfile
    profile: str
    env: envfile.EnvDiff
    recommended: tuple[EngineChoice, ...] = ()
    optional: tuple[EngineChoice, ...] = ()
    selected: tuple[EngineChoice, ...] = ()
    installs: tuple[EngineInstallPlan, ...] = ()
    download_gb: float = 0.0
    """Approximate weight size for the selection.

    Summed from ``hardware.MODEL_DOWNLOAD_GB`` and deliberately *not* net of what
    is already in the HF cache: the download step skips a present repo, so this is
    the worst case shown before the prompt.
    """
    env_updates: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    refusal: str = ""
    device: DevicePreference = "auto"
    download: bool = False
    interactive: bool = False
    dry_run: bool = False
    engines_dir: Path | None = None
    settings: Any = field(default=None, repr=False, compare=False)

    @property
    def selected_ids(self) -> tuple[str, ...]:
        return tuple(choice.engine_id for choice in self.selected)

    @property
    def has_changes(self) -> bool:
        return bool(self.selected) or self.env.has_changes

    def install_for(self, engine_id: str) -> EngineInstallPlan | None:
        for install in self.installs:
            if install.engine_id == engine_id:
                return install
        return None


@dataclass(frozen=True)
class _DevicePolicy:
    """The two fields :func:`hardware.resolve_device` reads."""

    device: DevicePreference = "auto"
    devices: Mapping[str, ComputeDevice] = field(default_factory=dict)


def _attr(settings: Any, name: str, fallback: Any) -> Any:
    value = getattr(settings, name, None)
    return fallback if value is None else value


def _config_path(settings: Any, home: Path) -> Path:
    configured = getattr(settings, "config_path", None)
    if isinstance(configured, Path):
        return configured
    return settings_env_file(home)


def _split_engines(engines: str | Iterable[str] | None) -> tuple[str, ...] | None:
    if engines is None:
        return None
    if isinstance(engines, str):
        parts = [part.strip() for part in engines.split(",")]
    else:
        parts = [str(part).strip() for part in engines]
    wanted = tuple(dict.fromkeys(part for part in parts if part))
    return wanted or None


def _cpu_only(hardware: hwlib.HardwareProfile) -> hwlib.HardwareProfile:
    return replace(hardware, accelerator="cpu", gpus=(), unified_ram_gb=None)


def _recommendation(
    hardware: hwlib.HardwareProfile, profile: SetupProfile
) -> tuple[hwlib.SetupRecommendation, str, list[str]]:
    """The recommendation for ``--profile``, plus a refusal when the host cannot.

    A profile the host cannot serve is not silently downgraded: the operator asked
    for it, so the plan says why it is impossible and still shows the auto
    recommendation so the command remains useful.
    """
    warnings: list[str] = []
    if profile == "auto":
        return hwlib.recommend_profile(hardware), "", warnings
    if profile == "cpu-only":
        warnings.append(
            "profile cpu-only: the accelerator engines are left out on purpose"
        )
        return hwlib.recommend_profile(_cpu_only(hardware)), "", warnings
    if profile == "nvidia-gpu":
        if hardware.accelerator != "cuda":
            return (
                hwlib.recommend_profile(hardware),
                f"profile nvidia-gpu requested, but no NVIDIA GPU was detected "
                f"(accelerator: {hardware.accelerator})",
                warnings,
            )
        return hwlib.recommend_profile(hardware), "", warnings
    if hardware.accelerator != "mps":
        return (
            hwlib.recommend_profile(hardware),
            "profile apple-silicon requested, but this is not an Apple Silicon host "
            f"(accelerator: {hardware.accelerator})",
            warnings,
        )
    return hwlib.recommend_profile(hardware), "", warnings


def _preset_only(manifest: EngineManifest | None) -> bool:
    """Whether the engine can speak without a cloned reference clip."""
    if manifest is None:
        return False
    capabilities = manifest.capabilities
    return bool(capabilities.custom_voice) and not bool(capabilities.voice_clone)


def _choice(
    engine_id: str,
    device: ComputeDevice,
    models: Sequence[str],
    manifest: EngineManifest | None,
    note: str = "",
) -> EngineChoice:
    return EngineChoice(
        engine_id=engine_id,
        device=device,
        models=tuple(models),
        download_gb=round(
            sum(hwlib.MODEL_DOWNLOAD_GB.get(alias, 0.0) for alias in models), 2
        ),
        level=hwlib.pair_level(engine_id, device),
        note=note,
        kind=manifest.kind if manifest is not None else "tts",
        preset=_preset_only(manifest),
    )


def _from_recommendation(
    entry: hwlib.EngineRecommendation, manifest: EngineManifest | None
) -> EngineChoice:
    return EngineChoice(
        engine_id=entry.engine_id,
        device=entry.device,
        models=tuple(entry.models),
        download_gb=entry.download_gb,
        level=entry.level,
        note=entry.note,
        kind=manifest.kind if manifest is not None else "tts",
        preset=_preset_only(manifest),
    )


def _default_engine(selected: Sequence[EngineChoice]) -> str | None:
    """The TTS engine ``speak`` should use when the caller names none.

    A preset-voice engine wins: it speaks without a reference clip, so a fresh
    install can say something immediately. Otherwise the first TTS engine in
    recommendation order.
    """
    tts = [choice for choice in selected if choice.kind == "tts"]
    for choice in tts:
        if choice.preset:
            return choice.engine_id
    return tts[0].engine_id if tts else None


def plan_setup(
    settings: Any,
    hardware: hwlib.HardwareProfile,
    *,
    profile: SetupProfile = "auto",
    device: DevicePreference = "auto",
    engines: str | Iterable[str] | None = None,
    download: bool = False,
    interactive: bool = False,
    dry_run: bool = False,
) -> SetupPlan:
    """Resolve hardware, engines, devices, download size and the ``.env`` diff.

    Reads only: the host profile is passed in, manifests and the existing ``.env``
    are read, nothing is created. Defaults are **verified pairs only** — an
    experimental pair is offered in ``optional`` and never selected, because it was
    never run on the reference host.
    """
    warnings: list[str] = []
    home = Path(_attr(settings, "home", Path.cwd()))
    engines_dir = Path(_attr(settings, "engines_dir", home / "engines"))
    data_dir = Path(_attr(settings, "data_dir", home / "data"))
    config_path = _config_path(settings, home)
    configured_device: DevicePreference = _attr(settings, "device", "auto") or "auto"

    manifests = load_manifests(engines_dir)
    refusal = ""
    if not engines_dir.is_dir():
        refusal = (
            f"engines directory not found: {engines_dir} — set ENGINES_DIR or "
            "AGENT_VOICE_HOME before running setup"
        )
    elif not manifests:
        # An empty engines directory is not an install to configure: writing `.env`
        # for it would pin data/engines at a directory no engine can live in.
        refusal = (
            f"no engine manifests found in {engines_dir}: this does not look like an "
            "agent-voice home. Set AGENT_VOICE_HOME (or ENGINES_DIR) to the checkout "
            "and re-run setup"
        )

    recommendation, profile_refusal, profile_warnings = _recommendation(
        hardware, profile
    )
    warnings.extend(profile_warnings)
    refusal = refusal or profile_refusal

    recommended = tuple(
        _from_recommendation(entry, manifests.get(entry.engine_id))
        for entry in recommendation.engines
    )
    optional = tuple(
        _from_recommendation(entry, manifests.get(entry.engine_id))
        for entry in recommendation.optional
    )
    warnings.extend(recommendation.notes)

    # The global preference: an explicit --device wins, otherwise whatever the
    # operator already configured stays in force (`auto` if nothing is set).
    explicit_device = device != "auto"
    global_device: DevicePreference = device if explicit_device else configured_device
    policy = _DevicePolicy(device=global_device)

    wanted = _split_engines(engines)
    pool = list(recommended) + list(optional)
    ordered: list[EngineChoice] = []
    if wanted is None:
        ordered = [choice for choice in recommended if choice.level == hwlib.VERIFIED]
    else:
        seen: set[str] = set()
        for choice in pool:
            if choice.engine_id not in wanted or choice.engine_id in seen:
                continue
            seen.add(choice.engine_id)
            ordered.append(choice)
        for engine_id in wanted:
            if engine_id in seen:
                continue
            manifest = manifests.get(engine_id)
            if manifest is None:
                warnings.append(
                    f"engine '{engine_id}' is not discovered in {engines_dir}; skipped"
                )
                continue
            default_models = tuple(
                spec.alias for spec in manifest.models if spec.default
            ) or tuple(spec.alias for spec in manifest.models[:1])
            ordered.append(
                _choice(
                    engine_id,
                    manifest.default_device or "cpu",
                    default_models,
                    manifest,
                    note="requested explicitly; not in the recommended set",
                )
            )

    selected: list[EngineChoice] = []
    for choice in ordered:
        manifest = manifests.get(choice.engine_id)
        if manifest is None:
            # An empty engines dir is already a refusal; a single missing manifest
            # next to healthy ones is worth a line of its own.
            if manifests:
                warnings.append(
                    f"{choice.engine_id}: no {MANIFEST_NAME}; keeping the recommended "
                    f"device {choice.device}"
                )
            resolved = choice.device
        else:
            try:
                resolved = hwlib.resolve_device(
                    choice.engine_id, manifest, policy, hardware
                )
            except AgentVoiceError as exc:
                warnings.append(f"{choice.engine_id}: {exc.message} — skipped")
                continue
        level = hwlib.pair_level(choice.engine_id, resolved)
        if level == hwlib.UNSUPPORTED:
            warnings.append(
                f"{choice.engine_id}@{resolved} is unsupported by the package "
                "metadata; skipped"
            )
            continue
        if level == hwlib.EXPERIMENTAL:
            warnings.append(
                f"{choice.engine_id}@{resolved} is experimental (never run on the "
                "reference host); skipped — install it deliberately with "
                f"`agent-voice engines install {choice.engine_id} --device "
                f"{resolved} --experimental`"
            )
            continue
        selected.append(
            replace(
                choice,
                device=resolved,
                level=level,
                download_gb=round(
                    sum(
                        hwlib.MODEL_DOWNLOAD_GB.get(alias, 0.0)
                        for alias in choice.models
                    ),
                    2,
                ),
            )
        )

    if not selected and not refusal:
        refusal = (
            "no verified engine pair was selected"
            if wanted is None
            else "none of the requested engines can be installed as verified"
        )

    installs: list[EngineInstallPlan] = []
    for choice in selected:
        manifest = manifests.get(choice.engine_id)
        if manifest is None:
            installs.append(
                EngineInstallPlan(
                    engine_id=choice.engine_id,
                    device=choice.device,
                    level=choice.level,
                    venv_dir=engines_dir / choice.engine_id / VENV_DIR,
                    problem=f"no {MANIFEST_NAME} in {engines_dir / choice.engine_id}",
                )
            )
            continue
        try:
            installs.append(
                plan_engine_install(
                    engine_id=choice.engine_id,
                    directory=engines_dir / choice.engine_id,
                    manifest=manifest,
                    device=choice.device,
                )
            )
        except AgentVoiceError as exc:
            # The suggestion is the actionable half ("add variants/<device>/…");
            # dropping it left a setup plan that named a problem but not its fix.
            problem = (
                f"{exc.message}: {exc.suggestion}" if exc.suggestion else exc.message
            )
            installs.append(
                EngineInstallPlan(
                    engine_id=choice.engine_id,
                    device=choice.device,
                    level=choice.level,
                    venv_dir=engines_dir / choice.engine_id / VENV_DIR,
                    problem=problem,
                )
            )
            warnings.append(f"{choice.engine_id}: {problem}")

    # --- .env -----------------------------------------------------------------
    updates: dict[str, str] = {
        "AGENT_VOICE_HOME": str(home),
        "DATA_DIR": str(data_dir),
        "ENGINES_DIR": str(engines_dir),
    }
    if global_device != "auto":
        updates["AGENT_VOICE_DEVICE"] = global_device

    overrides: dict[str, str] = {}
    for choice in selected:
        manifest = manifests.get(choice.engine_id)
        declared = manifest.default_device if manifest is not None else None
        if choice.device != global_device and choice.device != declared:
            overrides[choice.engine_id] = choice.device
    if overrides:
        updates["AGENT_VOICE_DEVICES"] = ",".join(
            f"{engine_id}={choice_device}"
            for engine_id, choice_device in overrides.items()
        )

    default_engine = _default_engine(selected)
    if default_engine:
        updates["AGENT_VOICE_DEFAULT_ENGINE"] = default_engine
    asr_engine = next(
        (choice.engine_id for choice in selected if choice.kind == "asr"), None
    )
    if asr_engine:
        updates["AGENT_VOICE_DEFAULT_ASR_ENGINE"] = asr_engine

    existing = envfile.read_env(config_path)
    existing_devices = existing.get("AGENT_VOICE_DEVICES", "")
    if existing_devices and "AGENT_VOICE_DEVICES" not in updates:
        warnings.append(
            f"{config_path} already pins AGENT_VOICE_DEVICES={existing_devices}; "
            "setup leaves that line alone — remove it to let setup manage devices"
        )

    if not hardware.has_uv:
        warnings.append(
            "uv is not on PATH; installing engines needs it "
            "(https://docs.astral.sh/uv/getting-started/installation/)"
        )

    env_diff = envfile.plan_env_update(config_path, updates)
    return SetupPlan(
        hardware=hardware,
        profile=recommendation.profile,
        env=env_diff,
        recommended=recommended,
        optional=optional,
        selected=tuple(selected),
        installs=tuple(installs),
        download_gb=round(sum(choice.download_gb for choice in selected), 2),
        env_updates=tuple(updates.items()),
        warnings=tuple(warnings),
        refusal=refusal,
        device=global_device,
        download=download,
        interactive=interactive,
        dry_run=dry_run,
        engines_dir=engines_dir,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

Runner = Callable[..., Any]
"""``runner(argv, *, env, cwd) -> int | CompletedProcess``. Injected by callers."""

Downloader = Callable[..., Any]
"""``downloader(engine_id, alias) -> str | None``. Injected by callers."""


@dataclass(frozen=True)
class SetupStep:
    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class SetupResult:
    plan: SetupPlan
    steps: tuple[SetupStep, ...] = ()
    env_written: bool = False
    backup_path: Path | None = None
    doctor_exit: int | None = None
    mcp_config: Mapping[str, Any] | None = None
    refused: str = ""

    @property
    def ok(self) -> bool:
        if self.refused:
            return False
        if any(step.ok is False for step in self.steps):
            return False
        return self.doctor_exit in (None, 0)


def _returncode(result: Any) -> int:
    if isinstance(result, bool):
        return 0 if result else 1
    if isinstance(result, int):
        return result
    return int(getattr(result, "returncode", 1) or 0)


def _tail(text: str, limit: int = 200) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed[-limit:] if len(collapsed) > limit else collapsed


def default_downloader(engines_dir: Path | None) -> Downloader:
    """The real weight downloader, built lazily so planning stays offline."""

    def download(engine_id: str, alias: str) -> str:
        manifests = load_manifests(engines_dir)
        manifest = manifests.get(engine_id)
        spec = None
        if manifest is not None:
            spec = next(
                (model for model in manifest.models if model.alias == alias), None
            )
        if spec is None:
            return f"skipped {engine_id}/{alias}: the manifest declares no such model"
        from .engine_registry import weights_downloaded

        if weights_downloaded(spec, manifest):
            return f"already present: {spec.hf_repo}"
        from huggingface_hub import snapshot_download

        return str(snapshot_download(repo_id=spec.hf_repo, repo_type="model"))

    return download


def apply_setup(
    plan: SetupPlan,
    *,
    runner: Runner,
    downloader: Downloader | None = None,
    yes: bool,
    doctor: Callable[[], int] | None = None,
    mcp_config: Callable[[], Mapping[str, Any]] | None = None,
) -> SetupResult:
    """Execute a plan. Every side effect goes through an injected callable.

    Order: engine installs (``uv sync`` per selected engine, in the same argv
    shape ``engines install`` runs), optional weight downloads, the ``.env``
    write, ``ensure_dirs`` + settings-cache reset, ``doctor``, then the
    ``mcp-config`` payload.

    Three guarantees are structural, not conventional:

    * ``plan.dry_run`` returns before the first call — a dry run cannot install,
      download or write, because it never reaches that code.
    * ``yes=False`` returns before the first call too: this function applies, so
      without confirmation it refuses. The CLI asks the question.
    * Weights are only fetched when ``plan.download`` is true, which is only true
      when ``--download`` was passed or the interactive prompt was answered yes.
      ``--yes`` alone installs engines and writes ``.env``; it never downloads.
    """
    if plan.dry_run:
        return SetupResult(
            plan=plan,
            steps=(
                SetupStep(
                    "plan",
                    True,
                    "--dry-run: nothing installed, downloaded or written",
                ),
            ),
        )
    if plan.refusal:
        return SetupResult(plan=plan, refused=plan.refusal)
    if not yes:
        return SetupResult(
            plan=plan,
            refused="confirmation required before changing anything",
        )

    steps: list[SetupStep] = []

    for install in plan.installs:
        name = f"install {install.engine_id}"
        if install.problem:
            steps.append(SetupStep(name, False, install.problem))
            continue
        if not install.argv:
            steps.append(
                SetupStep(name, True, install.note or "no dependencies to install")
            )
            continue
        if install.project_dir is None:  # pragma: no cover - argv implies a project
            steps.append(SetupStep(name, False, "no project directory resolved"))
            continue
        try:
            code = _returncode(
                runner(install.argv, env=install.env, cwd=install.project_dir)
            )
        except OSError as exc:
            steps.append(SetupStep(name, False, f"uv did not run: {_tail(exc)}"))
            continue
        steps.append(
            SetupStep(
                name,
                code == 0,
                f"{install.note}; venv {install.venv_dir}"
                if code == 0
                else f"uv sync exited {code}",
            )
        )

    if plan.download:
        active = downloader or default_downloader(plan.engines_dir)
        for choice in plan.selected:
            for alias in choice.models:
                name = f"download {choice.engine_id}/{alias}"
                try:
                    detail = active(choice.engine_id, alias)
                except Exception as exc:  # noqa: BLE001 - reported as a failed step
                    steps.append(SetupStep(name, False, _tail(exc)))
                    continue
                steps.append(SetupStep(name, True, _tail(detail or "downloaded")))
    elif plan.download_gb:
        steps.append(
            SetupStep(
                "download",
                True,
                f"skipped {plan.download_gb:.2f} GB of weights (no --download)",
            )
        )

    write = envfile.write_env(plan.env.path, dict(plan.env_updates))
    steps.append(
        SetupStep(
            "write .env",
            True,
            f"{write.path}"
            + (f" (backup {write.backup})" if write.backup else "")
            + ("" if write.written else " (unchanged)"),
        )
    )

    try:
        if plan.settings is None:
            raise OSError("no settings attached to this plan")
        plan.settings.ensure_dirs()
        steps.append(
            SetupStep("data dirs", True, str(_attr(plan.settings, "data_dir", "")))
        )
    except OSError as exc:
        steps.append(SetupStep("data dirs", False, _tail(exc)))
    reset_settings_cache()

    doctor_exit: int | None = None
    if doctor is not None:
        doctor_exit = int(doctor())
        steps.append(
            SetupStep(
                "doctor",
                doctor_exit == 0,
                "no failures"
                if doctor_exit == 0
                else f"{doctor_exit} failing check(s); see the rows above",
            )
        )

    payload = mcp_config() if mcp_config is not None else None
    return SetupResult(
        plan=plan,
        steps=tuple(steps),
        env_written=write.written,
        backup_path=write.backup,
        doctor_exit=doctor_exit,
        mcp_config=payload,
    )
