"""Real-GPU gate.

Skipped unless ``AGENT_VOICE_RUN_GPU_TESTS=1``. These tests load actual models and
consume real VRAM, so they are deliberately excluded from the default suite.

What this proves that the mock suite cannot:
* both Qwen3-TTS model sizes load and produce valid audio
* a preset timbre and a designed voice render valid audio on real hardware
* the CUDA-graph backend genuinely streams (not emulated), in every mode
* a design render becomes an ordinary clone once it is saved as a voice
* MAX_RESIDENT_MODELS=1 evicts the design checkpoint for the clone checkpoint
* barge-in cancel stops generation quickly
* unload really returns the GPU to 0 MB
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from agent_voice.api.app import AppContext
from agent_voice.cache import SynthesisCache
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry, hf_model_downloaded
from agent_voice.schemas import (
    DesignVoiceRequest,
    DesignVoiceResult,
    SynthesisResult,
    VoiceMetadata,
)
from agent_voice.services.voice_design_service import render_design
from agent_voice.voice_store import VoiceStore
from agent_voice.worker_manager import WorkerManager

pytestmark = pytest.mark.gpu

REPO_ROOT = Path(__file__).resolve().parents[1]
SR = 24_000

ENGINES = ["qwen3_tts", "qwen3_tts_fast"]

CLONE_MODELS = ["0.6b-base", "1.7b-base"]
"""The two Base checkpoints: the only aliases that can clone a reference clip."""

MODELS = [*CLONE_MODELS, "1.7b-custom", "0.6b-custom", "1.7b-design"]
"""Every alias both shipped manifests declare: clone, preset timbre and design."""

MODEL_REPOS = {
    "0.6b-base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
    "1.7b-base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "1.7b-custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "0.6b-custom": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "1.7b-design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
}

CUSTOM_INSTRUCT = "Speak brightly and cheerfully, a little faster than usual."

DESIGN_INSTRUCT = (
    "A warm, low-pitched British narrator in his forties, unhurried and "
    "reassuring, with a hint of gravel."
)
"""VoiceDesign takes a description, not a keyword list: a real sentence is the
contract, and it is also what a caller would actually write."""


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="qwen3_tts_fast",
        MCP_BASE_PATH="",
        MODEL_IDLE_TTL_SECONDS=900,
    )
    base.update(overrides)
    return Settings(**base)


def _nvidia_used_mb() -> int | None:
    """Actual driver-reported VRAM use, not torch's allocator view."""
    if shutil.which("nvidia-smi") is None:
        return None
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def _engine_ready(engine_id: str) -> bool:
    registry = EngineRegistry(REPO_ROOT / "engines")
    try:
        return registry.require_installed(engine_id) is not None
    except Exception:
        return False


def _require_model(engine_id: str, *model_aliases: str) -> None:
    """Skip unless the engine venv *and* every named checkpoint are local.

    A missing checkpoint is not a test failure: the worker would try to pull 4 GB
    from the Hub mid-run, which is neither a contract violation nor quick.
    """
    if not _engine_ready(engine_id):
        pytest.skip(f"{engine_id} venv not installed; run `agent-voice engines install`")
    for alias in model_aliases:
        repo = MODEL_REPOS[alias]
        if not hf_model_downloaded(repo):
            pytest.skip(f"{repo} not present locally; run `agent-voice models download`")


def _assert_valid_wav(path: str | Path, *, min_seconds: float = 0.3) -> None:
    """The engine contract: 24 kHz mono, and actually longer than a blip."""
    out = Path(path)
    assert out.is_file(), f"no audio was written to {out}"
    info = sf.info(str(out))
    assert info.samplerate == SR
    assert info.channels == 1
    assert info.frames > SR * min_seconds, f"audio must be longer than {min_seconds}s"


def _ref_wav(tmp_path: Path) -> Path:
    """A 6 s clip in the recommended sweet spot."""
    path = tmp_path / "ref.wav"
    seconds = 6.0
    n = int(SR * seconds)
    t = np.linspace(0, seconds, n, endpoint=False)
    # Two formants so the speaker embedding has something to latch onto.
    signal = 0.18 * np.sin(2 * np.pi * 180 * t) + 0.09 * np.sin(2 * np.pi * 720 * t)
    sf.write(path, signal.astype(np.float32), SR)
    return path


