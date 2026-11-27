"""The ``kind: asr`` half of the plugin contract.

Nothing here imports torch or spawns a real engine: the manifests, the registry
filters and the two workers' pure helpers are exercised directly. The phonon
worker is loaded by path because it is a script, not an installed package, and
it deliberately has no module-level backend import.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import ModuleType

import pytest

from agent_voice.api.routing import resolve_asr_target, resolve_target
from agent_voice.bindings import BindingStore
from agent_voice.cli import ASR_STEP_NAMES, STEP_NAMES, _asr_check, _conformance_target
from agent_voice.config import Settings
from agent_voice.engine_registry import (
    EngineRegistry,
    fermion_model_downloaded,
    weights_downloaded,
)
from agent_voice.schemas import (
    AgentVoiceError,
    EngineEntry,
    EngineManifest,
    ErrorCode,
    ModelEntry,
    ModelSpec,
)
from agent_voice.voice_store import VoiceStore

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINES = REPO_ROOT / "engines"


def _load_worker(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def phonon_worker() -> ModuleType:
    """The phonon worker module, with its module-scope env changes restored."""
    before = {
        key: os.environ.get(key) for key in ("CUDA_VISIBLE_DEVICES", "FERMION_DEVICE")
    }
    module = _load_worker(ENGINES / "phonon" / "worker.py", f"phonon_worker_{uuid.uuid4().hex}")
    try:
        yield module
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def mock_asr_worker() -> ModuleType:
    return _load_worker(
        ENGINES / "mock_asr" / "worker.py", f"mock_asr_worker_{uuid.uuid4().hex}"
    )


# ---------------------------------------------------------------------------
# Manifest and registry
# ---------------------------------------------------------------------------


def test_manifest_and_entries_default_to_tts() -> None:
    """Every v0.4 manifest must keep loading with no changes at all."""
    manifest = EngineManifest(engine_id="legacy", backend_name="legacy")
    assert manifest.kind == "tts"
    assert manifest.device == "gpu"
    assert manifest.capabilities.word_timestamps is False
    assert manifest.capabilities.hotwords is False
    assert manifest.capabilities.multilingual is False

    entry = EngineEntry(
        engine_id="legacy", backend_name="legacy", backend_version="1", installed=True
    )
    assert entry.kind == "tts"

    model = ModelEntry(
        engine_id="legacy",
        alias="m",
        hf_repo="local/m",
        quality_tier="high",
        est_vram_gb=0.0,
    )
    assert model.kind == "tts"


def test_asr_engines_are_discovered_with_their_kind() -> None:
    registry = EngineRegistry(ENGINES)
    assert {"mock_asr", "phonon"} <= set(registry.asr_engine_ids())

    asr = registry.list_entries(kind="asr")
    assert {entry.engine_id for entry in asr} == {"mock_asr", "phonon"}
    assert all(entry.kind == "asr" for entry in asr)
    assert all(entry.device == "cpu" for entry in asr)
    assert all(entry.word_timestamps for entry in asr)

    tts = registry.list_entries(kind="tts")
    assert "phonon" not in {entry.engine_id for entry in tts}
    assert "mock" in {entry.engine_id for entry in tts}

    aliases = {model.alias for model in registry.list_models(kind="asr")}
    assert {"phonon-2", "phonon-2.5", "phonon-2.5-en", "mock-asr-parity"} <= aliases
    assert "mock-tone" not in aliases


def test_default_asr_alias_and_kind_refusals(settings: Settings) -> None:
    registry = EngineRegistry(ENGINES)
    assert registry.default_asr_model_alias("mock_asr") == "mock-asr-parity"

    with pytest.raises(AgentVoiceError) as wrong_default:
        registry.default_asr_model_alias("mock")
    assert wrong_default.value.error_code == ErrorCode.MODEL_NOT_FOUND

    with pytest.raises(AgentVoiceError) as asr_refusal:
        resolve_asr_target(settings=settings, registry=registry, engine="mock")
    assert asr_refusal.value.error_code == ErrorCode.BAD_REQUEST
    assert "cannot transcribe" in asr_refusal.value.message

    voices = VoiceStore(settings.voices_dir, max_ref_text_chars=settings.max_ref_text_chars)
    bindings = BindingStore(settings.bindings_path)
    with pytest.raises(AgentVoiceError) as tts_refusal:
        resolve_target(
            settings=settings,
            registry=registry,
            voices=voices,
            bindings=bindings,
            engine="phonon",
            model="phonon-2.5",
        )
    assert tts_refusal.value.error_code == ErrorCode.BAD_REQUEST
    assert "cannot speak" in tts_refusal.value.message


# ---------------------------------------------------------------------------
# Phonon worker helpers
# ---------------------------------------------------------------------------


def test_phonon_worker_forces_cpu_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FERMION_DEVICE", "cuda")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    _load_worker(
        ENGINES / "phonon" / "worker.py", f"phonon_cpu_check_{uuid.uuid4().hex}"
    )
    assert os.environ["FERMION_DEVICE"] == "cpu"
    assert os.environ["CUDA_VISIBLE_DEVICES"] == ""


def test_phonon_sentence_grouping_closes_through_quotes(phonon_worker: ModuleType) -> None:
    words = [
        {"word": 'He said "done."', "start_sec": 0.0, "end_sec": 1.0},
        {"word": "Really?)", "start_sec": 1.1, "end_sec": 1.5},
        {"word": "Next", "start_sec": 2.7, "end_sec": 3.0},
    ]
    segments = phonon_worker.group_words_into_segments(words)

    assert [segment["text"] for segment in segments] == [
        'He said "done."',
        "Really?)",
        "Next",
    ]
    assert segments[0]["start_sec"] == 0.0
    assert segments[0]["end_sec"] == 1.0


def test_phonon_grouping_respects_pause_and_duration_ceiling(
    phonon_worker: ModuleType,
) -> None:
    words = [
        {"word": f"w{index}", "start_sec": float(index), "end_sec": index + 0.5}
        for index in range(10)
    ]
    segments = phonon_worker.group_words_into_segments(words)

    assert len(segments) > 1
    assert all(segment["end_sec"] - segment["start_sec"] <= 7.0 for segment in segments)
    assert [word["word"] for segment in segments for word in segment["words"]] == [
        f"w{index}" for index in range(10)
    ]


def test_phonon_normalisation_accepts_both_word_shapes(phonon_worker: ModuleType) -> None:
    raw = [
        {"text": "hello", "start": 0.0, "end": 0.5},
        {"word": "there", "start_sec": 0.5, "end_sec": 1.0},
        {"word": "no-timing"},
    ]
    assert phonon_worker._normalise_words(raw) == [
        {"word": "hello", "start_sec": 0.0, "end_sec": 0.5},
        {"word": "there", "start_sec": 0.5, "end_sec": 1.0},
    ]


def test_phonon_segments_inherit_flat_words_and_fall_back_to_grouping(
    phonon_worker: ModuleType,
) -> None:
    words = [
        {"word": "Hello.", "start_sec": 0.0, "end_sec": 0.5},
        {"word": "Again.", "start_sec": 0.6, "end_sec": 1.0},
    ]
    from_backend = phonon_worker._normalise_segments(
        [{"start": 0.0, "end": 1.0, "text": "Hello. Again."}], words
    )
    assert [word["word"] for word in from_backend[0]["words"]] == ["Hello.", "Again."]

    grouped = phonon_worker._normalise_segments(None, words)
    assert [segment["text"] for segment in grouped] == ["Hello.", "Again."]


# ---------------------------------------------------------------------------
# Mock ASR worker helpers
# ---------------------------------------------------------------------------


def test_mock_asr_spans_and_segments_are_deterministic(
    mock_asr_worker: ModuleType,
) -> None:
    assert mock_asr_worker._spans(9.0, 3) == [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)]
    assert mock_asr_worker._segment_count(0.5) == 2
    assert mock_asr_worker._segment_count(100.0) == 8

    plain = mock_asr_worker._segments(9.0, word_timestamps=False, hotwords=[])
    assert [segment["index"] for segment in plain] == [0, 1, 2]
    assert all("words" not in segment for segment in plain)

    with_words = mock_asr_worker._segments(
        9.0, word_timestamps=True, hotwords=["CUDA"]
    )
    assert "CUDA" in with_words[-1]["text"]
    assert all(segment["words"] for segment in with_words)
    assert with_words == mock_asr_worker._segments(
        9.0, word_timestamps=True, hotwords=["CUDA"]
    )


def test_mock_asr_reports_its_real_language(mock_asr_worker: ModuleType) -> None:
    engine = mock_asr_worker.MockAsrEngine
    assert engine._language({}) == "English"
    assert engine._language({"language": "fr"}) == "English"
    assert engine._language({"language": "auto"}) == "English"


# ---------------------------------------------------------------------------
# Engines test wiring and doctor
# ---------------------------------------------------------------------------


def test_conformance_target_uses_the_transcribe_row_for_asr() -> None:
    registry = EngineRegistry(ENGINES)
    kind, speaker, alias = _conformance_target(registry, "mock_asr")

    assert kind == "transcribe"
    assert speaker is None
    assert alias == "mock-asr-parity"
    assert len(ASR_STEP_NAMES) == len(STEP_NAMES) == 7
    assert "transcribe" in ASR_STEP_NAMES
    assert "synthesize" not in ASR_STEP_NAMES


def _entry(engine_id: str, *, installed: bool, aliases: tuple[str, ...] = ()) -> EngineEntry:
    return EngineEntry(
        engine_id=engine_id,
        backend_name=engine_id,
        backend_version="1",
        installed=installed,
        kind="asr",
        models=[
            ModelEntry(
                engine_id=engine_id,
                alias=alias,
                hf_repo=f"local/{alias}",
                quality_tier="fast",
                est_vram_gb=0.0,
                kind="asr",
            )
            for alias in aliases
        ],
    )


def test_doctor_asr_row_warns_when_the_default_is_missing(tmp_path: Path) -> None:
    settings = Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=ENGINES,
        AGENT_VOICE_DEFAULT_ASR_ENGINE="phonon",
    )
    check = _asr_check([_entry("mock_asr", installed=True)], settings)

    assert check.status == "WARN"
    assert "phonon" in check.detail
    assert "engines install phonon" in check.fix


def test_doctor_asr_row_passes_with_the_aliases(tmp_path: Path) -> None:
    settings = Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=ENGINES,
        AGENT_VOICE_DEFAULT_ASR_ENGINE="push-to-talk",
    )
    check = _asr_check(
        [_entry("push-to-talk", installed=True, aliases=("push-2.5",))], settings
    )

    assert check.status == "PASS"
    assert "push-2.5" in check.detail


# ---------------------------------------------------------------------------
# Fermion cache detection and the models-download predicate
# ---------------------------------------------------------------------------


def test_fermion_cache_detection_uses_the_unpacked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "fermion"
    model_dir = cache / "speech" / "FermionResearch__Phonon-2.5" / "model_phonon25"
    model_dir.mkdir(parents=True)
    (model_dir / "model.fermion").write_bytes(b"weights")
    monkeypatch.setenv("FERMION_CACHE_DIR", str(cache))

    assert fermion_model_downloaded("FermionResearch/Phonon-2.5") is True
    # A repo that is neither in this temp cache nor in the real ~/.cache/fermion
    # (the spike left Phonon-2 there, so it cannot be the negative case).
    assert fermion_model_downloaded("FermionResearch/Not-A-Phonon-Model") is False

    spec = ModelSpec(alias="phonon-2.5", hf_repo="FermionResearch/Phonon-2.5")
    manifest = EngineManifest(
        engine_id="phonon", backend_name="fermion-research", kind="asr"
    )
    assert weights_downloaded(spec, manifest) is True


def test_models_download_short_circuits_on_the_fermion_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from agent_voice.cli import app

    cache = tmp_path / "fermion"
    model_dir = cache / "speech" / "FermionResearch__Phonon-2.5" / "model_phonon25"
    model_dir.mkdir(parents=True)
    (model_dir / "model.fermion").write_bytes(b"weights")
    monkeypatch.setenv("FERMION_CACHE_DIR", str(cache))

    import huggingface_hub

    def _must_not_download(*args: object, **kwargs: object) -> None:
        raise AssertionError("snapshot_download must not run for a cached model")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", _must_not_download)

    result = CliRunner().invoke(app, ["models", "download", "phonon", "phonon-2.5"])

    assert result.exit_code == 0, result.output
    assert "already present locally" in result.output
