#!/usr/bin/env python3
"""Build the Svelte SPA and copy it into the Python package data.

Users never run Node/Bun. This is a maintainer/release command; CI runs it and
checks that the committed `src/agent_voice/web` is current.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
TARGET = ROOT / "src" / "agent_voice" / "web"
MANIFEST = "web-build.json"


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == MANIFEST:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(root)).replace("\\", "/")] = digest
    return result


def main() -> int:
    if shutil.which("bun") is None:
        print("bun is required to build the Studio web assets", file=sys.stderr)
        return 1
    subprocess.run(["bun", "run", "build"], cwd=FRONTEND, check=True)
    build = FRONTEND / "build"
    temp = Path(tempfile.mkdtemp(dir=str(ROOT), prefix=".web-build-"))
    try:
        destination = temp / "web"
        shutil.copytree(build, destination)
        (destination / MANIFEST).write_text(
            json.dumps({"files": _hash_tree(destination)}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if TARGET.exists():
            shutil.rmtree(TARGET)
        shutil.copytree(destination, TARGET)
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print(f"copied {len(_hash_tree(TARGET))} files into {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