REF_TRANSCRIPT = (
    "This is a reference clip used to verify voice cloning on real hardware."
)
"""ICL mode needs an accurate transcript; an empty one makes the engine fail with
`Error opening ''` rather than producing bad audio, which is the correct
behaviour but an unhelpful test."""


def _manager(settings: Settings) -> WorkerManager:
    return WorkerManager(
        settings=settings,
        registry=EngineRegistry(settings.engines_dir),
        cache=SynthesisCache(settings.cache_dir),
        voices=VoiceStore(settings.voices_dir),
    )


async def _drain_pcm(queue: asyncio.Queue[bytes], timeout: float = 120.0) -> tuple[int, int]:
    """Read a PCM stream to its end-of-stream sentinel.

    Returns ``(non-empty chunk count, total bytes)``. The sentinel is an empty
    bytes object, so a producer that dies early still terminates this loop.
    """
    chunks = 0
    total = 0
    while True:
        chunk = await asyncio.wait_for(queue.get(), timeout=timeout)
        if not chunk:
            return chunks, total
        chunks += 1
        total += len(chunk)


@pytest.mark.parametrize("engine_id", ENGINES)
def test_engine_is_installed(engine_id):
    if not _engine_ready(engine_id):
        pytest.skip(f"{engine_id} venv not installed; run `agent-voice engines install`")


@pytest.mark.parametrize("model_alias", MODELS)
def test_models_are_downloaded(model_alias):
    repo = MODEL_REPOS[model_alias]
    if not hf_model_downloaded(repo):
        pytest.skip(f"{repo} not present locally; run `agent-voice models download`")


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_id", ENGINES)
@pytest.mark.parametrize("model_alias", CLONE_MODELS)
async def test_clone_and_synthesise(engine_id, model_alias, tmp_path):
    """The core promise: a cloned voice produces a valid 24 kHz WAV."""
    if not _engine_ready(engine_id):
        pytest.skip(f"{engine_id} not installed")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(
        name="gpu-tester", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT
    )

    manager = _manager(settings)
    await manager.start()
    try:
        result = await manager.synthesize(
            engine_id=engine_id,
            model_alias=model_alias,
            text="This sentence verifies that cloning works on real hardware.",
            voice_id=voice.voice_id,
            ref_text=voice.ref_text,
            x_vector_only=False,
            language="English",
        )
    finally:
        await manager.stop()

    out = Path(result.file_path)
    assert out.is_file()
    info = sf.info(str(out))
    assert info.samplerate == SR
    assert info.channels == 1
    assert info.frames > SR * 0.5, "audio must be longer than half a second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("engine_id", "model_alias", "speaker", "instruct"),
    [
        ("qwen3_tts", "1.7b-custom", "Ryan", CUSTOM_INSTRUCT),
        ("qwen3_tts_fast", "1.7b-custom", "Ryan", CUSTOM_INSTRUCT),
        # The installed 0.6B CustomVoice backend drops instruct (both manifests
        # say `supports_instruct: false`), so the preset-only path runs there.
        ("qwen3_tts_fast", "0.6b-custom", "Ryan", ""),
    ],
    ids=[
        "qwen3_tts-1.7b-custom-instruct",
        "qwen3_tts_fast-1.7b-custom-instruct",
        "qwen3_tts_fast-0.6b-custom-preset",
    ],
)
async def test_custom_voice_synthesise(engine_id, model_alias, speaker, instruct, tmp_path):
    """A preset timbre needs no reference clip, and instruct must reach the model."""
    _require_model(engine_id, model_alias)

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        result = await manager.synthesize(
            engine_id=engine_id,
            model_alias=model_alias,
            text="This sentence is spoken from a preset timbre, not a cloned clip.",
            mode="custom_voice",
            speaker=speaker,
            instruct=instruct,
            language="English",
        )
    finally:
        await manager.stop()

    assert result.mode == "custom_voice"
    assert result.speaker == speaker
    assert result.voice_id is None, "a preset render has no reference voice"
    _assert_valid_wav(result.file_path, min_seconds=0.3)


