"""Dubbing pipeline: ``prepare_dub`` -> (caller translates) -> ``render_dub``.

The whole suite runs on real ffmpeg media with the ``mock_asr`` listener and the
``mock`` speaker, so nothing here needs a GPU, a model download or torch. Every
test builds a genuine h264/aac MP4 with lavfi sources and probes the deliverable
with ffprobe, because the dubbing path's failure modes live in the container
work (extraction, placement, mux) rather than in Python.

Two things about the fixtures are load-bearing:

* ``asr_engine="mock_asr"`` is passed explicitly by ``_prepare``. The suite must
  not depend on ``AGENT_VOICE_DEFAULT_ASR_ENGINE`` being set: a host that has not
  configured it defaults to ``phonon``, which is a real model.
* ``render_dub`` deletes ``<dubs>/<dub_id>/`` on success. The retry scenarios
  ("render, fix one line, render again") therefore snapshot the plan first, and
  one test asserts the deletion itself so the snapshot stays honest.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from agent_voice.api.app import AppContext, create_app
from agent_voice.audio_utils import probe_media
from agent_voice.config import Settings
from agent_voice.schemas import (
    AgentVoiceError,
    DubPrepareRequest,
    DubPrepareResult,
    DubRenderRequest,
    DubRenderResult,
    DubSegmentSpec,
    ErrorCode,
    TranscribeSegment,
)
from agent_voice.security import MAX_REF_AUDIO_SECONDS, MIN_REF_AUDIO_SECONDS
from agent_voice.services.dub_service import PROJECT_FILE, prepare_dub, render_dub

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="dubbing is an ffmpeg pipeline: both ffmpeg and ffprobe are required",
)

ASR_ENGINE = "mock_asr"
"""A dependency-free, deterministic ASR plugin: the dub side of ``kind: asr``."""

SOURCE_SECONDS = 6
"""The short source: long enough for a timeline, short enough to render fast."""

LONG_SOURCE_SECONDS = 20
"""A source whose transcript spans 5-10 s, so a speaker reference can be cut.

