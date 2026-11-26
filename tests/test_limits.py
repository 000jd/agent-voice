"""v0.3.1 input caps: oversized requests fail before any GPU work."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
HUGE = "x" * 100_000


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


def test_oversized_instruct_is_rejected_fast(client):
    response = client.post("/v1/agent/speak", json={"text": "hi", "instruct": HUGE})

    assert response.status_code == 413
    body = response.json()
    assert body["error_code"] == "TEXT_TOO_LONG"
    assert "instruct" in body["message"]


def test_oversized_design_sample_text_is_rejected_fast(client):
    response = client.post(
        "/v1/voices/design",
        json={"name": "too-long", "instruct": "warm", "sample_text": HUGE},
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "TEXT_TOO_LONG"


def test_oversized_ref_text_is_rejected_fast(client, ref_wav):
    response = client.post(
        "/v1/voices",
        json={
            "name": "too-long",
            "ref_audio_path": str(ref_wav),
            "ref_text": HUGE,
        },
    )

    assert response.status_code == 413
    assert response.json()["error_code"] == "TEXT_TOO_LONG"


def test_too_many_script_beats_are_rejected_before_work(client):
    beats = [{"text": f"beat {i}"} for i in range(201)]
    response = client.post("/v1/agent/script", json={"beats": beats})

    assert response.status_code == 400
    body = response.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert "201" in body["message"]


def test_instruct_cap_is_configurable(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path, MAX_INSTRUCT_CHARS=10), mount_mcp=False)
    with TestClient(app) as client:
        rejected = client.post(
            "/v1/agent/speak", json={"text": "hi", "instruct": "x" * 11}
        )
        allowed = client.post(
            "/v1/agent/speak", json={"text": "hi", "instruct": "x" * 10}
        )

    assert rejected.status_code == 413
    assert allowed.status_code == 200
