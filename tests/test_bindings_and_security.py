"""Bindings atomicity, path confinement and subprocess hygiene."""

from __future__ import annotations

import ast
import json
import os
import threading
from pathlib import Path

import pytest

from agent_voice.bindings import BindingStore
from agent_voice.schemas import AgentVoiceError, ErrorCode
from agent_voice.security import (
    ensure_within_base,
    is_loopback_client,
    resolve_artifact_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "agent_voice"


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------


def test_set_and_get_binding(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    binding = store.set("agent-a", voice_id="v1", engine_id="mock")
    assert binding.agent_id == "agent-a"
    assert binding.voice_id == "v1"
    assert store.get("agent-a").engine_id == "mock"


def test_missing_binding_is_none(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    assert store.get("nobody") is None


def test_delete_binding(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    store.set("agent-a", voice_id="v1")
    assert store.delete("agent-a") is True
    assert store.delete("agent-a") is False


def test_partial_update_preserves_other_fields(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    store.set("agent-a", voice_id="v1", engine_id="mock")
    store.set("agent-a", model_alias="mock-tone")
    binding = store.get("agent-a")
    assert binding.voice_id == "v1"
    assert binding.engine_id == "mock"
    assert binding.model_alias == "mock-tone"


def test_precedence_explicit_over_binding_over_default(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    store.set("agent-a", voice_id="bound-voice", engine_id="qwen3_tts")

    voice, engine, model = store.resolve(
        agent_id="agent-a",
        explicit_voice="explicit-voice",
        explicit_engine=None,
        explicit_model=None,
        default_voice="default-voice",
        default_engine="mock",
    )
    assert voice == "explicit-voice"
    assert engine == "qwen3_tts"

    voice, engine, _ = store.resolve(
        agent_id="agent-a",
        explicit_voice=None,
        explicit_engine=None,
        explicit_model=None,
        default_voice="default-voice",
        default_engine="mock",
    )
    assert voice == "bound-voice"
    assert engine == "qwen3_tts"

    voice, engine, _ = store.resolve(
        agent_id=None,
        explicit_voice=None,
        explicit_engine=None,
        explicit_model=None,
        default_voice="default-voice",
        default_engine="mock",
    )
    assert voice == "default-voice"
    assert engine == "mock"


def test_concurrent_writers_never_corrupt_the_file(tmp_path):
    """Atomic temp-file + os.replace means no interleaved partial writes."""
    path = tmp_path / "bindings.json"
    store = BindingStore(path)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for iteration in range(20):
                store.set(f"agent-{index}", voice_id=f"v{iteration}")
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # The document must always be valid JSON with every agent present.
    data = json.loads(path.read_text("utf-8"))
    assert len(data["bindings"]) == 8
    for index in range(8):
        assert f"agent-{index}" in data["bindings"]


def test_concurrent_writers_do_not_lose_updates(tmp_path):
    path = tmp_path / "bindings.json"
    store = BindingStore(path)

    def worker(index: int) -> None:
        store.set(f"agent-{index}", voice_id=f"voice-{index}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    bindings = {b.agent_id: b for b in store.list()}
    assert len(bindings) == 16
    for index in range(16):
        assert bindings[f"agent-{index}"].voice_id == f"voice-{index}"


def test_malformed_bindings_file_is_tolerated(tmp_path):
    path = tmp_path / "bindings.json"
    path.write_text("{not json", "utf-8")
    store = BindingStore(path)
    assert store.list() == []


def test_invalid_engine_id_rejected_in_binding(tmp_path):
    store = BindingStore(tmp_path / "bindings.json")
    with pytest.raises(ValueError):
        store.set("agent-a", engine_id="../evil")


# ---------------------------------------------------------------------------
# Path confinement
# ---------------------------------------------------------------------------


def test_path_inside_base_is_allowed(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    target = base / "ref.wav"
    target.write_bytes(b"x")
    assert ensure_within_base(target, base) == target.resolve()


def test_relative_path_resolves_inside_base(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    (base / "ref.wav").write_bytes(b"x")
    assert ensure_within_base("ref.wav", base) == (base / "ref.wav").resolve()


def test_traversal_out_of_base_rejected(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    with pytest.raises(AgentVoiceError) as excinfo:
        ensure_within_base("../outside.wav", base)
    assert excinfo.value.error_code == ErrorCode.PATH_OUTSIDE_BASE


def test_absolute_path_outside_base_rejected(tmp_path):
    base = tmp_path / "sandbox"
    base.mkdir()
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"x")
    with pytest.raises(AgentVoiceError) as excinfo:
        ensure_within_base(outside, base)
    assert excinfo.value.error_code == ErrorCode.PATH_OUTSIDE_BASE


def test_symlink_inside_base_pointing_out_rejected(tmp_path):
    """A symlink whose target is outside the sandbox must not be followed."""
    base = tmp_path / "sandbox"
    base.mkdir()
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"secret")
    link = base / "link.wav"
    os.symlink(outside, link)

    with pytest.raises(AgentVoiceError) as excinfo:
        ensure_within_base(link, base)
    assert excinfo.value.error_code == ErrorCode.PATH_OUTSIDE_BASE
    assert "symlink" in excinfo.value.message


def test_prefix_confusion_attack_rejected(tmp_path):
    """`/base-evil` must not pass a naive startswith('/base') check."""
    base = tmp_path / "base"
    base.mkdir()
    evil = tmp_path / "base-evil"
    evil.mkdir()
    target = evil / "x.wav"
    target.write_bytes(b"x")

    with pytest.raises(AgentVoiceError):
        ensure_within_base(target, base)


def test_artifact_lands_inside_mcp_base_when_set(tmp_path):
    """The returned path must be readable inside the agent's sandbox."""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    path = resolve_artifact_path(
        "abc123", "wav", artifacts_dir=tmp_path / "data", mcp_base_path=str(sandbox)
    )
    assert sandbox.resolve() in path.parents
    assert path.name == "abc123.wav"


def test_artifact_uses_data_dir_when_mcp_base_unset(tmp_path):
    artifacts = tmp_path / "data" / "artifacts"
    path = resolve_artifact_path("abc123", "wav", artifacts_dir=artifacts, mcp_base_path="")
    assert path.parent == artifacts.resolve()


def test_artifact_suffix_normalised(tmp_path):
    path = resolve_artifact_path("a", "mp3", artifacts_dir=tmp_path, mcp_base_path="")
    assert path.suffix == ".mp3"
    path2 = resolve_artifact_path("a", ".mp3", artifacts_dir=tmp_path, mcp_base_path="")
    assert path2.suffix == ".mp3"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_detection_accepts_local(host):
    assert is_loopback_client(host) is True


@pytest.mark.parametrize("host", ["192.168.1.5", "10.0.0.1", "8.8.8.8", None, ""])
def test_loopback_detection_rejects_remote(host):
    assert is_loopback_client(host) is False


# ---------------------------------------------------------------------------
# Subprocess hygiene
# ---------------------------------------------------------------------------


def test_no_shell_true_anywhere():
    """There must be no code path that builds a shell command string."""
    offenders: list[str] = []
    for path in list(SRC.rglob("*.py")) + list((REPO_ROOT / "engines").rglob("worker.py")):
        source = path.read_text("utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True:
                        offenders.append(f"{path}: shell=True")
    assert offenders == [], f"shell=True found: {offenders}"


def test_no_os_system_calls():
    offenders: list[str] = []
    for path in list(SRC.rglob("*.py")) + list((REPO_ROOT / "engines").rglob("worker.py")):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
                and node.attr in {"system", "popen"}
            ):
                offenders.append(f"{path}: os.{node.attr}")
    assert offenders == [], f"unsafe process calls found: {offenders}"


def test_core_never_imports_torch_at_module_level():
    """The 0 MB VRAM guarantee is structural: torch must not be importable here."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "torch":
                        offenders.append(f"{path}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] == "torch":
                    offenders.append(f"{path}: from {node.module}")
    assert offenders == [], f"core package imports torch: {offenders}"
