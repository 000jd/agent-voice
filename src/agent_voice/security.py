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
    "INBOX_REF_AUDIO_SUGGESTION",
    "MAX_REF_AUDIO_SECONDS",
    "MIN_REF_AUDIO_SECONDS",
    "ensure_within_base",
    "is_loopback_client",
    "resolve_artifact_path",
    "resolve_ref_audio_path",
    "validate_slug",
]

MIN_REF_AUDIO_SECONDS = 1.0
"""Shorter than this and the model has nothing to clone from."""

MAX_REF_AUDIO_SECONDS = 30.0
"""Longer than this is wasted VRAM and prefill time."""

SWEET_SPOT_SECONDS = (3.0, 10.0)


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def ensure_within_base(
    path: str | Path, base: str | Path, *, suggestion: str = ""
) -> Path:
    """Return the resolved path if it is genuinely inside ``base``.

    Rejects traversal (``..``), absolute escapes, and symlinks — including
    symlinked *ancestors*, which a naive ``startswith`` check would miss.

    ``suggestion`` lets a caller name the actual destination (the inbox, a
    sandbox root) in the ``PATH_OUTSIDE_BASE`` fix; the default keeps the
    historical MCP_BASE_PATH wording.
    """
    base_real = _real(Path(base))
    candidate = Path(path)

    if not candidate.is_absolute():
        candidate = base_real / candidate
    if candidate.is_symlink():
        raise AgentVoiceError(
            ErrorCode.PATH_OUTSIDE_BASE,
            f"refusing to follow symlink: {candidate}",
            suggestion
            or "Pass the real file inside MCP_BASE_PATH, not a link to it.",
        )

    resolved = _real(candidate)
    if resolved != base_real and base_real not in resolved.parents:
        raise AgentVoiceError(
            ErrorCode.PATH_OUTSIDE_BASE,
            f"path escapes MCP_BASE_PATH: {path}",
            suggestion or f"Move the file inside {base_real} or unset MCP_BASE_PATH.",
        )
    return resolved


INBOX_REF_AUDIO_SUGGESTION = (
    "Put the reference clip in {inbox} (the inbox), or set "
    "AGENT_VOICE_ALLOW_ANY_PATH=true to allow any existing local path."
)
"""The one wording REST and MCP both give when the inbox is the only allowed root."""


def resolve_ref_audio_path(
    raw_path: str,
    *,
    inbox_dir: str | Path,
    mcp_base_path: str | Path = "",
    allow_any_path: bool = False,
) -> Path:
    """Resolve a caller-supplied ``ref_audio_path`` under the one shared policy.

    Three branches, in priority order — the same on REST and over MCP, because an
    agent that can reach both must not be able to smuggle a path through the
    looser surface:

    1. ``MCP_BASE_PATH`` set: the clip must live inside it (relative paths are
       resolved against it, escapes and symlinks are refused).
    2. Otherwise, ``allow_any_path`` false (the default): the clip must live in
       the data inbox, and the refusal names that inbox.
    3. Otherwise: any *absolute* existing local file. A relative path stays
       ambiguous — the daemon's cwd is not the caller's — so it is a
       ``BAD_REQUEST`` rather than a guess that could clone the wrong file.

    The returned path is checked to be a regular file before any ffmpeg probe.
    """
    if mcp_base_path:
        candidate = ensure_within_base(raw_path, mcp_base_path)
    elif not allow_any_path:
        candidate = ensure_within_base(
            raw_path,
            inbox_dir,
            suggestion=INBOX_REF_AUDIO_SUGGESTION.format(inbox=inbox_dir),
        )
    else:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise AgentVoiceError(
                ErrorCode.BAD_REQUEST,
                f"relative ref_audio_path is ambiguous: {raw_path}",
                "Pass an absolute path, or set MCP_BASE_PATH and use a relative one.",
            )

    if not candidate.is_file():
        raise AgentVoiceError(
            ErrorCode.BAD_REQUEST,
            f"ref_audio_path not found: {raw_path}",
            "Check the path exists and is readable by the daemon.",
        )
    return candidate


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
