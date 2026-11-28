"""Part B hardening: IDs, ingest gate, errors, docs auth, TTL and replay."""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.artifact_store import prune_artifacts
from agent_voice.config import Settings
from agent_voice.event_bus import EventBus
from agent_voice.ingest import configure_ingest, ingest_slot
from agent_voice.voice_drafts import VoiceDraftStore

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB = REPO_ROOT / "src" / "agent_voice" / "web"
SR = 24_000


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        AGENT_VOICE_HOME=tmp_path,
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        AGENT_VOICE_DEFAULT_ASR_ENGINE="mock_asr",
        AGENT_VOICE_ALLOWED_HOSTS="testserver",
        AGENT_VOICE_ALLOW_ANY_PATH="true",
    )
    base.update(overrides)
    return Settings(**base)


def _wav() -> bytes:
    t = np.linspace(0, 3, SR * 3, endpoint=False)
    buffer = io.BytesIO()
    sf.write(
        buffer,
        (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32),
        SR,
        format="WAV",
        subtype="PCM_16",
    )
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path: Path):
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("length", [300, 5000])
def test_long_voice_ids_are_400_without_path_leak(client, length):
    value = "x" * length
    for method, path in (
        ("get", f"/v1/voices/{value}"),
        ("delete", f"/v1/voices/{value}"),
        ("get", f"/v1/voices/{value}/export"),
        ("get", f"/v1/jobs/{value}"),
        ("post", f"/v1/jobs/{value}/cancel"),
    ):
        response = getattr(client, method)(path)
        assert response.status_code in {400, 404}, (path, response.status_code)
        body = response.text
        assert "/tmp/" not in body
        assert "voices/" not in body
        assert "File name too long" not in body


def test_long_default_voice_id_is_400(tmp_path):
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    with TestClient(app) as client:
        response = client.patch(
            "/v1/settings",
            json={"values": {"AGENT_VOICE_DEFAULT_VOICE_ID": "x" * 300}},
        )
        assert response.status_code == 400
        assert "/tmp/" not in response.text


def test_forced_internal_error_has_no_exception_text(tmp_path, monkeypatch):
    app = create_app(settings=_settings(tmp_path), mount_mcp=False)
    ctx = app.state.ctx

    def boom():
        raise RuntimeError("secret /tmp/example/voices/path")

    monkeypatch.setattr(ctx.voices, "list", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/v1/voices")
    assert response.status_code == 500
    body = response.json()
    assert body["error_code"] == "INTERNAL_ERROR"
    assert body["message"] == "internal server error"
    assert body.get("correlation_id")
    assert "secret" not in response.text
    assert "/tmp/" not in response.text


def test_docs_require_token_when_keyed(tmp_path):
    app = create_app(
        settings=_settings(tmp_path, AGENT_VOICE_API_KEY="secret"), mount_mcp=False
    )
    with TestClient(app) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert client.get(path).status_code == 401, path
            assert (
                client.get(path, headers={"Authorization": "Bearer secret"}).status_code
                == 200
            ), path
        assert client.get("/health").status_code == 200
        assert client.get("/ui/").status_code == 200


def test_ingest_gate_limits_concurrency(tmp_path, monkeypatch):
    configure_ingest(2)
    active = 0
    maximum = 0
    lock = threading.Lock()

    def operation() -> None:
        nonlocal active, maximum
        with ingest_slot():
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.02)
            with lock:
                active -= 1

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: operation(), range(10)))
    assert maximum <= 2


def test_ingest_gate_saturation_is_backpressure(monkeypatch):
    import agent_voice.ingest as ingest

    configure_ingest(1)
    monkeypatch.setattr(ingest, "INGEST_WAIT_SECONDS", 0.05)
    result: list[object] = []

    def contend() -> None:
        try:
            with ingest_slot():
                result.append("acquired")
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    with ingest_slot():
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=2)
    assert result and getattr(result[0], "error_code", None) == "BACKPRESSURE"


def test_malformed_wav_error_is_sanitized(client):
    response = client.post(
        "/v1/voice-drafts",
        files={"file": ("broken.wav", b"RIFF\x24\x00\x00\x00WAVEfmt " + b"x" * 200, "audio/wav")},
    )
    assert response.status_code in {415, 422}
    text = response.text
    assert "upload.bin" not in text
    assert "[in#" not in text
    assert "0x" not in text
    assert "/tmp/" not in text


def test_artifact_pruning_respects_references(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    old = time.time() - 10_000
    referenced = root / "0123456789abcdef.wav"
    unreferenced = root / "fedcba9876543210.wav"
    referenced.write_bytes(b"x")
    unreferenced.write_bytes(b"x")
    os.utime(referenced, (old, old))
    os.utime(unreferenced, (old, old))
    removed = prune_artifacts(
        root, referenced_ids={"0123456789abcdef"}, ttl_seconds=3600
    )
    assert removed == 1
    assert referenced.is_file()
    assert not unreferenced.exists()


def test_draft_ttl_sweeper_removes_files(tmp_path):
    store = VoiceDraftStore(
        tmp_path / "drafts",
        ttl_seconds=1,
        max_count=8,
        max_bytes=1024 * 1024,
        quota_bytes=10 * 1024 * 1024,
    )
    draft_id = "0123456789abcdef"
    target = store.dir(draft_id)
    target.mkdir()
    (target / "draft.json").write_text(
        json.dumps({"expires_at": time.time() - 1}), encoding="utf-8"
    )
    (target / "audio.wav").write_bytes(b"x")
    assert store.prune() == 1
    assert not target.exists()


def test_sse_replay_from_last_event_id():
    bus = EventBus()
    first = bus.publish("job", {"n": 1})
    second = bus.publish("job", {"n": 2})
    queue = bus.subscribe(first.id)
    assert queue.get_nowait().id == second.id
    bus.unsubscribe(queue)


def test_web_build_manifest_has_no_stale_files():
    manifest_path = WEB / "web-build.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text("utf-8"))["files"]
    actual = {}
    for path in WEB.rglob("*"):
        if not path.is_file() or path.name == "web-build.json":
            continue
        actual[str(path.relative_to(WEB)).replace("\\", "/")] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    assert actual == manifest


def test_voice_export_import_round_trip(client):
    wav = _wav()
    uploaded = client.post("/v1/voice-drafts", files={"file": ("a.wav", wav, "audio/wav")})
    assert uploaded.status_code == 201
    created = client.post(
        "/v1/voices",
        json={
            "name": "Round trip",
            "draft_id": uploaded.json()["draft_id"],
            "ref_text": "Round trip",
            "consent": True,
        },
    )
    assert created.status_code == 200, created.text
    voice_id = created.json()["voice_id"]
    exported = client.get(f"/v1/voices/{voice_id}/export")
    assert exported.status_code == 200
    archive = exported.content
    assert client.delete(f"/v1/voices/{voice_id}").status_code == 200
    imported = client.post(
        "/v1/voices/import",
        files={"file": ("voice.zip", archive, "application/zip")},
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["name"] == "Round trip"


def test_voice_import_rejects_traversal_member(client):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", "{}")
        archive.writestr("../escape.wav", b"x")
    response = client.post(
        "/v1/voices/import",
        files={"file": ("evil.zip", buffer.getvalue(), "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] in {"ARCHIVE_UNSAFE", "BAD_REQUEST"}