@pytest.mark.asyncio
async def test_voice_design_synthesise(tmp_path):
    """VoiceDesign turns a description into audio with no reference clip at all."""
    _require_model("qwen3_tts_fast", "1.7b-design")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        result = await manager.synthesize(
            engine_id="qwen3_tts_fast",
            model_alias="1.7b-design",
            text=(
                "Designing a voice out of nothing but a sentence of description is "
                "the entire point of this render."
            ),
            mode="voice_design",
            instruct=DESIGN_INSTRUCT,
            language="English",
        )
    finally:
        await manager.stop()

    assert result.mode == "voice_design"
    assert result.voice_id is None
    _assert_valid_wav(result.file_path, min_seconds=0.3)


@pytest.mark.asyncio
async def test_mp3_encoding_of_real_audio(tmp_path):
    if not _engine_ready("qwen3_tts"):
        pytest.skip("qwen3_tts not installed")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(name="mp3", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT)
    manager = _manager(settings)
    await manager.start()
    try:
        result = await manager.synthesize(
            engine_id="qwen3_tts",
            model_alias="0.6b-base",
            text="Encoding to mp3.",
            voice_id=voice.voice_id,
            ref_text=voice.ref_text,
            response_format="mp3",
        )
    finally:
        await manager.stop()
    assert Path(result.file_path).stat().st_size > 1000


@pytest.mark.asyncio
async def test_native_streaming_yields_audio_during_decode(tmp_path):
    """The CUDA-graph backend must stream natively, not buffer-then-chunk."""
    if not _engine_ready("qwen3_tts_fast"):
        pytest.skip("qwen3_tts_fast not installed")
    if not hf_model_downloaded("Qwen/Qwen3-TTS-12Hz-0.6B-Base"):
        pytest.skip("0.6b-base not downloaded")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(name="streamer", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT)

    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, mode = await manager.stream(
            engine_id="qwen3_tts_fast",
            model_alias="0.6b-base",
            text="Streaming this longer sentence so that several chunks are produced.",
            voice_id=voice.voice_id,
            ref_text=voice.ref_text,
            language="English",
        )
        assert mode == "native"

        chunks: list[float] = []
        chunk_bytes = 0
        while True:
            chunk = await asyncio.wait_for(queue.get(), timeout=120)
            if not chunk:
                break
            chunk_bytes += len(chunk)
            chunks.append(asyncio.get_running_loop().time())
        assert len(chunks) > 1, "native streaming must emit multiple chunks"
        assert chunk_bytes > SR, "at least half a second of PCM overall"
    finally:
        await manager.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_alias", "mode", "speaker", "instruct"),
    [
        ("1.7b-custom", "custom_voice", "Vivian", CUSTOM_INSTRUCT),
        ("1.7b-design", "voice_design", None, DESIGN_INSTRUCT),
    ],
    ids=["custom_voice", "voice_design"],
)
async def test_native_streaming_for_preset_and_design(
    model_alias, mode, speaker, instruct, tmp_path
):
    """Preset and designed voices must stream natively too, not just clone.

    Both entry points exist upstream (``generate_custom_voice_streaming`` and
    ``generate_voice_design_streaming``); routing them through the clone-only
    path would silently degrade to one buffered blob.
    """
    _require_model("qwen3_tts_fast", model_alias)

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, streaming_mode = await manager.stream(
            engine_id="qwen3_tts_fast",
            model_alias=model_alias,
            text=(
                "Streaming this longer sentence makes the decoder produce several "
                "chunks instead of one buffered blob."
            ),
            mode=mode,
            speaker=speaker,
            instruct=instruct,
            language="English",
        )
        assert streaming_mode == "native", f"{mode} must stream natively"

        chunks, pcm_bytes = await _drain_pcm(queue)
        assert chunks > 1, f"{mode} native streaming must emit multiple chunks"
        assert pcm_bytes > SR, "at least half a second of PCM overall"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_barge_in_cancel_is_fast(tmp_path):
    """Barge-in must actually stop the GPU within 200 ms."""
    if not _engine_ready("qwen3_tts_fast"):
        pytest.skip("qwen3_tts_fast not installed")
    if not hf_model_downloaded("Qwen/Qwen3-TTS-12Hz-0.6B-Base"):
        pytest.skip("0.6b-base not downloaded")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(name="canceller", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT)

    manager = _manager(settings)
    await manager.start()
    try:
        stream_id, queue, _mode = await manager.stream(
            engine_id="qwen3_tts_fast",
            model_alias="0.6b-base",
            text=(
                "This is a deliberately long passage designed to keep the decoder "
                "busy for several seconds so that a barge-in cancellation has "
                "something meaningful to interrupt mid-generation."
            ),
            voice_id=voice.voice_id,
            ref_text=voice.ref_text,
            language="English",
        )

        # Wait until the first audio arrives so decode is genuinely running.
        await asyncio.wait_for(queue.get(), timeout=120)

        started = asyncio.get_running_loop().time()
        cancelled = await manager.cancel_stream("qwen3_tts_fast", stream_id)
        elapsed_ms = (asyncio.get_running_loop().time() - started) * 1000

        assert cancelled is True
        assert elapsed_ms < 200, f"cancel took {elapsed_ms:.0f} ms, budget is 200 ms"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_unload_returns_gpu_to_zero(tmp_path):
    """`unload` must free VRAM, verified by the driver rather than by torch."""
    if not _engine_ready("qwen3_tts"):
        pytest.skip("qwen3_tts not installed")
    baseline = _nvidia_used_mb()
    if baseline is None:
        pytest.skip("nvidia-smi unavailable")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(name="vram", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT)
    manager = _manager(settings)
    await manager.start()
    try:
        await manager.synthesize(
            engine_id="qwen3_tts",
            model_alias="0.6b-base",
            text="Occupy the GPU.",
            voice_id=voice.voice_id,
            ref_text=voice.ref_text,
        )
        loaded = _nvidia_used_mb()
        assert loaded is not None and loaded > baseline, "model should consume VRAM"

        await manager.unload_models(force=True)
        await asyncio.sleep(2.0)

        after = _nvidia_used_mb()
        assert after is not None
        # Allow a small driver/context overhead margin.
        assert after <= baseline + 512, (
            f"VRAM after unload ({after} MB) did not return near baseline "
            f"({baseline} MB)"
        )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_cache_hit_avoids_gpu_after_warm(tmp_path):
    """A repeat request must be served from disk with the worker gone."""
    if not _engine_ready("qwen3_tts"):
        pytest.skip("qwen3_tts not installed")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    voices = VoiceStore(settings.voices_dir)
    voice = voices.add(name="cache", source_path=_ref_wav(tmp_path), ref_text=REF_TRANSCRIPT)
    manager = _manager(settings)
    await manager.start()
    try:
        text = "Cache this exact sentence."
        await manager.synthesize(
            engine_id="qwen3_tts", model_alias="0.6b-base", text=text,
            voice_id=voice.voice_id, ref_text=voice.ref_text,
        )
        await manager.unload_models(force=True)
        spawns_before = manager.spawn_count

        hit = await manager.synthesize(
            engine_id="qwen3_tts", model_alias="0.6b-base", text=text,
            voice_id=voice.voice_id, ref_text=voice.ref_text,
        )
        assert hit.cached is True
        assert manager.spawn_count == spawns_before, "cache hit must not respawn the worker"
    finally:
        await manager.stop()


