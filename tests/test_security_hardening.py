"""Security hardening regression tests (v0.2).

Every test here encodes a defect found by live probing of v0.1:
* the shipped example enabled agent engine mutation
* `/audio/{id}` was unauthenticated and used a wildcard glob
* the MCP mutation path silently skipped the loopback check
* `Host`/`Origin` were never validated (DNS-rebinding exposure)
* bearer comparison was not constant-time

They are written to fail against v0.1 and pass against v0.2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_voice.api.app import create_app
from agent_voice.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, **overrides) -> Settings:
    """Copy engine manifests into tmp so mutation tests never touch the repo.

    Only manifest/worker files are copied: an engine venv can be gigabytes and no
    security test needs a real interpreter. Without this, a test that calls
    register_model rewrites the shipped `engines/mock/engine.yaml`.
    """
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
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def client(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def authed_client(tmp_path):
    settings = _settings(tmp_path, AGENT_VOICE_API_KEY="secret-key")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# S1: the example must not enable agent mutation
# ---------------------------------------------------------------------------


def test_example_env_disables_agent_mutation():
    """Enabling this lets an agent write and execute Python on the host."""
    text = (REPO_ROOT / ".env.example").read_text("utf-8")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("ALLOW_AGENT_ENGINE_MUTATION=")
    ]
    assert lines, "ALLOW_AGENT_ENGINE_MUTATION must be documented in .env.example"
    assert lines[0] == "ALLOW_AGENT_ENGINE_MUTATION=false", (
        f"unsafe default shipped: {lines[0]}"
    )


# ---------------------------------------------------------------------------
# S3: Host / Origin guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "testserver"])
def test_loopback_hosts_are_accepted(client, host):
    response = client.get("/health", headers={"Host": host})
    assert response.status_code == 200


def test_foreign_host_is_rejected(client):
    response = client.get("/health", headers={"Host": "evil.example.com"})
    assert response.status_code == 421, "foreign Host must not be served"
    body = response.json()
    assert body["error_code"] == "BAD_REQUEST"
    assert body["suggestion"]


def test_foreign_origin_is_rejected(client):
    response = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert response.status_code == 403
    assert response.json()["error_code"] == "BAD_REQUEST"


def test_loopback_origin_is_accepted(client):
    response = client.get("/health", headers={"Origin": "http://127.0.0.1:8000"})
    assert response.status_code == 200


def test_host_with_port_is_parsed(client):
    """Host headers carry a port; comparison must ignore it."""
    assert client.get("/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/health", headers={"Host": "127.0.0.1:9999"}).status_code == 200


def test_configured_allowed_hosts_are_accepted(tmp_path):
    settings = _settings(tmp_path, AGENT_VOICE_ALLOWED_HOSTS="agentbox.local,10.0.0.5")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        assert test_client.get("/health", headers={"Host": "agentbox.local"}).status_code == 200
        assert test_client.get("/health", headers={"Host": "10.0.0.5"}).status_code == 200


# ---------------------------------------------------------------------------
# S3b: refuse an unsafe bind
# ---------------------------------------------------------------------------


def test_non_loopback_bind_without_key_refuses_to_start(tmp_path):
    """Binding a LAN interface with no auth turns a voice server into an open relay."""
    settings = _settings(tmp_path, AGENT_VOICE_HOST="0.0.0.0", AGENT_VOICE_API_KEY="")
    with pytest.raises(Exception) as excinfo:
        app = create_app(settings=settings, mount_mcp=False)
        with TestClient(app):
            pass
    message = str(excinfo.value).lower()
    assert "api_key" in message or "api key" in message or "loopback" in message


def test_non_loopback_bind_with_key_is_allowed(tmp_path):
    settings = _settings(tmp_path, AGENT_VOICE_HOST="0.0.0.0", AGENT_VOICE_API_KEY="k")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "127.0.0.5"])
def test_loopback_bind_without_key_is_allowed(tmp_path, host):
    settings = _settings(tmp_path, AGENT_VOICE_HOST=host)
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        assert test_client.get("/health").status_code == 200


# ---------------------------------------------------------------------------
# S5: /audio must be authenticated and must not glob
# ---------------------------------------------------------------------------


def test_audio_requires_bearer_when_key_set(authed_client):
    response = authed_client.get("/audio/0123456789abcdef")
    assert response.status_code == 401


def test_audio_accessible_with_bearer(authed_client):
    created = authed_client.post(
        "/v1/agent/speak",
        json={"text": "Authenticated artifact."},
        headers={"Authorization": "Bearer secret-key"},
    )
    assert created.status_code == 200
    artifact_id = created.json()["artifact_id"]
    response = authed_client.get(
        f"/audio/{artifact_id}", headers={"Authorization": "Bearer secret-key"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "bad_id",
    [
        "*",
        "..",
        "../etc/passwd",
        "ABCDEF0123456789",
        "short",
        "0123456789abcde",
        "0123456789abcdef0",
    ],
)
def test_audio_rejects_malformed_artifact_ids(client, bad_id):
    response = client.get(f"/audio/{bad_id}")
    assert response.status_code in {400, 403, 404}, f"{bad_id!r} was not rejected"
    assert b"RIFF" not in response.content, "must never serve audio for a bad id"

    # Traversal-looking ids are rejected by Starlette's router before our handler
    # runs, so they carry no structured body. Anything that does reach the handler
    # must use the standard error envelope.
    if response.headers.get("content-type", "").startswith("application/json"):
        body = response.json()
        if "detail" not in body:
            assert body["error_code"] in {"BAD_REQUEST", "VOICE_NOT_FOUND"}


def test_audio_wildcard_does_not_list_artifacts(client):
    """The v0.1 glob let `*` match the first artifact on disk."""
    created = client.post("/v1/agent/speak", json={"text": "Some artifact."})
    assert created.status_code == 200

    response = client.get("/audio/*")
    assert response.status_code == 400
    assert b"RIFF" not in response.content, "must never serve audio for a wildcard id"


# ---------------------------------------------------------------------------
# S4: constant-time comparison
# ---------------------------------------------------------------------------


def test_bearer_comparison_uses_compare_digest():
    """A byte-by-byte compare leaks the key prefix through timing."""
    source = (REPO_ROOT / "src" / "agent_voice" / "api" / "app.py").read_text("utf-8")
    assert "compare_digest" in source, "bearer_auth must use hmac.compare_digest"


def test_wrong_key_length_rejected(authed_client):
    """compare_digest must still behave correctly on a length mismatch."""
    response = authed_client.post(
        "/v1/agent/speak",
        json={"text": "Hi."},
        headers={"Authorization": "Bearer secret-key-plus-extra"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# S7: mutation audit log
# ---------------------------------------------------------------------------


def test_mutation_writes_audit_line(tmp_path):
    settings = _settings(tmp_path, ALLOW_AGENT_ENGINE_MUTATION="true")
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/v1/models",
            json={
                "engine_id": "mock",
                "model_alias": "audit-tone",
                "hf_repo": "local/audit-tone",
                "download": False,
            },
        )
        assert response.status_code == 200

    log_path = settings.data_dir / "mutations.log"
    assert log_path.is_file(), "mutation must be audited"
    lines = [line for line in log_path.read_text("utf-8").splitlines() if line.strip()]
    assert lines, "audit log is empty"
    entry = json.loads(lines[-1])
    for key in ("ts", "action", "engine_id", "model_alias"):
        assert key in entry, f"audit line missing {key}"
    assert entry["model_alias"] == "audit-tone"


# ---------------------------------------------------------------------------
# S2: MCP mutations must respect the same loopback rule
# ---------------------------------------------------------------------------


def test_mcp_server_has_no_fake_request_shim():
    """`_FakeRequest.client` returned None, silently disabling the loopback check."""
    source = (REPO_ROOT / "src" / "agent_voice" / "mcp_server.py").read_text("utf-8")
    assert "_FakeRequest" not in source, (
        "MCP must call services.script_service with an explicit identity, "
        "not fake an HTTP request"
    )


def test_script_service_is_transport_agnostic():
    """The extracted service must take agent_id explicitly and not touch Request."""
    source = (
        REPO_ROOT / "src" / "agent_voice" / "services" / "script_service.py"
    ).read_text("utf-8")
    assert "from fastapi" not in source, "service layer must not depend on FastAPI"
    assert "agent_id" in source


def test_script_service_accepts_progress_callback():
    source = (
        REPO_ROOT / "src" / "agent_voice" / "services" / "script_service.py"
    ).read_text("utf-8")
    assert "on_beat" in source, "per-beat progress needs a callback hook"
