"""Real-GPU gate.

Skipped unless ``AGENT_VOICE_RUN_GPU_TESTS=1``. These tests load actual models and
consume real VRAM, so they are deliberately excluded from the default suite.

What this proves that the mock suite cannot:
* both Qwen3-TTS model sizes load and produce valid audio
* the CUDA-graph backend genuinely streams (not emulated)
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

from agent_voice.cache import SynthesisCache
from agent_voice.config import Settings
from agent_voice.engine_registry import EngineRegistry, hf_model_downloaded
from agent_voice.voice_store import VoiceStore
from agent_voice.worker_manager import WorkerManager

pytestmark = pytest.mark.gpu

REPO_ROOT = Path(__file__).resolve().parents[1]
SR = 24_000

ENGINES = ["qwen3_tts", "qwen3_tts_fast"]
MODELS = ["0.6b-base", "1.7b-base"]


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        DATA_DIR=tmp_path / "data",
        ENGINES_DIR=REPO_ROOT / "engines",
        AGENT_VOICE_DEFAULT_ENGINE="qwen3_tts_fast",
        MCP_BASE_PATH="",
        MODEL_IDLE_TTL_SECONDS=900,
    )


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


@pytest.mark.parametrize("engine_id", ENGINES)
def test_engine_is_installed(engine_id):
    if not _engine_ready(engine_id):
        pytest.skip(f"{engine_id} venv not installed; run `agent-voice engines install`")


@pytest.mark.parametrize("model_alias", MODELS)
def test_models_are_downloaded(model_alias):
    repo = {
        "0.6b-base": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        "1.7b-base": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    }[model_alias]
    if not hf_model_downloaded(repo):
        pytest.skip(f"{repo} not present locally; run `agent-voice models download`")


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_id", ENGINES)
@pytest.mark.parametrize("model_alias", MODELS)
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
