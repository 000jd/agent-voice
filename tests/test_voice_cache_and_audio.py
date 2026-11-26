"""Voice store, cache key isolation and the audio pipeline."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from agent_voice.audio_utils import (
    chunk_text,
    crossfade_stitch,
    fit_segment,
    pcm_bytes_to_seconds,
    probe_duration,
    write_srt,
    write_vtt,
)
from agent_voice.cache import CACHE_KEY_FIELDS, SynthesisCache, build_cache_key
from agent_voice.schemas import AgentVoiceError, ErrorCode
from agent_voice.voice_store import VoiceStore, sha256_file

SR = 24_000


def _tone(path: Path, seconds: float = 5.0, freq: float = 200.0) -> Path:
    n = int(SR * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    sf.write(path, (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32), SR)
    return path


def _key(**overrides) -> str:
    base = {
        "text": "hello",
        "voice_id": "v1",
        "ref_wav_sha256": "abc",
        "ref_text": "ref",
        "x_vector_only_mode": False,
        "engine_id": "mock",
        "model_alias": "mock-tone",
        "backend_name": "mock",
        "backend_version": "1",
        "language": "Auto",
        "seed": None,
        "speed": 1.0,
        "response_format": "wav",
        "normalize_lufs": -16.0,
        "chunk_chars": 400,
    }
    base.update(overrides)
    return build_cache_key(**base)


# ---------------------------------------------------------------------------
# Voice store
# ---------------------------------------------------------------------------


def test_add_voice_canonicalises_to_24khz_mono(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="tester", source_path=_tone(tmp_path / "in.wav"))
    ref = store.ref_wav_path(metadata.voice_id)
    assert ref.is_file()
    info = sf.info(str(ref))
    assert info.samplerate == SR
    assert info.channels == 1


def test_voice_metadata_records_sha256(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="tester", source_path=_tone(tmp_path / "in.wav"))
    assert metadata.ref_wav_sha256 == sha256_file(store.ref_wav_path(metadata.voice_id))
    assert len(metadata.ref_wav_sha256) == 64


def test_short_reference_rejected(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    with pytest.raises(AgentVoiceError) as excinfo:
        store.add(name="short", source_path=_tone(tmp_path / "in.wav", seconds=0.4))
    assert excinfo.value.error_code == ErrorCode.INVALID_REF_AUDIO
    assert "minimum" in excinfo.value.message


def test_overlong_reference_rejected(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    with pytest.raises(AgentVoiceError) as excinfo:
        store.add(name="long", source_path=_tone(tmp_path / "in.wav", seconds=45))
    assert excinfo.value.error_code == ErrorCode.INVALID_REF_AUDIO


def test_sweet_spot_warning(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="longish", source_path=_tone(tmp_path / "in.wav", seconds=20))
    assert any("sweet spot" in w for w in metadata.warnings)


def test_empty_ref_text_warns_about_icl_quality(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(
        name="no-text", source_path=_tone(tmp_path / "in.wav"), ref_text="", x_vector_only=False
    )
    assert any("ref_text is empty" in w for w in metadata.warnings)


def test_resolve_by_name_and_id(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="resolvable", source_path=_tone(tmp_path / "in.wav"))
    assert store.resolve("resolvable").voice_id == metadata.voice_id
    assert store.resolve(metadata.voice_id).name == "resolvable"


def test_delete_voice_removes_directory(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="temp", source_path=_tone(tmp_path / "in.wav"))
    assert store.delete(metadata.voice_id) is True
    assert store.delete(metadata.voice_id) is False


def test_missing_voice_raises_with_suggestion(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    with pytest.raises(AgentVoiceError) as excinfo:
        store.get("nope")
    assert excinfo.value.error_code == ErrorCode.VOICE_NOT_FOUND
    assert "list_voices" in excinfo.value.suggestion


def test_prompt_cache_invalidated_when_backend_version_changes(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="p", source_path=_tone(tmp_path / "in.wav"))
    prompt_path, _ = store.prompt_cache_paths(metadata.voice_id, "mock", "mock-tone")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_bytes(b"fake")

    from agent_voice.voice_store import PromptCacheWriter

    PromptCacheWriter(store).record(
        voice_id=metadata.voice_id,
        engine_id="mock",
        model_alias="mock-tone",
        prompt_path=prompt_path,
        ref_wav_sha256=metadata.ref_wav_sha256,
        ref_text=metadata.ref_text,
        backend_version="1",
    )
    cached = store.load_prompt(
        voice_id=metadata.voice_id,
        engine_id="mock",
        model_alias="mock-tone",
        ref_wav_sha256=metadata.ref_wav_sha256,
        ref_text=metadata.ref_text,
        backend_version="1",
    )
    assert cached == prompt_path

    # A backend upgrade must invalidate the prompt: a stale prompt produces the
    # wrong speaker silently, which is far worse than recomputing.
    stale = store.load_prompt(
        voice_id=metadata.voice_id,
        engine_id="mock",
        model_alias="mock-tone",
        ref_wav_sha256=metadata.ref_wav_sha256,
        ref_text=metadata.ref_text,
        backend_version="2",
    )
    assert stale is None
    assert not prompt_path.exists()


def test_prompt_cache_invalidated_when_ref_text_changes(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="p2", source_path=_tone(tmp_path / "in.wav"))
    prompt_path, _ = store.prompt_cache_paths(metadata.voice_id, "mock", "mock-tone")
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_bytes(b"fake")

    from agent_voice.voice_store import PromptCacheWriter

    PromptCacheWriter(store).record(
        voice_id=metadata.voice_id,
        engine_id="mock",
        model_alias="mock-tone",
        prompt_path=prompt_path,
        ref_wav_sha256=metadata.ref_wav_sha256,
        ref_text="original transcript",
        backend_version="1",
    )
    result = store.load_prompt(
        voice_id=metadata.voice_id,
        engine_id="mock",
        model_alias="mock-tone",
        ref_wav_sha256=metadata.ref_wav_sha256,
        ref_text="different transcript",
        backend_version="1",
    )
    assert result is None


def test_prompt_cache_separate_per_engine_and_model(tmp_path):
    store = VoiceStore(tmp_path / "voices")
    metadata = store.add(name="p3", source_path=_tone(tmp_path / "in.wav"))
    a, _ = store.prompt_cache_paths(metadata.voice_id, "qwen3_tts", "1.7b-base")
    b, _ = store.prompt_cache_paths(metadata.voice_id, "qwen3_tts_fast", "1.7b-base")
    c, _ = store.prompt_cache_paths(metadata.voice_id, "qwen3_tts", "0.6b-base")
    assert a != b != c


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------


def test_cache_key_is_deterministic():
    assert _key() == _key()


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "different"),
        ("voice_id", "v2"),
        ("ref_wav_sha256", "def"),
        ("ref_text", "other"),
        ("x_vector_only_mode", True),
        ("engine_id", "qwen3_tts"),
        ("model_alias", "1.7b-base"),
        ("backend_name", "qwen-tts"),
        ("backend_version", "2"),
        ("language", "English"),
        ("seed", 42),
        ("speed", 1.5),
        ("response_format", "mp3"),
        ("normalize_lufs", -20.0),
        ("chunk_chars", 200),
    ],
)
def test_every_cached_field_changes_the_key(field, value):
    """Full isolation: any parameter that can alter audio must alter the key."""
    assert _key() != _key(**{field: value}), f"{field} must participate in the cache key"


def test_all_fields_are_covered():
    assert set(CACHE_KEY_FIELDS) == {
        "text",
        "voice_id",
        "ref_wav_sha256",
        "ref_text",
        "x_vector_only_mode",
        "engine_id",
        "model_alias",
        "backend_name",
        "backend_version",
        "language",
        "seed",
        "speed",
        "response_format",
        "normalize_lufs",
        "chunk_chars",
    }


def test_missing_field_is_an_error():
    with pytest.raises(ValueError, match="missing required fields"):
        build_cache_key(text="x")


def test_cache_put_and_get(tmp_path):
    cache = SynthesisCache(tmp_path / "cache")
    artifact = _tone(tmp_path / "a.wav", seconds=1)
    entry = cache.put("a" * 64, artifact, format="wav")
    assert entry.path.is_file()

    hit = cache.get("a" * 64)
    assert hit is not None
    assert hit.path == entry.path


def test_cache_miss_returns_none(tmp_path):
    cache = SynthesisCache(tmp_path / "cache")
    assert cache.get("b" * 64) is None


def test_cache_lru_eviction_respects_size_bound(tmp_path):
    cache = SynthesisCache(tmp_path / "cache", max_gb=0.0001)  # ~100 KB
    for index in range(4):
        artifact = _tone(tmp_path / f"{index}.wav", seconds=1)
        cache.put(f"{index:064d}", artifact, format="wav")
    assert cache.total_bytes() <= cache.max_bytes


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def test_chunking_splits_on_sentences():
    chunks = chunk_text("One. Two. Three.", max_chars=100)
    assert [c.text for c in chunks] == ["One.", "Two.", "Three."]


def test_chunking_handles_cjk_terminators():
    chunks = chunk_text("你好。今天天气不错！再见？", max_chars=100)
    assert len(chunks) == 3


def test_chunking_hard_wraps_long_runs():
    chunks = chunk_text("x" * 50, max_chars=10)
    assert all(len(c.text) <= 10 for c in chunks)
    assert "".join(c.text for c in chunks) == "x" * 50


def test_chunking_preserves_all_text():
    text = "Alpha beta. Gamma delta! Epsilon zeta?"
    chunks = chunk_text(text, max_chars=100)
    joined = " ".join(c.text for c in chunks)
    assert joined.split() == text.split()


def test_chunking_empty_input():
    assert chunk_text("   ") == []


def test_chunk_indices_are_sequential():
    chunks = chunk_text("A. B. C. D.", max_chars=10)
    assert [c.index for c in chunks] == list(range(len(chunks)))


# ---------------------------------------------------------------------------
# Audio pipeline
# ---------------------------------------------------------------------------


def test_crossfade_stitch_length(tmp_path):
    a = _tone(tmp_path / "a.wav", seconds=1.0, freq=200)
    b = _tone(tmp_path / "b.wav", seconds=1.0, freq=300)
    out = tmp_path / "out.wav"
    duration = crossfade_stitch([a, b], out)
    # A 20 ms crossfade overlaps, so total is slightly under 2 s.
    assert 1.9 < duration < 2.01


def test_crossfade_single_segment(tmp_path):
    a = _tone(tmp_path / "a.wav", seconds=1.0)
    out = tmp_path / "out.wav"
    duration = crossfade_stitch([a], out)
    assert abs(duration - 1.0) < 0.01


def test_crossfade_empty_list_raises(tmp_path):
    with pytest.raises(AgentVoiceError):
        crossfade_stitch([], tmp_path / "out.wav")


def test_pad_fit_mode(tmp_path):
    source = _tone(tmp_path / "a.wav", seconds=1.0)
    out = tmp_path / "padded.wav"
    duration = fit_segment(
        source, out, target_duration_sec=2.0, fit_mode="pad"
    )
    assert abs(duration - 2.0) < 0.05
    assert abs(probe_duration(out) - 2.0) < 0.05


def test_atempo_fit_mode_compresses(tmp_path):
    source = _tone(tmp_path / "a.wav", seconds=2.0)
    out = tmp_path / "fast.wav"
    duration = fit_segment(
        source, out, target_duration_sec=1.0, fit_mode="atempo"
    )
    assert abs(duration - 1.0) < 0.1


def test_atempo_extreme_ratio_is_chained(tmp_path):
    """A single atempo stage refuses ratios outside [0.5, 2.0]; we chain them."""
    source = _tone(tmp_path / "a.wav", seconds=8.0)
    out = tmp_path / "veryfast.wav"
    duration = fit_segment(
        source, out, target_duration_sec=1.0, fit_mode="atempo"
    )
    assert duration < 2.0, "an 8x speedup must actually compress"


def test_fit_mode_none_copies(tmp_path):
    source = _tone(tmp_path / "a.wav", seconds=1.0)
    out = tmp_path / "copy.wav"
    duration = fit_segment(source, out, target_duration_sec=5.0, fit_mode="none")
    assert abs(duration - 1.0) < 0.01


def test_unknown_fit_mode_rejected(tmp_path):
    source = _tone(tmp_path / "a.wav", seconds=1.0)
    with pytest.raises(AgentVoiceError):
        fit_segment(source, tmp_path / "o.wav", target_duration_sec=2.0, fit_mode="stretch")


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------


def test_srt_format(tmp_path):
    out = write_srt([(0.0, 1.5, "First line"), (1.5, 3.0, "Second line")], tmp_path / "a.srt")
    content = out.read_text("utf-8")
    assert "00:00:00,000 --> 00:00:01,500" in content
    assert "1\n00:00:00,000" in content
    assert content.strip().endswith("Second line")


def test_vtt_format_uses_dot_separator(tmp_path):
    out = write_vtt([(0.0, 1.5, "Line")], tmp_path / "a.vtt")
    content = out.read_text("utf-8")
    assert content.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:01.500" in content


def test_srt_handles_hours(tmp_path):
    out = write_srt([(3661.5, 3662.0, "Late")], tmp_path / "a.srt")
    assert "01:01:01,500" in out.read_text("utf-8")


def test_pcm_bytes_to_seconds():
    assert pcm_bytes_to_seconds(SR * 2) == pytest.approx(1.0)


def test_probe_duration_matches_written_file(tmp_path):
    path = _tone(tmp_path / "d.wav", seconds=2.5)
    assert probe_duration(path) == pytest.approx(2.5, abs=0.02)