# ---------------------------------------------------------------------------
# v0.3: "design once, speak a thousand times"
# ---------------------------------------------------------------------------


async def _design_then_clone(
    ctx: AppContext,
) -> tuple[DesignVoiceResult, VoiceMetadata, SynthesisResult]:
    """Design a voice from a description, then clone it on the same engine.

    Shared by two tests that observe different end states of the identical
    sequence: the provenance of the saved voice, and the VRAM left behind. Both
    steps stay on ``qwen3_tts_fast`` so the resident-model cap is what has to
    evict the design checkpoint — no engine switch is involved.
    """
    design = await render_design(
        ctx,
        DesignVoiceRequest(
            name="gpu-designer",
            instruct=DESIGN_INSTRUCT,
            engine="qwen3_tts_fast",
            quality="high",
        ),
    )
    assert design.model_alias == "1.7b-design"
    assert design.voice_id is not None, "save_as_clone defaults to true"
    _assert_valid_wav(design.preview_file_path, min_seconds=0.3)

    voice = ctx.voices.get(design.voice_id)
    clone = await ctx.workers.synthesize(
        engine_id="qwen3_tts_fast",
        model_alias="1.7b-base",
        text="The designed voice is an ordinary reference clip from here on.",
        voice_id=voice.voice_id,
        ref_text=voice.ref_text,
        x_vector_only=False,
        language="English",
    )
    return design, voice, clone


