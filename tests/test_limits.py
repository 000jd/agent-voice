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


def test_websocket_rejects_oversized_text_without_closing(client):
    with client.websocket_connect("/v1/audio/ws") as websocket:
        websocket.send_json({"type": "speak", "text": "x" * 20_001})
        frame = websocket.receive_json()

        assert frame["type"] == "error"
        assert frame["error_code"] == "TEXT_TOO_LONG"
        assert "limit" in frame["message"]

        # The socket must stay usable for the next request.
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


# ---------------------------------------------------------------------------
# v0.4.0 Wave 1 (M8): a bad frame is an error frame, not a closed socket
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        "{not json at all",
        "",
        "[1, 2, 3]",
        '"just a string"',
        "42",
    ],
)
def test_websocket_survives_a_malformed_frame(client, frame):
    """v0.3.3 let ``ValueError``/``AttributeError`` escape the receive loop.

    The handler sat outside the loop, so one malformed frame closed the socket and
    the client had to reconnect to say anything else.
    """
    with client.websocket_connect("/v1/audio/ws") as websocket:
        websocket.send_text(frame)
        error = websocket.receive_json()

        assert error["type"] == "error"
        assert error["error_code"] == "BAD_REQUEST"
        assert error["suggestion"]

        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "speak", "text": "hi", "engine": "no-such-engine"},
        {
            "type": "speak",
            "text": "hi",
            "engine": "mock",
            "model": "mock-custom",
            "speaker": "Nobody",
        },
        {"type": "speak", "text": "hi", "voice": "v", "speaker": "Ryan"},
    ],
)
def test_websocket_recoverable_errors_keep_the_socket_usable(client, payload):
    """Unknown engine/speaker and contradictory timbres are caller mistakes.

    They must come back as an error frame on the same socket — the handler used to
    live outside the loop, so any of them terminated the connection.
    """
    with client.websocket_connect("/v1/audio/ws") as websocket:
        websocket.send_json(payload)
        error = websocket.receive_json()

        assert error["type"] == "error"
        assert error["error_code"] in {
            "ENGINE_NOT_FOUND",
            "BAD_REQUEST",
            "VOICE_NOT_FOUND",
            "VOICE_REQUIRED",
        }

        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


def test_websocket_still_answers_ping_after_a_malformed_frame_and_an_error(client):
    """Several bad frames in a row must not degrade into a closed socket."""
    with client.websocket_connect("/v1/audio/ws") as websocket:
        for bad in ("{oops", "[]"):
            websocket.send_text(bad)
            assert websocket.receive_json()["error_code"] == "BAD_REQUEST"

        websocket.send_json({"type": "speak", "text": "hi", "engine": "ghost"})
        assert websocket.receive_json()["type"] == "error"

        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}
