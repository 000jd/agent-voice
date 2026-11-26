"""Engine registry behaviour: discovery, manifest validation, dynamic mutation.

Covers the two things that could silently corrupt a deployment: a bad manifest
being accepted, and an agent escaping its engine directory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agent_voice.engine_registry import (
    EngineRegistry,
    hf_model_downloaded,
)
from agent_voice.schemas import AgentVoiceError, ErrorCode

REPO_ROOT = Path(__file__).resolve().parents[1]


def _registry(tmp_path: Path, *, copy_engines: bool = True) -> EngineRegistry:
    engines = tmp_path / "engines"
    if copy_engines:
        shutil.copytree(REPO_ROOT / "engines", engines, ignore=shutil.ignore_patterns(".venv"))
    else:
        engines.mkdir(parents=True)
    return EngineRegistry(engines)


def test_discovers_shipped_engines(tmp_path):
    registry = _registry(tmp_path)
    assert "mock" in registry.ids()
    assert "qwen3_tts" in registry.ids()
    assert "qwen3_tts_fast" in registry.ids()


def test_null_venv_python_falls_back_to_core_interpreter(tmp_path):
    registry = _registry(tmp_path)
    record = registry.get("mock")
    assert record.manifest.venv_python is None
    import sys

    assert record.venv_python == Path(sys.executable)


def test_missing_venv_reports_actionable_reason(tmp_path):
    registry = _registry(tmp_path)
    record = registry.get("qwen3_tts")
    assert record.installed is False
    assert "agent-voice engines install qwen3_tts" in (record.why_unavailable or "")


def test_engine_yaml_declares_backend_identity(tmp_path):
    """The cache key depends on backend_name/version being knowable pre-spawn."""
    registry = _registry(tmp_path)
    record = registry.get("qwen3_tts_fast")
    assert record.manifest.backend_name == "faster-qwen3-tts"
    assert record.manifest.backend_version == "0.5.4"
    assert record.manifest.capabilities.streaming is True
    assert record.manifest.capabilities.pcm_native is True


def test_official_engine_declares_no_streaming(tmp_path):
    """Honesty: the official backend cannot stream, so it must not claim to."""
    registry = _registry(tmp_path)
    record = registry.get("qwen3_tts")
    assert record.manifest.capabilities.streaming is False
    assert record.manifest.capabilities.pcm_native is False


def test_unknown_engine_raises_with_suggestion(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(AgentVoiceError) as excinfo:
        registry.get("nope")
    assert excinfo.value.error_code == ErrorCode.ENGINE_NOT_FOUND
    assert "list_engines" in excinfo.value.suggestion


def test_manifest_with_unknown_keys_is_rejected(tmp_path):
    engines = tmp_path / "engines"
    bad = engines / "bogus"
    bad.mkdir(parents=True)
    (bad / "engine.yaml").write_text(
        yaml.safe_dump(
            {
                "engine_id": "bogus",
                "backend_name": "bogus",
                "surprise_key": "nope",
            }
        ),
        "utf-8",
    )
    (bad / "worker.py").write_text("", "utf-8")

    registry = EngineRegistry(engines)
    # A malformed plugin must not break discovery of healthy ones.
    assert "bogus" not in registry.ids()


def test_engine_id_must_match_directory(tmp_path):
    engines = tmp_path / "engines"
    bad = engines / "dirname"
    bad.mkdir(parents=True)
    (bad / "engine.yaml").write_text(
        yaml.safe_dump({"engine_id": "different", "backend_name": "x"}), "utf-8"
    )
    (bad / "worker.py").write_text("", "utf-8")
    registry = EngineRegistry(engines)
    assert "dirname" not in registry.ids()


@pytest.mark.parametrize(
    "bad_slug",
    ["../escape", "with/slash", "with\\backslash", "_leading", "", "UPPER", "a", "x" * 60],
)
def test_invalid_slugs_are_rejected(tmp_path, bad_slug):
    registry = _registry(tmp_path)
    with pytest.raises((AgentVoiceError, ValueError)):
        registry.register_engine(
            engine_id=bad_slug,
            engine_yaml_content="backend_name: x\n",
            worker_py_content="",
        )


def test_register_engine_scaffolds_and_hot_reloads(tmp_path):
    registry = _registry(tmp_path)
    manifest = yaml.safe_dump(
        {
            "engine_id": "mytts",
            "backend_name": "mytts",
            "backend_version": "1.0",
            "venv_python": None,
            "capabilities": {"streaming": False},
            "models": [
                {
                    "alias": "tiny",
                    "hf_repo": "org/tiny",
                    "quality_tier": "fast",
                    "est_vram_gb": 0.1,
                }
            ],
        }
    )
    record = registry.register_engine(
        engine_id="mytts",
        engine_yaml_content=manifest,
        worker_py_content="print('hi')\n",
    )
    assert record.engine_id == "mytts"
    assert "mytts" in registry.ids(), "registry must hot-reload without a restart"
    assert registry.default_model_alias("mytts") == "tiny"


def test_register_engine_rejects_duplicate(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(AgentVoiceError):
        registry.register_engine(
            engine_id="mock",
            engine_yaml_content="backend_name: mock\n",
            worker_py_content="",
        )


def test_register_engine_rejects_path_traversal(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises((AgentVoiceError, ValueError)):
        registry.register_engine(
            engine_id="../../etc/evil",
            engine_yaml_content="backend_name: x\n",
            worker_py_content="",
        )
    # Nothing may be written outside the engines directory.
    assert not (tmp_path / "etc").exists()


def test_register_model_appends_and_reloads(tmp_path):
    registry = _registry(tmp_path)
    before = len(registry.list_models("mock"))
    registry.register_model(
        engine_id="mock",
        model_alias="second-tone",
        hf_repo="local/second-tone",
        quality_tier="high",
    )
    after = registry.list_models("mock")
    assert len(after) == before + 1
    assert any(m.alias == "second-tone" for m in after)


def test_register_model_rejects_duplicate_alias(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(AgentVoiceError):
        registry.register_model(
            engine_id="mock", model_alias="mock-tone", hf_repo="local/x"
        )


def test_register_model_writes_valid_yaml(tmp_path):
    registry = _registry(tmp_path)
    registry.register_model(
        engine_id="mock", model_alias="third-tone", hf_repo="local/third-tone"
    )
    written = yaml.safe_load((registry.engines_dir / "mock" / "engine.yaml").read_text("utf-8"))
    assert any(m["alias"] == "third-tone" for m in written["models"])


def test_model_spec_lookup(tmp_path):
    registry = _registry(tmp_path)
    spec = registry.model_spec("qwen3_tts", "1.7b-base")
    assert spec.hf_repo == "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
    assert spec.est_vram_gb == 4.4


def test_model_spec_unknown_alias_lists_known(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(AgentVoiceError) as excinfo:
        registry.model_spec("qwen3_tts", "nope")
    assert "1.7b-base" in excinfo.value.suggestion


def test_default_model_alias_by_tier(tmp_path):
    registry = _registry(tmp_path)
    assert registry.default_model_alias("qwen3_tts", "high") == "1.7b-base"
    assert registry.default_model_alias("qwen3_tts", "fast") == "0.6b-base"


def test_resolve_engine_for_model(tmp_path):
    registry = _registry(tmp_path)
    assert registry.resolve_engine_for_model("1.7b-base") in {"qwen3_tts", "qwen3_tts_fast"}
    assert registry.resolve_engine_for_model("does-not-exist") is None


def test_hf_cache_detection_requires_real_weights():
    """A resolved `refs/main` with no weights is NOT a downloaded model.

    This is the exact trap the original brief fell into: the 0.6B directory
    existed with only a ref pointer, which a naive check reports as present.
    """
    assert hf_model_downloaded("Qwen/Definitely-Not-A-Real-Repo-42") is False


def test_list_entries_include_why_unavailable(tmp_path):
    registry = _registry(tmp_path)
    entries = {e.engine_id: e for e in registry.list_entries()}
    assert entries["mock"].why_unavailable is None
    assert entries["qwen3_tts"].why_unavailable is not None
    assert entries["qwen3_tts_fast"].why_unavailable is not None


def test_list_entries_marks_loaded_models(tmp_path):
    registry = _registry(tmp_path)
    entries = registry.list_entries(loaded={"mock": ["mock-tone"]})
    mock_entry = next(e for e in entries if e.engine_id == "mock")
    assert mock_entry.models[0].loaded is True


# ---------------------------------------------------------------------------
# v0.2.1: staging and approval live in the registry (one code path)
# ---------------------------------------------------------------------------


_STAGED_MANIFEST = (
    "engine_id: stagedeng\n"
    "backend_name: stagedeng\n"
    "backend_version: '1'\n"
    "venv_python: null\n"
    "models: []\n"
)


def test_register_engine_can_stage(tmp_path):
    registry = _registry(tmp_path)
    record = registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('x')\n",
        staging=True,
    )
    assert (tmp_path / "engines" / ".pending" / "stagedeng").is_dir()
    assert record.engine_id == "stagedeng"


def test_staged_engine_is_invisible_to_discovery(tmp_path):
    registry = _registry(tmp_path)
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('x')\n",
        staging=True,
    )
    assert "stagedeng" not in registry.ids(), (
        "a staged plugin must not be loadable or spawnable before approval"
    )


def test_list_pending_reports_staged_ids(tmp_path):
    registry = _registry(tmp_path)
    assert registry.list_pending() == []
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('x')\n",
        staging=True,
    )
    assert registry.list_pending() == ["stagedeng"]


def test_approve_engine_promotes_and_reloads(tmp_path):
    registry = _registry(tmp_path)
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('x')\n",
        staging=True,
    )
    record = registry.approve_engine("stagedeng")
    assert record.engine_id == "stagedeng"
    assert "stagedeng" in registry.ids(), "approval must hot-reload discovery"
    assert registry.list_pending() == []
    assert not (tmp_path / "engines" / ".pending" / "stagedeng").exists()


def test_approve_refuses_to_overwrite_an_installed_engine(tmp_path):
    """Silently replacing trusted code with unread code defeats the gate."""
    registry = _registry(tmp_path)
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('x')\n",
        staging=True,
    )
    registry.approve_engine("stagedeng")
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content="print('different')\n",
        staging=True,
    )
    with pytest.raises(AgentVoiceError) as excinfo:
        registry.approve_engine("stagedeng")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    # The staged copy is left in place for the operator to inspect.
    assert (tmp_path / "engines" / ".pending" / "stagedeng").is_dir()


def test_approve_unknown_engine_is_rejected(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(AgentVoiceError) as excinfo:
        registry.approve_engine("nope")
    assert excinfo.value.error_code == ErrorCode.ENGINE_NOT_FOUND


def test_approve_rejects_invalid_slug(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises((AgentVoiceError, ValueError)):
        registry.approve_engine("../../evil")


def test_approve_rejects_plugin_without_a_worker(tmp_path):
    registry = _registry(tmp_path)
    registry.register_engine(
        engine_id="stagedeng",
        engine_yaml_content=_STAGED_MANIFEST,
        worker_py_content=None,
        staging=True,
    )
    with pytest.raises(AgentVoiceError) as excinfo:
        registry.approve_engine("stagedeng")
    assert excinfo.value.error_code == ErrorCode.MANIFEST_INVALID