@pytest.mark.asyncio
async def test_design_to_clone_bridge(tmp_path):
    """A designed voice must be usable exactly like a cloned one, and cheap.

    The bridge is the whole v0.3 promise: design once (expensive, 1.7B
    VoiceDesign), then reuse the saved reference on the cheaper clone path. With
    ``MAX_RESIDENT_MODELS=1`` the design checkpoint must be evicted, not co-resident.
    """
    _require_model("qwen3_tts_fast", "1.7b-design", "1.7b-base")

    settings = _settings(tmp_path, MAX_RESIDENT_MODELS=1)
    settings.ensure_dirs()
    ctx = AppContext(settings)
    await ctx.workers.start()
    try:
        design, voice, clone = await _design_then_clone(ctx)

        # The render is persisted as a normal voice, with its provenance intact.
        assert voice.voice_id == design.voice_id
        assert voice.source == "designed"
        assert voice.design_instruct == DESIGN_INSTRUCT
        assert voice.ref_text, "a designed voice keeps its anchor sentence as transcript"

        # ... and the clone half really speaks that voice's reference clip.
        assert clone.voice_id == voice.voice_id
        assert clone.mode == "clone"
        assert clone.model_alias == "1.7b-base"
        _assert_valid_wav(clone.file_path, min_seconds=0.3)

        assert ctx.workers.loaded_models().get("qwen3_tts_fast") == ["1.7b-base"], (
            "MAX_RESIDENT_MODELS=1 must evict the design checkpoint for the clone one"
        )
    finally:
        await ctx.workers.stop()


@pytest.mark.asyncio
async def test_design_to_clone_bridge_releases_vram(tmp_path):
    """A full design -> clone cycle must still hand the GPU back to the driver.

    The design checkpoint is a second 1.7B model in the life of one worker. If
    eviction leaked the unloaded one, this is the only test that would notice.
    """
    baseline = _nvidia_used_mb()
    if baseline is None:
        pytest.skip("nvidia-smi unavailable")
    _require_model("qwen3_tts_fast", "1.7b-design", "1.7b-base")

    settings = _settings(tmp_path, MAX_RESIDENT_MODELS=1)
    settings.ensure_dirs()
    ctx = AppContext(settings)
    await ctx.workers.start()
    try:
        await _design_then_clone(ctx)
        in_use = _nvidia_used_mb()
        assert in_use is not None and in_use > baseline, "the bridge should hold VRAM"

        await ctx.workers.unload_models(force=True)
        await asyncio.sleep(2.0)

        after = _nvidia_used_mb()
        assert after is not None
        assert after <= baseline + 512, (
            f"VRAM after the bridge unload ({after} MB) did not return near baseline "
            f"({baseline} MB)"
        )
    finally:
        await ctx.workers.stop()


@pytest.mark.asyncio
async def test_design_candidates_render_and_save_reuses_cache(tmp_path):
    """candidates=2 renders two takes; saving one reuses the cached artifact."""
    _require_model("qwen3_tts_fast", "1.7b-design")

    settings = _settings(tmp_path)
    settings.ensure_dirs()
    ctx = AppContext(settings)
    await ctx.workers.start()
    try:
        result = await render_design(
            ctx,
            DesignVoiceRequest(
                name="gpu-candidates",
                instruct=DESIGN_INSTRUCT,
                engine="qwen3_tts_fast",
                quality="high",
                candidates=2,
            ),
        )
        assert result.voice_id is None, "multi-candidate renders never save a voice"
        assert [candidate.take for candidate in result.candidates] == [1, 2]
        paths = {candidate.file_path for candidate in result.candidates}
        assert len(paths) == 2, "each take must produce its own artifact"
        for candidate in result.candidates:
            _assert_valid_wav(candidate.file_path, min_seconds=0.2)

        chosen = result.candidates[1]
        saved = await render_design(
            ctx,
            DesignVoiceRequest(
                name="gpu-candidates",
                instruct=DESIGN_INSTRUCT,
                engine="qwen3_tts_fast",
                quality="high",
                take=chosen.take,
                save_as_clone=True,
            ),
        )
        assert saved.voice_id is not None
        assert saved.preview_file_path == chosen.file_path, (
            "saving the chosen take must reuse the cached candidate artifact"
        )
        _assert_valid_wav(saved.preview_file_path, min_seconds=0.2)
    finally:
        await ctx.workers.stop()
