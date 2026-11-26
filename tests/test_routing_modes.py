"""v0.3 routing: the three ways to choose a timbre, and how they conflict.

``resolve_target`` is the single place REST and MCP agree on what a request
means. This module pins the v0.3 additions:

* ``speaker`` alone asks a CustomVoice checkpoint for a preset timbre,
* ``instruct`` with no reference clip asks a VoiceDesign checkpoint to invent one,
* everything else keeps cloning, exactly as v0.2 did,
* an explicit or bound model decides the mode, and a model that cannot do what
  was asked fails loudly instead of silently producing the wrong voice.

Every test routes against the shipped manifests (the mock engine by default) and
never spawns a worker: this is pure resolution logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from agent_voice.api import routing
from agent_voice.api.routing import ResolvedTarget, resolve_target
from agent_voice.bindings import BindingStore
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry
from agent_voice.schemas import AgentVoiceError, ErrorCode, VoiceMetadata
from agent_voice.voice_store import VoiceStore

REPO_ROOT = Path(__file__).resolve().parents[1]
SR = 24_000

QWEN_ENGINE = "qwen3_tts"
QWEN_ENGINES = ["qwen3_tts", "qwen3_tts_fast"]
ALL_MODES = {"clone", "custom_voice", "voice_design"}


@dataclass
class Rig:
    """Everything ``resolve_target`` needs, pointed at the shipped manifests."""

    settings: Settings
    registry: EngineRegistry
    voices: VoiceStore
    bindings: BindingStore
    tmp_path: Path
    ffmpeg: bool = field(default=True)

    def resolve(self, **kwargs) -> ResolvedTarget:
        return resolve_target(
            settings=self.settings,
            registry=self.registry,
            voices=self.voices,
            bindings=self.bindings,
            **kwargs,
        )

    def add_voice(self, name: str = "narrator", ref_text: str = "hello there") -> VoiceMetadata:
        """Register a real 5 s reference clip, because the store canonicalises it."""
        if not self.ffmpeg:
            pytest.skip("ffmpeg is required to register a reference voice")
        path = self.tmp_path / f"{name}.wav"
        n = int(SR * 5.0)
        t = np.linspace(0.0, 5.0, n, endpoint=False)
        sf.write(path, (0.2 * np.sin(2 * np.pi * 210.0 * t)).astype(np.float32), SR)
        return self.voices.add(name=name, source_path=path, ref_text=ref_text)


@pytest.fixture
def rig(tmp_path: Path, has_ffmpeg: bool) -> Rig:
    settings = Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="mock",
        AGENT_VOICE_DEFAULT_VOICE_ID="",
        MCP_BASE_PATH="",
    )
    return Rig(
        settings=settings,
        registry=EngineRegistry(REPO_ROOT / "engines"),
        voices=VoiceStore(tmp_path / "data" / "voices"),
        bindings=BindingStore(tmp_path / "data" / "bindings.json"),
        tmp_path=tmp_path,
        ffmpeg=has_ffmpeg,
    )


def _require_installed(registry: EngineRegistry, engine_id: str) -> None:
    """These tests need a real engine venv to reach the manifest-driven path."""
    try:
        registry.require_installed(engine_id)
    except AgentVoiceError as exc:
        pytest.skip(f"{engine_id} is not installed: {exc.message}")


# ---------------------------------------------------------------------------
# Intent decides the mode when the caller names no model
# ---------------------------------------------------------------------------


def test_speaker_selects_custom_voice_with_canonical_spelling(rig: Rig):
    target = rig.resolve(speaker="ryan")

    assert target.mode == "custom_voice"
    assert target.engine_id == "mock"
    assert target.model_alias == "mock-custom"
    # Canonicalised to the manifest spelling: the speaker is part of the cache
    # key, so `ryan` and `Ryan` must not become two entries for one voice.
    assert target.speaker == "Ryan"
    assert target.instruct == ""
    assert target.voice_id is None
    assert target.ref_text == ""
    assert target.x_vector_only is False


def test_instruct_alone_selects_voice_design(rig: Rig):
    description = "a warm, low British narrator in his forties, unhurried"
    target = rig.resolve(instruct=description)

    assert target.mode == "voice_design"
    assert target.model_alias == "mock-design"
    assert target.instruct == description
    assert target.speaker is None
    # A designed voice *is* its own reference; it must not inherit a clip.
    assert target.voice_id is None
    assert target.ref_text == ""
    assert target.x_vector_only is False


def test_explicit_voice_selects_clone(rig: Rig):
    metadata = rig.add_voice(ref_text="the quick brown fox")
    target = rig.resolve(voice=metadata.name)

    assert target.mode == "clone"
    assert target.model_alias == "mock-tone"
    assert target.voice_id == metadata.voice_id
    assert target.ref_text == "the quick brown fox"
    assert target.x_vector_only is False
    assert target.speaker is None


def test_no_arguments_still_clones(rig: Rig):
    """The v0.2 path is unchanged: no speaker, no instruct, no model -> clone."""
    target = rig.resolve()

    assert target.mode == "clone"
    assert target.engine_id == "mock"
    assert target.model_alias == "mock-tone"
    # The mock engine cannot clone and no voice is registered, so there is no
    # reference to attach -- but the *mode* must still be clone.
    assert target.voice_id is None
    assert target.speaker is None
    assert target.instruct == ""


@pytest.mark.parametrize(
    "kwargs,expected_mode,expected_alias",
    [
        pytest.param({"model": "mock:mock-tone"}, "clone", "mock-tone", id="explicit-clone"),
        pytest.param(
            {"model": "mock:mock-custom", "speaker": "Serena"},
            "custom_voice",
            "mock-custom",
            id="explicit-custom",
        ),
        pytest.param(
            {"model": "mock:mock-design", "instruct": "gravelly"},
            "voice_design",
            "mock-design",
            id="explicit-design",
        ),
    ],
)
def test_explicit_model_decides_the_mode(rig: Rig, kwargs, expected_mode, expected_alias):
    target = rig.resolve(**kwargs)

    assert target.mode == expected_mode
    assert target.model_alias == expected_alias


# ---------------------------------------------------------------------------
# Contradictory or incomplete requests fail loudly
# ---------------------------------------------------------------------------


def test_voice_and_speaker_cannot_be_combined(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(voice="narrator", speaker="Ryan")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "voice and speaker" in excinfo.value.message


def test_custom_voice_model_without_speaker_is_rejected(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(model="mock:mock-custom")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "speaker" in excinfo.value.message


def test_unknown_speaker_is_rejected_and_lists_the_presets(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(model="mock:mock-custom", speaker="Nobody")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "Nobody" in excinfo.value.message
    assert "Ryan" in excinfo.value.suggestion


def test_design_model_without_instruct_is_rejected(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(model="mock:mock-design")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "instruct" in excinfo.value.message


def test_unknown_voice_still_fails_for_clone(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(voice="does-not-exist")
    assert excinfo.value.error_code == ErrorCode.VOICE_NOT_FOUND


# ---------------------------------------------------------------------------
# Instruct support is a per-model fact, not a per-engine one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine_id", QWEN_ENGINES)
def test_instruct_works_on_1_7b_custom(rig: Rig, engine_id):
    _require_installed(rig.registry, engine_id)

    target = rig.resolve(
        engine=engine_id,
        model="1.7b-custom",
        speaker="Ryan",
        instruct="speak slowly and warmly",
    )

    assert target.mode == "custom_voice"
    assert target.model_alias == "1.7b-custom"
    assert target.speaker == "Ryan"
    assert target.instruct == "speak slowly and warmly"


@pytest.mark.parametrize("engine_id", QWEN_ENGINES)
def test_instruct_on_0_6b_custom_is_rejected_with_the_alternative(rig: Rig, engine_id):
    """The installed 0.6B CustomVoice backend has no instruct channel."""
    _require_installed(rig.registry, engine_id)

    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(
            engine=engine_id,
            model="0.6b-custom",
            speaker="Ryan",
            instruct="speak slowly and warmly",
        )
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "1.7b-custom" in excinfo.value.message
    assert "1.7b-custom" in excinfo.value.suggestion

    # Dropping the instruction is the supported path for the same checkpoint.
    plain = rig.resolve(engine=engine_id, model="0.6b-custom", speaker="Ryan")
    assert plain.mode == "custom_voice"
    assert plain.instruct == ""


# ---------------------------------------------------------------------------
# force_mode: voice design is its own reference, so bindings must not leak in
# ---------------------------------------------------------------------------


def test_force_mode_ignores_a_bound_voice_and_model(rig: Rig):
    metadata = rig.add_voice(name="bound")
    rig.bindings.set(
        "designer", voice_id=metadata.voice_id, engine_id="mock", model_alias="mock-tone"
    )

    target = rig.resolve(
        agent_id="designer",
        instruct="a brash radio host",
        force_mode="voice_design",
    )

    assert target.mode == "voice_design"
    assert target.model_alias == "mock-design"
    assert target.instruct == "a brash radio host"
    # Both halves of the binding are ignored: the render is its own reference.
    assert target.voice_id is None
    assert target.ref_text == ""
    assert target.x_vector_only is False


def test_force_mode_accepts_an_explicit_design_model(rig: Rig):
    target = rig.resolve(
        model="mock:mock-design",
        instruct="a whisper",
        force_mode="voice_design",
    )
    assert target.mode == "voice_design"
    assert target.model_alias == "mock-design"


def test_force_mode_rejects_a_model_of_the_wrong_mode(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(
            model="mock:mock-tone",
            instruct="a whisper",
            force_mode="voice_design",
        )
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "clone" in excinfo.value.message
    assert "voice_design" in excinfo.value.message
    assert "mock-design" in excinfo.value.suggestion


# ---------------------------------------------------------------------------
# Model choice: weights already on disk beat the requested quality tier
# ---------------------------------------------------------------------------


def test_downloaded_model_wins_over_the_requested_tier(rig: Rig, monkeypatch):
    _require_installed(rig.registry, QWEN_ENGINE)

    def downloaded_only(*repos: str):
        return lambda hf_repo: hf_repo in set(repos)

    # Only the fast checkpoint is on disk: speak a tier lower rather than pull 4 GB.
    monkeypatch.setattr(
        routing,
        "hf_model_downloaded",
        downloaded_only("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"),
    )
    small = rig.resolve(engine=QWEN_ENGINE, speaker="Ryan", quality="high")
    assert small.mode == "custom_voice"
    assert small.model_alias == "0.6b-custom"
    assert small.speaker == "Ryan"

    # Nothing on disk: the download check is a preference, not a filter, so the
    # requested tier still decides instead of erroring out.
    monkeypatch.setattr(routing, "hf_model_downloaded", downloaded_only())
    big = rig.resolve(engine=QWEN_ENGINE, speaker="Ryan", quality="high")
    assert big.mode == "custom_voice"
    assert big.model_alias == "1.7b-custom"
    assert big.speaker == "Ryan"


# ---------------------------------------------------------------------------
# Cross-cutting invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"speaker": "Ryan"}, id="speaker-only"),
        pytest.param({"model": "mock:mock-custom", "speaker": "serena"}, id="explicit-custom"),
        pytest.param({"instruct": "a warm narrator"}, id="instruct-only"),
        pytest.param({"model": "mock:mock-design", "instruct": "gravelly"}, id="explicit-design"),
    ],
)
def test_non_clone_modes_carry_no_reference(rig: Rig, kwargs):
    target = rig.resolve(**kwargs)

    assert target.mode in {"custom_voice", "voice_design"}
    assert target.voice_id is None
    assert target.ref_text == ""
    assert target.x_vector_only is False


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"model": "mock:mock-tone", "instruct": "shout it"}, id="with-instruct"),
        pytest.param({"model": "mock:mock-tone"}, id="without-instruct"),
    ],
)
def test_clone_ignores_instruct(rig: Rig, kwargs):
    """A Base checkpoint has no instruct channel, so carrying one would only widen
    the cache key for a parameter the engine cannot use."""
    target = rig.resolve(**kwargs)

    assert target.mode == "clone"
    assert target.instruct == ""


def test_clone_ignores_instruct_even_with_a_voice(rig: Rig):
    rig.add_voice()
    target = rig.resolve(voice="narrator", instruct="shout it")

    assert target.mode == "clone"
    assert target.voice_id is not None
    assert target.instruct == ""


def test_every_resolved_mode_is_a_known_mode(rig: Rig):
    """Guards the wire contract: a fourth mode needs schemas.ModelMode first."""
    for kwargs in ({"speaker": "Ryan"}, {"instruct": "soft"}, {}):
        assert rig.resolve(**kwargs).mode in ALL_MODES


# ---------------------------------------------------------------------------
# Explicit conflicts are errors, never silent drops
# ---------------------------------------------------------------------------


def test_speaker_on_an_explicit_clone_model_is_rejected(rig: Rig):
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(model="mock:mock-tone", speaker="Ryan")
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "clone" in excinfo.value.message


def test_voice_on_an_explicit_design_model_is_rejected(rig: Rig):
    metadata = rig.add_voice()
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(
            model="mock:mock-design",
            voice=metadata.voice_id,
            instruct="a whisper",
        )
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "voice_design" in excinfo.value.message


def test_force_mode_rejects_an_explicit_voice(rig: Rig):
    metadata = rig.add_voice()
    with pytest.raises(AgentVoiceError) as excinfo:
        rig.resolve(
            voice=metadata.voice_id,
            instruct="a whisper",
            force_mode="voice_design",
        )
    assert excinfo.value.error_code == ErrorCode.BAD_REQUEST
    assert "force_mode" in excinfo.value.message
