#!/usr/bin/env python3
"""Write the daemon's OpenAPI document deterministically.

The frontend's TypeScript client is generated from this file, so the bytes must
be stable across machines and runs. The app is built with a temporary home,
data dir and engines dir, and with MCP mounting disabled: schema generation
must not start workers, read the developer's data, or import torch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_OUTPUT = ROOT / "frontend" / "openapi.json"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_voice.api.app import create_app  # noqa: E402
from agent_voice.config import Settings  # noqa: E402


def _setting_env_names() -> set[str]:
    """Every environment spelling the settings model reads."""
    names: set[str] = set()
    for field in Settings.model_fields.values():
        if field.alias:
            names.add(field.alias)
        choices = getattr(field.validation_alias, "choices", None)
        if choices:
            names.update(choice for choice in choices if isinstance(choice, str))
    return names


@contextmanager
def _isolated_env() -> Iterator[None]:
    """Hide the developer's environment from Settings for this run."""
    names = _setting_env_names()
    saved = {name: os.environ.pop(name) for name in names if name in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def build_schema() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="agent-voice-openapi-") as tmp:
        home = Path(tmp)
        settings = Settings(
            agent_home=home,
            data_dir=home / "data",
            engines_dir=home / "engines",
            default_engine="mock",
            default_asr_engine="mock_asr",
            host="127.0.0.1",
            api_key="",
            allow_any_path=False,
            mcp_base_path="",
            allowed_hosts=[],
            devices={},
        )
        return create_app(settings=settings, mount_mcp=False).openapi()


def render(schema: dict[str, Any]) -> bytes:
    return (json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"destination (default: {DEFAULT_OUTPUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare with the destination instead of writing it",
    )
    args = parser.parse_args(argv)

    with _isolated_env():
        schema = build_schema()
    payload = render(schema)
    output: Path = args.output

    if args.check:
        if not output.is_file():
            print(f"missing {output}", file=sys.stderr)
            return 1
        if output.read_bytes() != payload:
            print(
                f"{output} is stale; run `uv run python scripts/export_openapi.py`",
                file=sys.stderr,
            )
            return 1
        print(f"{output} is current")
        return 0

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.tmp")
    temp.write_bytes(payload)
    temp.replace(output)
    print(f"wrote {output} ({len(schema.get('paths', {}))} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
