"""Engine discovery, manifest validation and hot-reload.

Two things worth understanding:

* **Declared backend identity.** ``engine.yaml`` states ``backend_name`` and
  ``backend_version``. That is what makes the synthesis cache key computable
  before any worker is spawned. The registry cross-checks the declaration
  against installed distribution metadata; a mismatch is surfaced rather than
  silently used.
* **Atomic mutation.** ``register_model`` and ``register_engine`` rewrite YAML on
  disk with temp-file + ``os.replace`` under a lock, then reload in place. No
  daemon restart, no half-written manifest.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from filelock import FileLock

from .schemas import (
    AgentVoiceError,
    EngineEntry,
    EngineManifest,
    ErrorCode,
    ModelEntry,
    ModelMode,
    ModelSpec,
    validate_alias,
    validate_slug,
)

MANIFEST_NAME = "engine.yaml"
WORKER_NAME = "worker.py"
VENV_DIR = ".venv"

# Keys we own; a caller cannot smuggle arbitrary structure into a manifest.
_ALLOWED_MANIFEST_KEYS = {
    "engine_id",
    "display_name",
    "backend_name",
    "backend_version",
    "protocol_version",
    "venv_python",
    "capabilities",
    "models",
}


@dataclass
class EngineRecord:
    engine_id: str
    directory: Path
    manifest: EngineManifest
    installed: bool
    why_unavailable: str | None = None
    models: list[ModelEntry] = field(default_factory=list)

    @property
    def venv_python(self) -> Path:
        """Concrete interpreter to spawn.

        ``venv_python: null`` means "run in the core daemon's interpreter" — used
        by the dependency-free mock engine.

        Deliberately NOT ``.resolve()``: uv creates ``.venv/bin/python`` as a
        symlink to the base interpreter, and following it would bypass the venv
        entirely (so ``import torch`` fails with "No module named 'torch'").
        Only the *directory* is made absolute.
        """
        if self.manifest.venv_python is None:
            return Path(sys.executable)
        candidate = Path(self.manifest.venv_python)
        if candidate.is_absolute():
            return candidate
        return (self.directory / candidate).absolute()

    def worker_command(self) -> list[str]:
        """Argv that starts this engine's worker.

        Transport-agnostic on purpose: the caller owns the environment (notably
        the IPC endpoint in ``AGENT_VOICE_IPC_*``) and decides whether to spawn.
        The registry itself never starts a process.
        """
        return [str(self.venv_python), str(self.directory / WORKER_NAME)]


def _hf_cache_path(hf_repo: str) -> Path:
    org, _, name = hf_repo.partition("/")
    return Path.home() / ".cache" / "huggingface" / "hub" / f"models--{org}--{name}"


def model_revision(hf_repo: str) -> str:
    """Best-effort local revision of ``hf_repo`` for the synthesis cache key.

    Re-pulling weights must change the key, otherwise a cached render from the old
    snapshot is served as if it were the new model's. Returning the commit hash
    from the HF cache's ``refs/main`` is the only pointer we have before (or
    without) a worker, and it is a plain file read — no network, no hub client.

    ``"local"`` marks a repo that is not an HF repo at all (``local/mock-tone``),
    and ``"unknown"`` means the repo may be remote but was never resolved on this
    machine. Both are honest: neither invents a version.
    """
    if "/" not in hf_repo or hf_repo.startswith("local/"):
        return "local"
    ref = _hf_cache_path(hf_repo) / "refs" / "main"
    try:
        revision = ref.read_text("utf-8").strip()
    except OSError:
        return "unknown"
    return revision or "unknown"


def hf_model_downloaded(hf_repo: str) -> bool:
    """True when real weight files exist in the local HF cache.

    Deliberately checks for snapshot content, not just ``refs/``: a resolved ref
    with no weights means the model is *not* usable, and reporting otherwise
    would send an agent down a path that fails at load time.
    """
    root = _hf_cache_path(hf_repo)
    snapshots = root / "snapshots"
    if not snapshots.is_dir():
        return False
    for revision in snapshots.iterdir():
        if not revision.is_dir():
            continue
        for entry in revision.iterdir():
            # .pth is Kokoro's 327 MB checkpoint; without it a fully working
            # install is reported as "not downloaded" and the OpenAI preset
            # fallback refuses to speak.
            if entry.suffix in {".safetensors", ".bin", ".pt", ".pth", ".gguf"}:
                try:
                    if entry.resolve().stat().st_size > 0:
                        return True
                except OSError:
                    continue
        # Some repos nest weights (e.g. speech_tokenizer/).
        for nested in revision.rglob("*.safetensors"):
            try:
                if nested.resolve().stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


def _dist_version(interpreter: Path, distribution: str) -> str | None:
    """Read an installed distribution version from a specific interpreter."""
    script = (
        "import importlib.metadata as m, json, sys\n"
        f"try:\n"
        f"    print(json.dumps(m.version({distribution!r})))\n"
        f"except Exception:\n"
        f"    print(json.dumps(None))\n"
    )
    try:
        result = subprocess.run(
            [str(interpreter), "-c", script],
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.decode("utf-8").strip() or "null")
    except json.JSONDecodeError:
        return None


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class EngineRegistry:
    def __init__(self, engines_dir: Path) -> None:
        # Absolute, because workers are spawned with cwd=<engine dir> and a
        # relative interpreter/worker path would resolve against that cwd.
        self.engines_dir = Path(engines_dir).resolve()
        self.engines_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, EngineRecord] = {}
        self._lock = FileLock(str(self.engines_dir / ".registry.lock"))
        self.reload()

    # -- discovery ------------------------------------------------------
    def reload(self) -> None:
        records: dict[str, EngineRecord] = {}
        for directory in sorted(self.engines_dir.iterdir()):
            if not directory.is_dir():
                continue
            manifest_path = directory / MANIFEST_NAME
            if not manifest_path.is_file():
                continue
            try:
                records[directory.name] = self._load_engine(directory)
            except (AgentVoiceError, yaml.YAMLError, ValueError):
                # A broken plugin must not take down discovery of healthy ones.
                continue
        self._records = records

    def _load_engine(self, directory: Path) -> EngineRecord:
        manifest_path = directory / MANIFEST_NAME
        raw = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
        if not isinstance(raw, dict):
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"{manifest_path} must contain a YAML mapping",
                "See engines/ADDING_AN_ENGINE.md for the schema.",
            )
        unknown = set(raw) - _ALLOWED_MANIFEST_KEYS
        if unknown:
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"{manifest_path} has unknown keys: {sorted(unknown)}",
                f"Allowed keys: {sorted(_ALLOWED_MANIFEST_KEYS)}",
            )

        manifest = EngineManifest(**raw)
        if manifest.engine_id != directory.name:
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"engine_id '{manifest.engine_id}' does not match directory '{directory.name}'",
                "Rename the directory or fix engine_id so they agree.",
            )

        record = EngineRecord(
            engine_id=manifest.engine_id,
            directory=directory,
            manifest=manifest,
            installed=False,
        )

        interpreter = record.venv_python
        worker_path = directory / WORKER_NAME
        if manifest.venv_python is not None and not interpreter.is_file():
            record.why_unavailable = (
                f"engine venv missing at {interpreter}; run `agent-voice engines install "
                f"{manifest.engine_id}`"
            )
        elif not worker_path.is_file():
            record.why_unavailable = f"worker.py missing in {directory}"
        else:
            record.installed = True
            declared = manifest.backend_version
            actual = self._declared_backend_version(record, interpreter)
            if actual is not None and actual != declared:
                record.why_unavailable = (
                    f"engine.yaml declares {manifest.backend_name}=={declared} but "
                    f"{actual} is installed; run install to reconcile"
                )

        record.models = [
            ModelEntry(
                engine_id=manifest.engine_id,
                alias=spec.alias,
                hf_repo=spec.hf_repo,
                quality_tier=spec.quality_tier,
                est_vram_gb=spec.est_vram_gb,
                is_default=spec.default,
                downloaded_locally=hf_model_downloaded(spec.hf_repo),
                loaded=False,
                mode=spec.mode,
                supports_instruct=spec.supports_instruct,
                preset_speakers=list(spec.preset_speakers),
            )
            for spec in manifest.models
        ]
        return record

    def _declared_backend_version(self, record: EngineRecord, interpreter: Path) -> str | None:
        """Best-effort read of the installed backend distribution version."""
        dist = record.manifest.backend_name
        if not dist:
            return None
        return _dist_version(interpreter, dist)

    # -- queries --------------------------------------------------------
    def ids(self) -> list[str]:
        return sorted(self._records)

    def get(self, engine_id: str) -> EngineRecord:
        record = self._records.get(engine_id)
        if record is None:
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_FOUND,
                f"no such engine: {engine_id}",
                f"Available engines: {', '.join(self.ids()) or '(none)'}. "
                "Call list_engines() for details.",
            )
        return record

    def require_installed(
        self, engine_id: str, *, allow_agent_mutation: bool = False
    ) -> EngineRecord:
        record = self.get(engine_id)
        if not record.installed:
            suggestion = f"Run `agent-voice engines install {engine_id}`"
            suggestion += " or call install_engine()." if allow_agent_mutation else "."
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_INSTALLED,
                f"engine '{engine_id}' is not installed: {record.why_unavailable}",
                suggestion,
            )
        return record

    def list_entries(self, *, loaded: dict[str, list[str]] | None = None) -> list[EngineEntry]:
        loaded = loaded or {}
        entries: list[EngineEntry] = []
        for engine_id in self.ids():
            record = self._records[engine_id]
            loaded_models = set(loaded.get(engine_id, []))
            entries.append(
                EngineEntry(
                    engine_id=record.engine_id,
                    display_name=record.manifest.display_name,
                    backend_name=record.manifest.backend_name,
                    backend_version=record.manifest.backend_version,
                    installed=record.installed,
                    venv_python=(
                        None
                        if record.manifest.venv_python is None
                        else str(record.venv_python)
                    ),
                    streaming=bool(record.manifest.capabilities.streaming),
                    pcm_native=bool(record.manifest.capabilities.pcm_native),
                    custom_voice=bool(record.manifest.capabilities.custom_voice),
                    voice_design=bool(record.manifest.capabilities.voice_design),
                    instruct_control=bool(record.manifest.capabilities.instruct_control),
                    models=[
                        model.model_copy(update={"loaded": model.alias in loaded_models})
                        for model in record.models
                    ],
                    why_unavailable=record.why_unavailable,
                )
            )
        return entries

    def list_models(self, engine_id: str | None = None) -> list[ModelEntry]:
        models: list[ModelEntry] = []
        for current in self.ids():
            if engine_id is not None and current != engine_id:
                continue
            models.extend(self._records[current].models)
        return models

    def default_model_alias(
        self, engine_id: str, quality: str = "high", mode: ModelMode | None = None
    ) -> str:
        """Pick the model for a quality tier, optionally restricted to one mode.

        Clone routing must pass ``mode="clone"``: with CustomVoice and VoiceDesign
        sharing the high tier, an unfiltered pick could load a model that cannot
        clone at all.
        """
        record = self.get(engine_id)
        candidates = [
            model for model in record.manifest.models if mode is None or model.mode == mode
        ]
        if not candidates:
            raise AgentVoiceError(
                ErrorCode.MODEL_NOT_FOUND,
                f"engine '{engine_id}' declares no {mode or 'usable'} model",
                "Fix engine.yaml or register a model with register_model().",
            )
        for model in candidates:
            if model.quality_tier == quality:
                return model.alias
        for model in candidates:
            if model.default:
                return model.alias
        return candidates[0].alias

    def model_spec(self, engine_id: str, alias: str) -> ModelSpec:
        record = self.get(engine_id)
        for model in record.manifest.models:
            if model.alias == alias:
                return model
        raise AgentVoiceError(
            ErrorCode.MODEL_NOT_FOUND,
            f"engine '{engine_id}' has no model '{alias}'",
            f"Known aliases: {[m.alias for m in record.manifest.models]}",
        )

    def resolve_engine_for_model(self, alias: str) -> str | None:
        for engine_id in self.ids():
            if any(m.alias == alias for m in self._records[engine_id].manifest.models):
                return engine_id
        return None

    # -- mutation -------------------------------------------------------
    def register_model(
        self,
        *,
        engine_id: str,
        model_alias: str,
        hf_repo: str,
        quality_tier: str = "high",
        est_vram_gb: float = 4.4,
        default: bool = False,
    ) -> EngineRecord:
        validate_slug(engine_id)
        validate_alias(model_alias)
        record = self.get(engine_id)
        manifest_path = record.directory / MANIFEST_NAME

        with self._lock:
            raw = yaml.safe_load(manifest_path.read_text("utf-8")) or {}
            models = list(raw.get("models") or [])
            for existing in models:
                if existing.get("alias") == model_alias:
                    raise AgentVoiceError(
                        ErrorCode.BAD_REQUEST,
                        f"model '{model_alias}' already exists on engine '{engine_id}'",
                        "Choose a different alias or update engine.yaml directly.",
                    )
            models.append(
                {
                    "alias": model_alias,
                    "hf_repo": hf_repo,
                    "quality_tier": quality_tier,
                    "est_vram_gb": est_vram_gb,
                    "default": default,
                }
            )
            raw["models"] = models
            EngineManifest(**raw)  # validate before touching disk

            _atomic_write_text(
                manifest_path,
                yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            )
            self.reload()

        return self.get(engine_id)

    def register_engine(
        self,
        *,
        engine_id: str,
        engine_yaml_content: str,
        pyproject_toml_content: str | None = None,
        worker_py_content: str | None = None,
        staging: bool = False,
    ) -> EngineRecord:
        """Scaffold a new plugin. Slug-validated, no path traversal, schema-checked.

        With ``staging=True`` the plugin is written to ``engines/.pending/<id>/``
        where discovery cannot see it, and an operator promotes it with
        :meth:`approve_engine`. That is the single implementation both the MCP
        server and the CLI use; there is deliberately no second copy.
        """
        validate_slug(engine_id)
        if re.search(r"[\\/]|\.\.", engine_id):
            raise AgentVoiceError(
                ErrorCode.INVALID_SLUG,
                f"invalid engine id: {engine_id!r}",
                "Use lowercase letters, digits and underscores only.",
            )

        try:
            raw = yaml.safe_load(engine_yaml_content) or {}
        except yaml.YAMLError as exc:
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"engine.yaml is not valid YAML: {exc}",
                "Fix the YAML and retry.",
            ) from exc
        if not isinstance(raw, dict):
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                "engine.yaml must be a YAML mapping",
                "See engines/ADDING_AN_ENGINE.md.",
            )
        raw.setdefault("engine_id", engine_id)
        raw["engine_id"] = engine_id
        unknown = set(raw) - _ALLOWED_MANIFEST_KEYS
        if unknown:
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"unknown manifest keys: {sorted(unknown)}",
                f"Allowed: {sorted(_ALLOWED_MANIFEST_KEYS)}",
            )
        EngineManifest(**raw)  # raises on schema violation

        directory = self.pending_dir / engine_id if staging else self.engines_dir / engine_id
        if directory.exists():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"engine directory already exists: {directory}",
                "Pick another id or delete the existing plugin first.",
            )

        with self._lock:
            directory.mkdir(parents=True)
            _atomic_write_text(
                directory / MANIFEST_NAME,
                yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
            )
            if pyproject_toml_content:
                _atomic_write_text(directory / "pyproject.toml", pyproject_toml_content)
            if worker_py_content:
                _atomic_write_text(directory / WORKER_NAME, worker_py_content)
            if not staging:
                self.reload()

        if staging:
            # Staged plugins are intentionally absent from the live registry, so
            # return a record describing the staged directory rather than
            # self.get(), which would raise.
            return self._load_engine(directory)
        return self.get(engine_id)

    @property
    def pending_dir(self) -> Path:
        return self.engines_dir / ".pending"

    def list_pending(self) -> list[str]:
        """Engine ids staged for operator approval."""
        if not self.pending_dir.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self.pending_dir.iterdir()
            if entry.is_dir() and (entry / MANIFEST_NAME).is_file()
        )

    def approve_engine(self, engine_id: str) -> EngineRecord:
        """Promote ``engines/.pending/<id>/`` to live, validating first.

        Refuses to overwrite an installed engine: a silent replacement could swap
        code an operator already trusted for code they have not read.
        """
        validate_slug(engine_id)
        staged = self.pending_dir / engine_id
        destination = self.engines_dir / engine_id

        if not staged.is_dir():
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_FOUND,
                f"no staged engine: {engine_id}",
                f"Staged plugins: {self.list_pending() or '(none)'}",
            )
        if destination.exists():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"engine '{engine_id}' is already installed",
                "Remove the installed plugin first if you really mean to replace it.",
            )

        # Validate the manifest before it becomes importable.
        raw = yaml.safe_load((staged / MANIFEST_NAME).read_text("utf-8")) or {}
        if not isinstance(raw, dict):
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"{MANIFEST_NAME} in the staged plugin is not a mapping",
                "Fix the manifest in engines/.pending and retry.",
            )
        raw.setdefault("engine_id", engine_id)
        EngineManifest(**raw)
        if not (staged / WORKER_NAME).is_file():
            raise AgentVoiceError(
                ErrorCode.MANIFEST_INVALID,
                f"staged plugin '{engine_id}' has no {WORKER_NAME}",
                "A plugin without a worker cannot run.",
            )

        with self._lock:
            import shutil

            shutil.move(str(staged), str(destination))
            self.reload()

        return self.get(engine_id)

    def unregister_engine(self, engine_id: str) -> None:
        """Remove a plugin directory. Only callers that already gated permission."""
        validate_slug(engine_id)
        directory = self.engines_dir / engine_id
        if not directory.is_dir():
            raise AgentVoiceError(
                ErrorCode.ENGINE_NOT_FOUND, f"no such engine: {engine_id}", ""
            )
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
        self.reload()
