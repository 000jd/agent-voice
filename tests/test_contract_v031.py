"""v0.3.1 contract: quality inheritance, voice names, candidates, approval default."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_voice.config import Settings
from agent_voice.schemas import (
    BeatSpec,
    CloneVoiceRequest,
    DesignVoiceRequest,
    DesignVoiceResult,
    ScriptRequest,
    SpeakRequest,
    VoiceMetadata,
)


def _metadata(name: str) -> VoiceMetadata:
    return VoiceMetadata(
        voice_id="abc", name=name, ref_wav_sha256="0", duration_sec=4.0, created_at=0.0
    )


def test_beat_quality_is_optional_and_inherits():
    assert BeatSpec(text="x").quality is None
    assert BeatSpec(text="x", quality="fast").quality == "fast"


@pytest.mark.parametrize(
    "bad", ["../x", "bad\nname", "with/slash", "tab\tname", "x" * 65]
)
def test_voice_names_reject_unsafe_values(bad: str):
    with pytest.raises(ValidationError):
        _metadata(bad)
    with pytest.raises(ValidationError):
        CloneVoiceRequest(name=bad, ref_audio_path="a.wav")
    with pytest.raises(ValidationError):
        DesignVoiceRequest(name=bad, instruct="warm")


@pytest.mark.parametrize("name", ["gpu-tester", "mcp voice", "narrator.v2", "a_b"])
def test_valid_voice_names_are_accepted(name: str):
    assert CloneVoiceRequest(name=name, ref_audio_path="a.wav").name == name


def test_require_engine_approval_default_is_true():
    assert Settings.model_fields["require_engine_approval"].default is True


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_candidates_accepts_the_documented_range(count: int):
    request = DesignVoiceRequest(name="tutor", instruct="warm", candidates=count)
    assert request.candidates == count


@pytest.mark.parametrize("count", [0, 6])
def test_candidates_rejects_out_of_range(count: int):
    with pytest.raises(ValidationError):
        DesignVoiceRequest(name="tutor", instruct="warm", candidates=count)


def test_design_result_candidates_default_to_empty():
    result = DesignVoiceResult(
        name="tutor",
        preview_file_path="preview.wav",
        duration_sec=6.0,
        design_instruct="warm",
        engine_id="mock",
        model_alias="mock-design",
    )
    assert result.candidates == []


def test_pronunciation_overrides_exist_on_both_request_shapes():
    assert SpeakRequest(text="x", pronunciations={"a": "b"}).pronunciations == {"a": "b"}
    script = ScriptRequest(beats=[BeatSpec(text="x")], pronunciations={"a": "b"})
    assert script.pronunciations == {"a": "b"}
