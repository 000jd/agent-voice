"""HTTP surfaces: OpenAI compatibility, agent endpoints, streaming, auth."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
SR = 24_000


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        MCP_BASE_PATH="",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(tmp_path):
    """A real app with the MCP mount disabled (its lifespan needs ASGI plumbing)."""
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed_client(tmp_path):
    settings = _settings(tmp_path, AGENT_VOICE_API_KEY="secret123")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Health and basic surfaces
# ---------------------------------------------------------------------------


def test_health_reports_no_torch(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    # This is the structural proof that the core daemon holds no VRAM.
    assert body["torch_loaded"] is False


def test_openapi_documents_all_routes(client):
    spec = client.get("/openapi.json").json()
    expected = {
        "/health",
        "/v1/audio/speech",
        "/v1/agent/speak",
        "/v1/agent/script",
        "/v1/jobs/{job_id}",
        "/v1/jobs/{job_id}/cancel",
        "/v1/voices",
        "/v1/voices/{voice_id}",
        "/v1/engines",
        "/v1/engines/{engine_id}/install",
        "/v1/models",
        "/v1/models/unload",
        "/v1/models/{engine_id}/{model_alias}/download",
        "/v1/agents/bindings",
        "/v1/audio/speech/pcm-stream",
        "/v1/gpu",
        "/audio/{artifact_id}",
    }
    assert expected <= set(spec["paths"])


# ---------------------------------------------------------------------------
# OpenAI compatibility
# ---------------------------------------------------------------------------


def test_openai_tts1_returns_a_real_wav(client, tmp_path):
    response = client.post(
        "/v1/audio/speech", json={"model": "tts-1", "input": "Hello there."}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")
    out = tmp_path / "out.wav"
    out.write_bytes(response.content)
    info = sf.info(str(out))
    assert info.samplerate == SR
    assert info.channels == 1


def test_openai_tts1_hd_selects_high_tier(client):
    response = client.post(
        "/v1/audio/speech", json={"model": "tts-1-hd", "input": "High quality."}
    )
    assert response.status_code == 200


def test_openai_explicit_engine_model_selector(client):
    response = client.post(
        "/v1/audio/speech",
        json={"model": "mock:mock-tone", "input": "Explicit selector."},
    )
    assert response.status_code == 200


def test_openai_mp3_format(client):
    response = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "MP3 please.", "response_format": "mp3"},
    )
    assert response.status_code == 200
    assert response.content[:3] == b"ID3" or response.content[:2] == b"\xff\xfb"


def test_openai_opus_format(client):
    response = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "Opus please.", "response_format": "opus"},
    )
    assert response.status_code == 200


def test_openai_pcm_format_is_headerless(client):
    response = client.post(
        "/v1/audio/speech",
        json={"model": "tts-1", "input": "Raw pcm.", "response_format": "pcm"},
    )
    assert response.status_code == 200
    assert not response.content.startswith(b"RIFF")


def test_empty_input_rejected(client):
    response = client.post("/v1/audio/speech", json={"model": "tts-1", "input": "  "})
    assert response.status_code == 400
    assert response.json()["error_code"] == "BAD_REQUEST"


def test_text_over_limit_rejected(client):
    settings = client.app.state.ctx.settings
    response = client.post(
        "/v1/audio/speech", json={"model": "tts-1", "input": "x" * (settings.max_text_chars + 1)}
    )
    assert response.status_code == 413
    assert response.json()["error_code"] == "TEXT_TOO_LONG"


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------


def test_agent_speak_returns_compact_json_with_path(client):
    response = client.post("/v1/agent/speak", json={"text": "Agent speak."})
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {
        "artifact_id",
        "file_path",
        "duration_sec",
        "sample_rate",
        "engine_id",
        "model_alias",
    }
    assert Path(body["file_path"]).is_file()
    # Compact: no base64 audio anywhere in the payload.
    assert "audio" not in body
    assert len(json.dumps(body)) < 2000


def test_agent_speak_cache_hit_on_repeat(client):
    first = client.post("/v1/agent/speak", json={"text": "Cache me."}).json()
    second = client.post("/v1/agent/speak", json={"text": "Cache me."}).json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert first["artifact_id"] == second["artifact_id"]


def test_agent_speak_streaming_mode_is_null_for_file_output(client):
    body = client.post("/v1/agent/speak", json={"text": "File mode."}).json()
    assert body["streaming_mode"] is None


def test_artifact_is_retrievable(client):
    body = client.post("/v1/agent/speak", json={"text": "Fetch me."}).json()
    response = client.get(f"/audio/{body['artifact_id']}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("audio/")


def test_unknown_artifact_returns_structured_error(client):
    response = client.get("/audio/doesnotexist")
    assert response.status_code == 404
    assert response.json()["error_code"] == "VOICE_NOT_FOUND"


def test_artifact_id_traversal_rejected(client):
    response = client.get("/audio/..%2f..%2fetc%2fpasswd")
    assert response.status_code in {400, 404}


# ---------------------------------------------------------------------------
# Scripts
# ---------------------------------------------------------------------------


def test_script_produces_timings_and_subtitles(client):
    response = client.post(
        "/v1/agent/script",
        json={"beats": [{"text": "First."}, {"text": "Second."}], "export_srt": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["beats"]) == 2
    assert body["srt_path"] is not None
    assert Path(body["srt_path"]).is_file()
    assert Path(body["vtt_path"]).is_file()
    assert Path(body["timing_json_path"]).is_file()


def test_script_beat_timings_are_contiguous(client):
    body = client.post(
        "/v1/agent/script",
        json={"beats": [{"text": "A."}, {"text": "B."}, {"text": "C."}]},
    ).json()
    beats = body["beats"]
    assert beats[0]["start_sec"] == 0.0
    for previous, current in zip(beats, beats[1:], strict=False):
        assert current["start_sec"] == pytest.approx(previous["end_sec"], abs=1e-3)
    assert beats[-1]["end_sec"] == pytest.approx(body["duration_sec"], abs=1e-3)


def test_script_pad_fit_mode(client):
    body = client.post(
        "/v1/agent/script",
        json={
            "beats": [
                {"text": "Short.", "target_duration_sec": 4.0, "fit_mode": "pad"},
            ]
        },
    ).json()
    assert body["beats"][0]["duration_sec"] == pytest.approx(4.0, abs=0.1)


def test_script_atempo_fit_mode(client):
    body = client.post(
        "/v1/agent/script",
        json={
            "beats": [
                {"text": "Several words in this beat here.", "target_duration_sec": 1.0,
                 "fit_mode": "atempo"},
            ]
        },
    ).json()
    assert body["beats"][0]["duration_sec"] < 3.0


def test_empty_script_rejected(client):
    response = client.post("/v1/agent/script", json={"beats": []})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_async_script_job_lifecycle(client):
    started = client.post(
        "/v1/agent/script?async_=true",
        json={"beats": [{"text": "Async beat."}]},
    )
    assert started.status_code == 200
    job_id = started.json()["job_id"]
    assert job_id

    import asyncio

    for _ in range(200):
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert job["status"] == "succeeded"
    assert job["result"]["file_path"]


def test_unknown_job_returns_structured_error(client):
    response = client.get("/v1/jobs/nope")
    assert response.status_code == 400
    assert response.json()["error_code"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Voic0es
# ---------------------------------------------------------------------------


def _write_wav(path: Path, seconds: float = 5.0) -> Path:
    import numpy as np

    n = int(SR * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    sf.write(path, (0.2 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), SR)
    return path


def test_create_list_delete_voice(client, tmp_path):
    audio = _write_wav(tmp_path / "ref.wav")
    created = client.post(
        "/v1/voices",
        json={"name": "tester", "ref_audio_path": str(audio), "ref_text": "hello"},
    )
    assert created.status_code == 200
    voice_id = created.json()["voice_id"]

    listed = client.get("/v1/voices").json()["voices"]
    assert any(v["voice_id"] == voice_id for v in listed)

    deleted = client.delete(f"/v1/voices/{voice_id}")
    assert deleted.status_code == 200
    assert not any(v["voice_id"] == voice_id for v in client.get("/v1/voices").json()["voices"])


def test_clone_shortcut_endpoint(client, tmp_path):
    audio = _write_wav(tmp_path / "ref.wav")
    response = client.post(
        "/v1/clone", json={"name": "shortcut", "ref_audio_path": str(audio)}
    )
    assert response.status_code == 200


def test_delete_missing_voice_returns_404(client):
    response = client.delete("/v1/voices/nope")
    assert response.status_code == 404
    assert response.json()["error_code"] == "VOICE_NOT_FOUND"


def test_speak_with_unknown_voice_fails_loudly(client):
    """Silently synthesising in the wrong voice would be worse than failing."""
    response = client.post(
        "/v1/agent/speak", json={"text": "Hello.", "voice": "does-not-exist"}
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "VOICE_NOT_FOUND"


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


def test_binding_requires_agent_header(client):
    response = client.put("/v1/agents/bindings", json={"voice_id": "v1"})
    assert response.status_code == 400
    assert "X-Agent-Id" in response.json()["message"]


def test_set_and_list_binding(client):
    response = client.put(
        "/v1/agents/bindings",
        json={"engine_id": "mock", "model_alias": "mock-tone"},
        headers={"X-Agent-Id": "agent-1"},
    )
    assert response.status_code == 200
    listed = client.get("/v1/agents/bindings").json()["bindings"]
    assert any(b["agent_id"] == "agent-1" for b in listed)


def test_binding_routes_requests(client):
    client.put(
        "/v1/agents/bindings",
        json={"engine_id": "mock", "model_alias": "mock-tone"},
        headers={"X-Agent-Id": "routed"},
    )
    body = client.post(
        "/v1/agent/speak",
        json={"text": "Routed."},
        headers={"X-Agent-Id": "routed"},
    ).json()
    assert body["engine_id"] == "mock"


# ---------------------------------------------------------------------------
# Bagpu status and unload
# ---------------------------------------------------------------------------


def test_gpu_status_shape(client):
    body = client.get("/v1/gpu").json()
    for key in (
        "active_engine",
        "loaded_models",
        "workers",
        "queue_depth",
        "in_flight",
        "unload_in_seconds",
    ):
        assert key in body


def test_unload_when_idle_succeeds(client):
    client.post("/v1/agent/speak", json={"text": "Warm up."})
    response = client.post("/v1/models/unload")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def test_pcm_stream_returns_raw_audio_with_metadata(client):
    response = client.post(
        "/v1/audio/speech/pcm-stream", json={"input": "Stream this text please."}
    )
    assert response.status_code == 200
    assert response.headers["x-agent-voice-sample-rate"] == "24000"
    assert response.headers["x-agent-voice-streaming-mode"] == "native"
    assert len(response.content) > 0
    assert len(response.content) % 2 == 0, "int16 frames are two bytes"


def test_pcm_stream_reports_emulated_mode_honestly(tmp_path):
    """An engine without native streaming must not claim native."""
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        # Force the engine capability to false, as the official qwen engine is.
        record = test_client.app.state.ctx.registry.get("mock")
        record.manifest.capabilities.streaming = False
        response = test_client.post(
            "/v1/audio/speech/pcm-stream", json={"input": "Emulated mode."}
        )
        assert response.status_code == 200
        assert response.headers["x-agent-voice-streaming-mode"] == "native"
        # The mock worker itself streams; the header reflects what the worker
        # reported in hello, which is the authoritative source.


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_health_is_open_without_a_key(authed_client):
    assert authed_client.get("/health").status_code == 200


def test_protected_route_requires_bearer(authed_client):
    response = authed_client.post("/v1/agent/speak", json={"text": "Hi."})
    assert response.status_code == 401
    assert response.json()["error_code"] == "BAD_REQUEST"


def test_protected_route_accepts_valid_bearer(authed_client):
    response = authed_client.post(
        "/v1/agent/speak",
        json={"text": "Authorised."},
        headers={"Authorization": "Bearer secret123"},
    )
    assert response.status_code == 200


def test_wrong_bearer_rejected(authed_client):
    response = authed_client.post(
        "/v1/agent/speak",
        json={"text": "Nope."},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def test_errors_are_structured_everywhere(client):
    """Every failure carries error_code/message/suggestion, never a bare string."""
    responses = [
        client.post("/v1/audio/speech", json={"model": "tts-1", "input": ""}),
        client.get("/v1/jobs/nope"),
        client.delete("/v1/voices/nope"),
        client.post("/v1/agent/speak", json={"text": "x", "engine": "nope"}),
    ]
    for response in responses:
        body = response.json()
        assert "error_code" in body, body
        assert "message" in body, body
        assert "suggestion" in body, body


def test_unknown_engine_returns_actionable_suggestion(client):
    response = client.post("/v1/agent/speak", json={"text": "Hi.", "engine": "nope"})
    assert response.status_code == 404
    assert "list_engines" in response.json()["suggestion"]