``_speaker_window`` only accepts a 5-10 s window inside the file's 5 % edges. A
6 s clip is tiled by ``mock_asr`` into two ~3 s segments, both of which touch an
edge, so a short source yields *no* reference at all (covered explicitly below).
"""

SLOT_SLACK_SEC = 0.25
"""The same slack ``_validate_segments`` allows, so a source segment that ends a
few milliseconds past the container duration can be rendered back unchanged."""

SAMPLE_RATE = 24_000

NUMBER_WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _synthesise_media(path: Path, *, seconds: int, audio: bool = True) -> Path:
    """Build a real MP4 with lavfi sources. An argv list, never a shell string."""
    argv = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=navy:s=320x240:d={seconds}",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        argv += ["-c:a", "aac", "-shortest"]
    argv.append(str(path))
    subprocess.run(argv, shell=False, check=True, capture_output=True)
    return path


@pytest.fixture
def synth_video(tmp_path: Path) -> Path:
    """6 s, 320x240, h264 + aac sine: the standard dubbing source."""
    return _synthesise_media(tmp_path / "source-6s.mp4", seconds=SOURCE_SECONDS)


@pytest.fixture
def long_video(tmp_path: Path) -> Path:
    """20 s source: the shortest clip whose transcript yields a speaker reference."""
    return _synthesise_media(tmp_path / "source-20s.mp4", seconds=LONG_SOURCE_SECONDS)


@pytest.fixture
def silent_video(tmp_path: Path) -> Path:
    """A video with no audio stream: dubbing has nothing to transcribe."""
    return _synthesise_media(tmp_path / "silent.mp4", seconds=SOURCE_SECONDS, audio=False)


@contextlib.asynccontextmanager
async def _started_ctx(settings: Settings) -> AsyncIterator[AppContext]:
    """An AppContext with a started worker manager, torn down whatever happens."""
    ctx = AppContext(settings)
    await ctx.workers.start()
    try:
        yield ctx
    finally:
        await ctx.workers.stop()


@pytest.fixture
async def app_ctx(settings: Settings) -> AsyncIterator[AppContext]:
    """The real service wiring (registry, voices, workers) on the isolated DATA_DIR."""
    async with _started_ctx(settings) as ctx:
        yield ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _prepare(
    ctx: AppContext, media: Path, **overrides: object
) -> DubPrepareResult:
    request = DubPrepareRequest(media_path=str(media), asr_engine=ASR_ENGINE, **overrides)
    return await prepare_dub(ctx, request)


async def _render(
    ctx: AppContext,
    dub_id: str,
    segments: list[DubSegmentSpec],
    **overrides: object,
) -> DubRenderResult:
    options: dict[str, object] = {"engine": "mock", "quality": "fast"}
    options.update(overrides)
    return await render_dub(ctx, DubRenderRequest(dub_id=dub_id, segments=segments, **options))


def _translated(prepared: DubPrepareResult, *, prefix: str = "Mock line") -> list[DubSegmentSpec]:
    """Fake translations: one short English line per source segment, same slot."""
    return [
        DubSegmentSpec(
            index=segment.index,
            start_sec=segment.start_sec,
            end_sec=segment.end_sec,
            text=f"{prefix} {NUMBER_WORDS[index]}.",
        )
        for index, segment in enumerate(prepared.segments)
    ]


def _spec(index: int, start_sec: float, end_sec: float, text: str = "A line.") -> DubSegmentSpec:
    return DubSegmentSpec(index=index, start_sec=start_sec, end_sec=end_sec, text=text)


def _srt_cues(text: str) -> list[tuple[str, str, str]]:
    """``(start, end, text)`` per cue, as written by ``audio_utils.write_srt``."""
    cues: list[tuple[str, str, str]] = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        start, _, end = lines[1].partition(" --> ")
        cues.append((start, end, "\n".join(lines[2:]).strip()))
    return cues


def _has_dub_filters() -> bool:
    """Whether this ffmpeg build can actually duck the bed."""
    binary = shutil.which("ffmpeg")
    if binary is None:
        return False
    result = subprocess.run(
        [binary, "-hide_banner", "-filters"],
        shell=False,
        capture_output=True,
        check=False,
    )
    listing = (result.stdout or b"").decode("utf-8", "replace")
    return all(name in listing for name in ("sidechaincompress", "alimiter"))


def _srt_timestamp(seconds: float) -> str:
    """Mirror of ``audio_utils._timestamp`` so a slot can be compared exactly."""
    total_ms = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


class _PlanSnapshot:
    """A copy of ``<dubs>/<dub_id>/`` so a second render can be attempted.

    ``render_dub`` removes the working directory once it succeeds, so the
    documented "fix the overflowing line and render again" retry has no plan left
    to load. Restoring the snapshot reconstructs exactly the state a caller has
    after a *failed* render, which is the state the per-line cache claim is about.
    ``test_a_successful_render_deletes_the_plan`` pins the deletion itself.
    """

    def __init__(self, settings: Settings, dub_id: str, root: Path) -> None:
        self.work = settings.dubs_dir / dub_id
        self.copy = root / f"plan-{dub_id}"
        assert (self.work / PROJECT_FILE).is_file(), "nothing to snapshot"
        shutil.copytree(self.work, self.copy)

    def restore(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.copytree(self.copy, self.work)


# ---------------------------------------------------------------------------
# 1. prepare_dub
# ---------------------------------------------------------------------------


async def test_prepare_dub_transcribes_a_video_and_persists_the_plan(
    app_ctx: AppContext, synth_video: Path
) -> None:
    """Regression: stage 1 must return the timed source segments *and* a plan
    on disk, because the render may run hours later in another process and has
    no way to re-run ASR."""
    prepared = await _prepare(app_ctx, synth_video)

    assert re.fullmatch(r"[0-9a-f]{16}", prepared.dub_id)
    assert prepared.duration_sec == pytest.approx(SOURCE_SECONDS, abs=0.2)
    assert prepared.engine_id == ASR_ENGINE
    assert prepared.model_alias
    assert prepared.segments, "a dub with no source segments has nothing to translate"

    cursor = 0.0
    for segment in prepared.segments:
        assert segment.text.strip()
        assert segment.start_sec < segment.end_sec
        # Monotonic and tiling: a gap here would leave a line of dialogue with no
        # slot to be spoken into.
        assert segment.start_sec >= cursor - 1e-9
        assert segment.end_sec <= prepared.duration_sec + SLOT_SLACK_SEC
        cursor = segment.end_sec

    project_path = Path(prepared.project_json_path)
    assert project_path.name == PROJECT_FILE
    assert project_path.is_file()
    plan = json.loads(project_path.read_text("utf-8"))
    assert plan["dub_id"] == prepared.dub_id
    assert plan["engine_id"] == ASR_ENGINE
    assert plan["media_path"] == prepared.media_path
    assert plan["duration_sec"] == pytest.approx(prepared.duration_sec, abs=1e-3)
    assert plan["segments"] == [segment.model_dump() for segment in prepared.segments]


async def test_prepare_dub_speaker_reference_is_accepted_by_the_voice_store(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: the reference clip fed to ``auto_clone_source`` must satisfy the
    same bounds ``VoiceStore.add`` enforces, or every auto-clone render dies at the
    clone step after the transcription has already been paid for."""
    prepared = await _prepare(app_ctx, long_video)

    assert prepared.speaker_ref_path is not None
    reference = Path(prepared.speaker_ref_path)
    assert reference.is_file()
    info = sf.info(str(reference))
    # Sliced straight out of asr.wav, so it keeps that file's shape on purpose.
    assert info.samplerate == 16_000
    assert info.channels == 1
    assert MIN_REF_AUDIO_SECONDS <= info.duration <= MAX_REF_AUDIO_SECONDS
    assert (MIN_REF_AUDIO_SECONDS, MAX_REF_AUDIO_SECONDS) == (1.0, 30.0)

    assert prepared.speaker_ref_text.strip()
    transcript = " ".join(segment.text for segment in prepared.segments)
    assert prepared.speaker_ref_text in transcript

    # The claim in the docstring, proven rather than assumed.
    metadata = app_ctx.voices.add(
        name="dub reference probe",
        source_path=reference,
        ref_text=prepared.speaker_ref_text,
    )
    assert metadata.duration_sec >= MIN_REF_AUDIO_SECONDS
    assert metadata.ref_text == prepared.speaker_ref_text


