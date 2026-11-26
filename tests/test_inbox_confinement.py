"""S2 regression: agent clone paths default to the data inbox.

`/etc/passwd` and `/proc/self/environ` must be rejected by the confinement
check before any ffmpeg probe, while a clip in ``<DATA_DIR>/inbox/`` works. The
operator can opt into arbitrary paths with AGENT_VOICE_ALLOW_ANY_PATH=true.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, **overrides) -> Settings:
    engines = tmp_path / "engines"
    source = REPO_ROOT / "engines"
    for entry in source.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        target = engines / entry.name
        target.mkdir(parents=True)
        for filename in ("engine.yaml", "worker.py", "pyproject.toml"):
            candidate = entry / filename
            if candidate.is_file():
                (target / filename).write_bytes(candidate.read_bytes())

    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=engines,
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        AGENT_VOICE_ALLOW_ANY_PATH="false",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.mark.parametrize("probe", ["/etc/passwd", "/proc/self/environ"])
def test_rest_clone_rejects_outside_paths_by_default(tmp_path, probe):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "probe", "ref_audio_path": probe},
        )

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error_code"] == "PATH_OUTSIDE_BASE"
    assert "inbox" in body["suggestion"].lower()


def test_rest_clone_accepts_clip_in_inbox(tmp_path, ref_wav):
    settings = _settings(tmp_path)
    inbox = settings.inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    target = inbox / "clip.wav"
    shutil.copy(ref_wav, target)

    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "inboxvoice", "ref_audio_path": str(target)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "inboxvoice"


def test_rest_clone_allow_any_path_permits_outside_clip(tmp_path, ref_wav):
    settings = _settings(tmp_path, AGENT_VOICE_ALLOW_ANY_PATH="true")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        response = client.post(
            "/v1/voices",
            json={"name": "outsidevoice", "ref_audio_path": str(ref_wav)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "outsidevoice"
