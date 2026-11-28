#!/usr/bin/env python3
"""Fail if the committed package-data SPA is missing its file manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "agent_voice" / "web"
MANIFEST = TARGET / "web-build.json"


def main() -> int:
    if not (TARGET / "200.html").is_file():
        print("src/agent_voice/web/200.html is missing; run scripts/build_web.py")
        return 1
    if not MANIFEST.is_file():
        print("src/agent_voice/web/web-build.json is missing; run scripts/build_web.py")
        return 1
    expected = json.loads(MANIFEST.read_text("utf-8")).get("files", {})
    actual: dict[str, str] = {}
    for path in sorted(TARGET.rglob("*")):
        if not path.is_file() or path.name == "web-build.json":
            continue
        actual[str(path.relative_to(TARGET)).replace("\\", "/")] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            key for key in set(actual) & set(expected) if actual[key] != expected[key]
        )
        print("committed web assets are stale")
        if missing:
            print("missing:", missing[:10])
        if extra:
            print("extra:", extra[:10])
        if changed:
            print("changed:", changed[:10])
        print("run: uv run python scripts/build_web.py")
        return 1
    print(f"web assets current ({len(actual)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
