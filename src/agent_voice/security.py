"""Path confinement, slug validation and process-safety helpers.

Two distinct concerns live here:

1. **`MCP_BASE_PATH` confinement.** When set, agent-supplied local paths may only
   be read from inside that directory, and output artifacts must be written
   *inside* it too — otherwise the returned ``file_path`` is unreadable from the
   agent's sandbox even though synthesis succeeded.
2. **Subprocess hygiene.** Every external command is invoked with an argv list
   and ``shell=False``. There is no code path in this project that builds a
   shell string.
"""

from __future__ import annotations

import os
from pathlib import Path

from .schemas import AgentVoiceError, ErrorCode, validate_slug

__all__ = [
    "MAX_REF_AUDIO_SECONDS",
    "MIN_REF_AUDIO_SECONDS",
    "ensure_within_base",
    "is_loopback_client",
    "resolve_artifact_path",
    "validate_slug",
]

MIN_REF_AUDIO_SECONDS = 1.0
"""Shorter than this and the model has nothing to clone from."""

MAX_REF_AUDIO_SECONDS = 30.0
"""Longer than this is wasted VRAM and prefill time."""

SWEET_SPOT_SECONDS = (3.0, 10.0)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def ensure_within_base(path: str | Path, base: str | Path) -> Path:
    """Return the resolved path if it is genuinely inside ``base``.

    Rejects traversal (``..``), absolute escapes, and symlinks — including
    symlinked *ancestors*, which a naive ``startswith`` check would miss.
    """
    base_real = _real(Path(base))
    candidate = Path(path)

    if not candidate.is_absolute():
        candidate = base_real / candidate
    if candidate.is_symlink():
        raise AgentVoiceError(
            ErrorCode.PATH_OUTSIDE_BASE,
            f"refusing to follow symlink: {candidate}",
            "Pass the real file inside MCP_BASE_PATH, not a link to it.",
        )

    resolved = _real(candidate)
    if resolved != base_real and base_real not in resolved.parents:
        raise AgentVoiceError(
            ErrorCode.PATH_OUTSIDE_BASE,
            f"path escapes MCP_BASE_PATH: {path}",
            f"Move the file inside {base_real} or unset MCP_BASE_PATH.",
        )
    return resolved


def resolve_artifact_path(
    artifact_id: str,
    suffix: str,
    *,
    artifacts_dir: Path,
    mcp_base_path: str = "",
) -> Path:
    """Decide where a generated artifact is written.

    When ``MCP_BASE_PATH`` is set the artifact is materialised under
    ``<MCP_BASE_PATH>/artifacts/`` so the path we hand back is actually readable
    inside the agent's confined sandbox.
    """
    suffix = suffix if suffix.startswith(".") else f".{suffix}"

    if mcp_base_path:
        base = _real(Path(mcp_base_path)) / "artifacts"
    else:
        base = _real(artifacts_dir)
    base.mkdir(parents=True, exist_ok=True)

    return base / f"{artifact_id}{suffix}"


def is_loopback_client(host: str | None) -> bool:
    """True for loopback addresses. Used to gate agent-driven engine mutation."""
    if not host:
        return False
    if host in {"localhost", "::1", "testclient"}:
        return True
    return host.startswith("127.")
