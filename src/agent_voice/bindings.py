"""Per-agent bindings: convenience routing on a local single-user box.

``X-Agent-Id`` is **not** an authorization boundary. It lets several agents share
one daemon while each keeps its own default voice/engine. Real access control is
``AGENT_VOICE_API_KEY``.

Writes are atomic (temp file + ``os.replace`` under a file lock) so concurrent
agents can never corrupt ``bindings.json``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

from .schemas import AgentBinding, AgentVoiceError, ErrorCode, validate_slug


class BindingStore:
    """A tiny locked JSON document. No database, no migrations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(str(self.path) + ".lock")

    # -- io -------------------------------------------------------------
    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"bindings": {}}
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"bindings": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("bindings"), dict):
            return {"bindings": {}}
        return raw

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".bindings-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- public api -----------------------------------------------------
    def list(self) -> list[AgentBinding]:
        with self._lock:
            data = self._read_unlocked()
        return [
            AgentBinding(agent_id=agent, **fields)
            for agent, fields in data["bindings"].items()
        ]

    def get(self, agent_id: str) -> AgentBinding | None:
        with self._lock:
            data = self._read_unlocked()
        fields = data["bindings"].get(agent_id)
        if fields is None:
            return None
        return AgentBinding(agent_id=agent_id, **fields)

    def set(
        self,
        agent_id: str,
        *,
        voice_id: str | None = None,
        engine_id: str | None = None,
        model_alias: str | None = None,
    ) -> AgentBinding:
        if engine_id is not None:
            validate_slug(engine_id)
        with self._lock:
            data = self._read_unlocked()
            entry = data["bindings"].get(agent_id, {})
            if voice_id is not None:
                entry["voice_id"] = voice_id or None
            if engine_id is not None:
                entry["engine_id"] = engine_id or None
            if model_alias is not None:
                entry["model_alias"] = model_alias or None
            data["bindings"][agent_id] = entry
            self._write_unlocked(data)
        return AgentBinding(agent_id=agent_id, **entry)

    def delete(self, agent_id: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            existed = data["bindings"].pop(agent_id, None) is not None
            if existed:
                self._write_unlocked(data)
        return existed

    def resolve(
        self,
        *,
        agent_id: str | None,
        explicit_voice: str | None,
        explicit_engine: str | None,
        explicit_model: str | None,
        default_voice: str | None,
        default_engine: str,
    ) -> tuple[str | None, str, str | None]:
        """Precedence: explicit argument -> X-Agent-Id binding -> server default."""
        binding = self.get(agent_id) if agent_id else None

        voice = explicit_voice or (binding.voice_id if binding else None) or default_voice or None
        engine = explicit_engine or (binding.engine_id if binding else None) or default_engine
        model = explicit_model or (binding.model_alias if binding else None) or None
        return voice, engine, model


def ensure_voice_exists(voice_id: str, voices_dir: Path) -> Path:
    """Fail fast with a useful suggestion rather than a confusing model error."""
    candidate = Path(voices_dir) / voice_id
    if not candidate.is_dir():
        raise AgentVoiceError(
            ErrorCode.VOICE_NOT_FOUND,
            f"no such voice: {voice_id}",
            "Call list_voices() to see available voice ids, or clone_voice() first.",
        )
    return candidate
