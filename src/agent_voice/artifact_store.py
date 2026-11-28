"""Retention for generated artifacts.

Artifacts are content-addressed files under ``data/artifacts`` (or
``<MCP_BASE_PATH>/artifacts``). History may reference them; an unreferenced file
older than the configured TTL is removed. A referenced file is never deleted,
even if it is old.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from pathlib import Path

ARTIFACT_NAME_RE = re.compile(r"^(?:dub_)?([0-9a-f]{16})\.")


def artifact_id(name: str) -> str | None:
    match = ARTIFACT_NAME_RE.match(name)
    return match.group(1) if match else None


def prune_artifacts(
    artifacts_dir: Path,
    *,
    referenced_ids: Iterable[str],
    ttl_seconds: float,
    now: float | None = None,
) -> int:
    root = Path(artifacts_dir)
    if not root.is_dir():
        return 0
    referenced = set(referenced_ids)
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    removed = 0
    for path in root.iterdir():
        if not path.is_file():
            continue
        ident = artifact_id(path.name)
        if ident is not None and ident in referenced:
            continue
        try:
            if path.stat().st_mtime <= cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