async def test_prepare_dub_can_skip_the_speaker_reference(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: ``extract_speaker_ref=False`` must cost no ffmpeg cut and must
    report the absence as null/empty rather than as a stale path."""
    prepared = await _prepare(app_ctx, long_video, extract_speaker_ref=False)

    assert prepared.speaker_ref_path is None
    assert prepared.speaker_ref_text == ""
    assert not (app_ctx.settings.dubs_dir / prepared.dub_id / "speaker_ref.wav").exists()
    assert prepared.segments, "skipping the reference must not change the transcript"


async def test_a_six_second_source_yields_no_speaker_reference(
    app_ctx: AppContext, synth_video: Path
) -> None:
    """Regression guard for the 5-10 s reference band.

    The window search excludes the file's first and last 5 % and requires a 5-10 s
    span. A 6 s clip is tiled into two ~3 s segments that both touch an edge, so
    there is no window at all; prepare must say so instead of registering a 3 s
    clone (which clones badly) or failing the whole prepare.
    """
    prepared = await _prepare(app_ctx, synth_video)

    assert prepared.speaker_ref_path is None
    assert prepared.speaker_ref_text == ""
    assert app_ctx.voices.list() == []
    # The transcript is still complete: this is a degraded reference, not a failure.
    assert prepared.segments


async def test_prepare_dub_refuses_media_without_an_audio_stream(
    app_ctx: AppContext, silent_video: Path
) -> None:
    """Regression: a silent screen recording must be refused by the probe, before
    any extraction and before a worker exists to transcribe nothing."""
    with pytest.raises(AgentVoiceError) as caught:
        await _prepare(app_ctx, silent_video)

    assert caught.value.error_code == ErrorCode.BAD_REQUEST
    assert "no audio stream" in caught.value.message
    assert silent_video.name in caught.value.message
    assert app_ctx.workers.spawn_count == 0
    # A refused request must not leave a half-built dub directory behind.
    assert list(app_ctx.settings.dubs_dir.iterdir()) == []


async def test_prepare_dub_refuses_media_over_the_transcribe_cap(
    settings: Settings, synth_video: Path
) -> None:
    """Regression: MAX_TRANSCRIBE_SECONDS is a cap on work, checked against the
    probe result — an over-long clip costs one ffprobe and no worker spawn."""
    capped = settings.model_copy(update={"max_transcribe_seconds": 0.5})
    async with _started_ctx(capped) as ctx:
        with pytest.raises(AgentVoiceError) as caught:
            await _prepare(ctx, synth_video)

        assert caught.value.error_code == ErrorCode.BAD_REQUEST
        assert "limit is" in caught.value.message
        assert f"{float(SOURCE_SECONDS):.1f}s" in caught.value.message
        assert ctx.workers.spawn_count == 0
        assert list(capped.dubs_dir.iterdir()) == []


async def test_render_refuses_a_traversal_dub_id_without_reading_it(
    app_ctx: AppContext, synth_video: Path, tmp_path: Path
) -> None:
    """Regression: ``dub_id`` becomes a directory component, so a traversal must be
    pattern-checked before the path is joined and read.

    The bait is real: ``<DATA_DIR>/dubs/../../etc/project.json`` is written with a
    loadable plan, so a service that joined the id and read the file would sail
    past admission instead of refusing the request.
    """
    escape_target = tmp_path / "etc"
    escape_target.mkdir(parents=True, exist_ok=True)
    bait = escape_target / PROJECT_FILE
    bait.write_text(
        json.dumps(
            {
                "dub_id": "../../etc",
                "media_path": str(synth_video),
                "duration_sec": float(SOURCE_SECONDS),
                "segments": [
                    TranscribeSegment(
                        index=0, start_sec=0.0, end_sec=float(SOURCE_SECONDS), text="bait"
                    ).model_dump()
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AgentVoiceError) as caught:
        await _render(app_ctx, "../../etc", [_spec(0, 0.0, 1.0)])

    assert caught.value.error_code == ErrorCode.BAD_REQUEST
    assert caught.value.message.startswith("unknown dub_id")
    assert app_ctx.workers.spawn_count == 0
    # Untouched: nothing was read, and nothing outside the dubs tree was written.
    assert json.loads(bait.read_text("utf-8"))["segments"][0]["text"] == "bait"
    assert list(tmp_path.glob("*.mp4")) == [synth_video]

    # A well-formed but unknown id gets the same answer.
    with pytest.raises(AgentVoiceError) as unknown:
        await _render(app_ctx, "0123456789abcdef", [_spec(0, 0.0, 1.0)])
    assert unknown.value.error_code == ErrorCode.BAD_REQUEST


# ---------------------------------------------------------------------------
# 2. render_dub, end to end
# ---------------------------------------------------------------------------


async def test_render_dub_places_translated_lines_into_the_source_video(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: a dub replaces the audio and leaves the picture alone. The
    deliverable must be a playable MP4 with both streams, at the source's length,
    and the video stream must be remuxed rather than re-encoded."""
    prepared = await _prepare(app_ctx, long_video)
    segments = _translated(prepared)

    result = await _render(app_ctx, prepared.dub_id, segments, auto_clone_source=True)

    final = Path(result.file_path)
    assert final == app_ctx.settings.artifacts_dir / f"dub_{prepared.dub_id}.mp4"
    assert final.is_file() and final.stat().st_size > 0

    info = probe_media(final)
    assert info.has_video, "the dubbed deliverable lost its picture"
    assert info.has_audio, "the dubbed deliverable has no audio"
    assert info.sample_rate == SAMPLE_RATE
    assert info.duration_sec == pytest.approx(prepared.duration_sec, abs=SLOT_SLACK_SEC)

    assert result.dub_id == prepared.dub_id
    assert result.video_stream_copied is True
    assert result.duration_sec == pytest.approx(prepared.duration_sec, abs=SLOT_SLACK_SEC)
    assert result.segments_count == len(segments)
    assert result.overflow_warnings == [], "every line fits, so nothing may overflow"
    assert result.engine_id == "mock"
    assert result.bed_ducked is _has_dub_filters()
    assert result.timing_json_path and Path(result.timing_json_path).is_file()

    # auto_clone_source registered the source speaker under a deterministic name.
    voice = app_ctx.voices.find_by_name(f"dub-{prepared.dub_id}")
    assert voice is not None

    # The working directory is consumed by a successful render: the bed, asr.wav
    # and the per-line WAVs have all been muxed into the deliverable.
    assert not (app_ctx.settings.dubs_dir / prepared.dub_id).exists()


async def test_auto_clone_source_reuses_its_voice_on_a_second_render(
    app_ctx: AppContext, long_video: Path, tmp_path: Path
) -> None:
    """Regression: a retried render must not register a second copy of the source
    voice. The name ``dub-<dub_id>`` is the idempotency key, and a duplicate would
    both leak a voice and fork the synthesis cache."""
    prepared = await _prepare(app_ctx, long_video)
    snapshot = _PlanSnapshot(app_ctx.settings, prepared.dub_id, tmp_path)
    segments = _translated(prepared)

    first = await _render(app_ctx, prepared.dub_id, segments, auto_clone_source=True)
    voices_after_first = app_ctx.voices.list()
    assert len(voices_after_first) == 1
    voice = app_ctx.voices.find_by_name(f"dub-{prepared.dub_id}")
    assert voice is not None

    snapshot.restore()
    second = await _render(app_ctx, prepared.dub_id, segments, auto_clone_source=True)

    assert len(app_ctx.voices.list()) == len(voices_after_first)
    reused = app_ctx.voices.find_by_name(f"dub-{prepared.dub_id}")
    assert reused is not None and reused.voice_id == voice.voice_id
    assert second.file_path == first.file_path
    # Nothing changed, so nothing was re-synthesised.
    assert second.cached_segments == len(segments)


async def test_editing_one_line_reuses_every_other_line_from_the_cache(
    app_ctx: AppContext, long_video: Path, tmp_path: Path
) -> None:
    """The headline v0.5 claim: a one-line fix must not re-synthesise the video.
    Editing exactly one translated line and rendering again must report every
    other line as a cache hit."""
    prepared = await _prepare(app_ctx, long_video)
    snapshot = _PlanSnapshot(app_ctx.settings, prepared.dub_id, tmp_path)
    segments = _translated(prepared)

    first = await _render(app_ctx, prepared.dub_id, segments, auto_clone_source=True)
    assert first.cached_segments == 0, "a first render of fresh text cannot hit the cache"

    snapshot.restore()
    edited = list(segments)
    middle = len(edited) // 2
    edited[middle] = edited[middle].model_copy(
        update={"text": "A freshly translated middle line."}
    )
    second = await _render(app_ctx, prepared.dub_id, edited, auto_clone_source=True)

    assert second.cached_segments == len(segments) - 1
    assert second.segments_count == len(segments)
    assert second.file_path == first.file_path
    assert second.overflow_warnings == []


async def test_a_successful_render_deletes_the_plan_and_a_rerun_needs_prepare(
    app_ctx: AppContext, synth_video: Path
) -> None:
    """Pins the retry contract the cache tests work around: a successful render
    removes ``dubs/<dub_id>``, so re-rendering the same id afterwards is refused
    and the caller has to call ``dub_prepare`` again."""
    prepared = await _prepare(app_ctx, synth_video)
    work = app_ctx.settings.dubs_dir / prepared.dub_id
    assert (work / PROJECT_FILE).is_file()

    await _render(app_ctx, prepared.dub_id, _translated(prepared))
    assert not work.exists()

    with pytest.raises(AgentVoiceError) as caught:
        await _render(app_ctx, prepared.dub_id, _translated(prepared))
    assert caught.value.error_code == ErrorCode.BAD_REQUEST
    assert caught.value.message.startswith("unknown dub_id")


async def test_render_without_the_original_bed_still_produces_playable_media(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: the bed is an enhancement, not a requirement. A render that
    replaces the source audio must still mux a playable file when the bed is
    skipped — and must not try to mix a file it was told to ignore."""
    prepared = await _prepare(app_ctx, long_video)

    result = await _render(
        app_ctx, prepared.dub_id, _translated(prepared), keep_original_bed=False
    )

    final = Path(result.file_path)
    assert final.is_file()
    info = probe_media(final)
    assert info.has_video
    assert info.has_audio
    assert info.duration_sec == pytest.approx(prepared.duration_sec, abs=SLOT_SLACK_SEC)
    assert info.channels in {1, 2}
    assert result.bed_ducked is False, "keep_original_bed=false must not report a duck"


async def test_burn_subtitles_re_encodes_the_video_stream(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: burned-in subtitles need a re-encode, so the result must say
    ``video_stream_copied=False`` and still be a valid MP4 with a picture.

    A caller who asked for pixels must never silently receive a clean video, and a
    host whose ffmpeg cannot burn is reported as a skip rather than a failure.
    """
    prepared = await _prepare(app_ctx, long_video)

    try:
        result = await _render(
            app_ctx, prepared.dub_id, _translated(prepared), burn_subtitles=True
        )
    except AgentVoiceError as exc:
        if "subtitles" not in exc.message.lower():
            raise
        pytest.skip(f"this ffmpeg build cannot burn subtitles: {exc.message}")

    assert result.video_stream_copied is False
    final = Path(result.file_path)
    assert final.is_file()
    info = probe_media(final)
    assert info.has_video
    assert info.has_audio
    assert info.duration_sec == pytest.approx(prepared.duration_sec, abs=SLOT_SLACK_SEC)


async def test_exported_subtitles_carry_the_translated_lines_on_the_source_slots(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: the audience reads what it hears. Cues must come from the
    *translated* segments, on the source slots — never from the source transcript."""
    prepared = await _prepare(app_ctx, long_video)
    segments = _translated(prepared)

    result = await _render(app_ctx, prepared.dub_id, segments)

    for path in (result.srt_path, result.vtt_path, result.timing_json_path):
        assert path is not None and Path(path).is_file()

    srt_text = Path(str(result.srt_path)).read_text("utf-8")
    cues = _srt_cues(srt_text)
    assert [cue[2] for cue in cues] == [segment.text for segment in segments]
    for (start, end, _), segment in zip(cues, segments, strict=True):
        assert start == _srt_timestamp(segment.start_sec)
        assert end == _srt_timestamp(segment.end_sec)
    for source in prepared.segments:
        assert source.text not in srt_text, "a source-language line leaked into the subtitles"

    vtt_text = Path(str(result.vtt_path)).read_text("utf-8")
    assert vtt_text.startswith("WEBVTT")
    assert all(segment.text in vtt_text for segment in segments)

    # The timing sidecar is the result: a caller that kept only the MP4 can still
    # recover what was cached and what overflowed.
    sidecar = json.loads(Path(str(result.timing_json_path)).read_text("utf-8"))
    assert sidecar["dub_id"] == prepared.dub_id
    assert sidecar["file_path"] == result.file_path
    assert sidecar["segments_count"] == len(segments)
    assert sidecar["overflow_warnings"] == []
    assert [entry["text"] for entry in sidecar["segments"]] == [
        segment.text for segment in segments
    ]
    assert [entry["start_sec"] for entry in sidecar["segments"]] == [
        segment.start_sec for segment in segments
    ]


# ---------------------------------------------------------------------------
# 3. Overflow accounting
# ---------------------------------------------------------------------------


async def test_an_over_long_line_is_reported_but_never_truncated(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: a translation that cannot fit its slot is sped up to
    ``max_atempo`` and then *reported*. Cutting the tail off produces confident
    nonsense; failing the render throws away every other line."""
    prepared = await _prepare(app_ctx, long_video)
    segments = [
        _spec(0, 0.0, 0.4, " ".join(["overflow"] * 60)),
        _spec(1, 0.4, 3.0, "A short second line."),
    ]

    result = await _render(app_ctx, prepared.dub_id, segments, max_atempo=1.35)

    assert len(result.overflow_warnings) == 1
    overflow = result.overflow_warnings[0]
    assert overflow.index == 0
    assert overflow.slot_sec == pytest.approx(0.4)
    assert overflow.audio_sec > overflow.slot_sec
    assert overflow.atempo == pytest.approx(1.35)

    # Reported, never fatal: the render succeeded and every line was placed.
    final = Path(result.file_path)
    assert final.is_file()
    info = probe_media(final)
    assert info.has_audio and info.has_video
    assert info.duration_sec == pytest.approx(prepared.duration_sec, abs=SLOT_SLACK_SEC)
    assert result.segments_count == 2
    # Not truncated to the 0.4 s slot: the line's full length is what was reported.
    assert overflow.audio_sec > 10 * overflow.slot_sec


async def test_a_line_that_fits_after_atempo_is_not_reported_as_overflow(
    app_ctx: AppContext, long_video: Path
) -> None:
    """Regression: ``overflow_warnings`` must mean "did not fit even at the
    ceiling". A line that a sub-ceiling speed-up fits is a success, and reporting
    it would train callers to ignore the field."""
    prepared = await _prepare(app_ctx, long_video)
    # The mock speaker renders ~words/2.6 s of audio: 4 words -> ~1.54 s in a
    # 1.0 s slot, a 1.54x speed-up that fits under a 1.6 ceiling.
    segments = [
        _spec(0, 0.0, 1.0, "one two three four"),
        _spec(1, 1.0, 5.0, "The rest of the line."),
    ]

    result = await _render(app_ctx, prepared.dub_id, segments, max_atempo=1.6)

    assert result.overflow_warnings == []
    assert result.segments_count == 2
    assert Path(result.file_path).is_file()


# ---------------------------------------------------------------------------
# 4. Admission on render
# ---------------------------------------------------------------------------


ADMISSION_CASES: dict[str, list[DubSegmentSpec]] = {
    "duplicate-index": [_spec(0, 0.0, 1.0), _spec(0, 1.0, 2.0)],
    "inverted-slot": [_spec(0, 2.0, 1.0)],
    "empty-slot": [_spec(0, 1.0, 1.0)],
    # Duration is 6 s, and the contract allows 0.25 s of slack: 6.5 s is out.
    "slot-past-the-media": [_spec(0, 0.0, float(SOURCE_SECONDS) + 0.5)],
    "blank-text": [_spec(0, 0.0, 1.0, "   ")],
    "no-segments": [],
}


@pytest.mark.parametrize("case", sorted(ADMISSION_CASES))
async def test_render_admission_refuses_an_impossible_timeline(
    app_ctx: AppContext, synth_video: Path, case: str
) -> None:
    """Regression: every slot is validated before the first synthesis. A bad slot
    found on line 90 of 120 would otherwise mean paying for 89 renders and
    throwing them away."""
    prepared = await _prepare(app_ctx, synth_video)
    # The prepare above spawned the ASR worker; the count is a baseline, not zero.
    spawns_before = app_ctx.workers.spawn_count

    with pytest.raises(AgentVoiceError) as caught:
        await _render(app_ctx, prepared.dub_id, ADMISSION_CASES[case])

    assert caught.value.error_code == ErrorCode.BAD_REQUEST
    assert app_ctx.workers.spawn_count == spawns_before, (
        "admission must fail before the TTS worker is spawned"
    )
    # A refused render keeps the plan, so the caller can fix the slots and retry
    # without paying for the extraction and the transcription again.
    assert (app_ctx.settings.dubs_dir / prepared.dub_id / PROJECT_FILE).is_file()


async def test_render_refuses_more_lines_than_max_dub_segments(
    settings: Settings, synth_video: Path
) -> None:
    """Regression: MAX_DUB_SEGMENTS is checked with the slots, before synthesis —
    an oversized request must cost a validation, not a batch of renders."""
    tight = settings.model_copy(update={"max_dub_segments": 2})
    async with _started_ctx(tight) as ctx:
        prepared = await _prepare(ctx, synth_video)
        spawns_before = ctx.workers.spawn_count
        too_many = [_spec(0, 0.0, 1.0), _spec(1, 1.0, 2.0), _spec(2, 2.0, 3.0)]

        with pytest.raises(AgentVoiceError) as caught:
            await _render(ctx, prepared.dub_id, too_many)

        assert caught.value.error_code == ErrorCode.BAD_REQUEST
        assert "limit is 2" in caught.value.message
        assert ctx.workers.spawn_count == spawns_before


# ---------------------------------------------------------------------------
# 5. REST and the job path
# ---------------------------------------------------------------------------


def _dub_request(responses_segments: list[dict], dub_id: str, **overrides: object) -> dict:
    """A translated render body built from a prepare response's ``segments``."""
    body: dict = {
        "dub_id": dub_id,
        "segments": [
            {
                "index": segment["index"],
                "start_sec": segment["start_sec"],
                "end_sec": segment["end_sec"],
                "text": f"Mock line {NUMBER_WORDS[index]}.",
            }
            for index, segment in enumerate(responses_segments)
        ],
        "engine": "mock",
        "quality": "fast",
    }
    body.update(overrides)
    return body


async def _await_job(client: TestClient, job_id: str, *, attempts: int = 120) -> dict:
    """Poll a job to a terminal state. Bounded: 120 * 50 ms is the hard ceiling."""
    record = client.get(f"/v1/jobs/{job_id}").json()
    for _ in range(attempts):
        if record["status"] in {"succeeded", "failed", "cancelled"}:
            break
        await asyncio.sleep(0.05)
        record = client.get(f"/v1/jobs/{job_id}").json()
    return record


async def test_rest_dub_prepare_render_and_job_round_trip(settings: Settings, synth_video: Path):
    """Regression: the three HTTP surfaces must drive the same pipeline as the
    service layer, and the job must land its result where it says it did."""
    app = create_app(settings=settings, mount_mcp=False)
    with TestClient(app) as client:
        prepared = client.post(
            "/v1/agent/dub/prepare",
            json={"media_path": str(synth_video), "asr_engine": ASR_ENGINE},
        )
        assert prepared.status_code == 200, prepared.text
        prepared_body = prepared.json()
        assert re.fullmatch(r"[0-9a-f]{16}", prepared_body["dub_id"])
        assert prepared_body["segments"], prepared_body
        assert Path(prepared_body["project_json_path"]).is_file()

        rendered = client.post(
            "/v1/agent/dub/render",
            json=_dub_request(prepared_body["segments"], prepared_body["dub_id"]),
        )
        assert rendered.status_code == 200, rendered.text
        rendered_body = rendered.json()
        assert Path(rendered_body["file_path"]).is_file()
        assert rendered_body["video_stream_copied"] is True
        assert rendered_body["segments_count"] == len(prepared_body["segments"])

        # A job needs its own prepared dub: the synchronous render consumed the plan.
        again = client.post(
            "/v1/agent/dub/prepare",
            json={"media_path": str(synth_video), "asr_engine": ASR_ENGINE},
        )
        assert again.status_code == 200, again.text
        again_body = again.json()

        started = client.post(
            "/v1/agent/dub/jobs",
            json=_dub_request(again_body["segments"], again_body["dub_id"]),
        )
        assert started.status_code == 200, started.text
        started_body = started.json()
        assert started_body["kind"] == "dub"
        assert started_body["job_id"]

        record = await _await_job(client, started_body["job_id"])

    assert record["status"] == "succeeded", record
    assert record["kind"] == "dub"
    assert record["progress"] == pytest.approx(1.0)
    assert record["result"]["dub_id"] == again_body["dub_id"]
    assert Path(record["result"]["file_path"]).is_file()


async def test_rest_dub_prepare_confines_media_to_the_inbox(
    settings: Settings, synth_video: Path
) -> None:
    """Regression: a dub reads a caller path, so it is confined exactly like a
    cloning reference — ``403 PATH_OUTSIDE_BASE`` before any ffmpeg probe."""
    strict = settings.model_copy(update={"allow_any_path": False})
    app = create_app(settings=strict, mount_mcp=False)
    with TestClient(app) as client:
        refused = client.post(
            "/v1/agent/dub/prepare",
            json={"media_path": str(synth_video), "asr_engine": ASR_ENGINE},
        )
        assert refused.status_code == 403, refused.text
        assert refused.json()["error_code"] == "PATH_OUTSIDE_BASE"
        assert "inbox" in refused.json()["suggestion"].lower()
        # Refused before any work: no dub directory was created.
        assert list(strict.dubs_dir.iterdir()) == []

        staged = strict.inbox_dir / "clip.mp4"
        shutil.copyfile(synth_video, staged)
        accepted = client.post(
            "/v1/agent/dub/prepare",
            json={"media_path": str(staged), "asr_engine": ASR_ENGINE},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["dub_id"]
