#!/usr/bin/env python3
"""Build the Svelte SPA and copy it into the Python package data.

Users never run Node/Bun. This is a maintainer/release command; CI runs it and
checks that the committed `src/agent_voice/web` is current.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
TARGET = ROOT / "src" / "agent_voice" / "web"
MANIFEST = "web-build.json"
CSP_FILE = "csp-sha256.json"
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)


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
        html = (destination / "200.html").read_text("utf-8")
        hashes = [
            "sha256-" + base64.b64encode(hashlib.sha256(match.group(1).encode()).digest()).decode()
            for match in INLINE_SCRIPT.finditer(html)
        ]
        (destination / CSP_FILE).write_text(
            json.dumps({"script_hashes": hashes}, indent=2) + "\n", encoding="utf-8"
        )
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
