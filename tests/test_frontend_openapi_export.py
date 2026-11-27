from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_openapi.py"


def test_export_openapi_is_deterministic(tmp_path: Path) -> None:
    outputs = [tmp_path / "first.json", tmp_path / "second.json"]
    for output in outputs:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    assert outputs[0].read_bytes() == outputs[1].read_bytes()

    schema = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert "/health" in schema["paths"]
    assert "/v1/engines" in schema["paths"]
    # The WebSocket route is not part of the HTTP OpenAPI document.
    assert "/v1/audio/ws" not in schema["paths"]
