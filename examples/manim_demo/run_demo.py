"""End-to-end narrated-video pipeline: agent-voice → .timing.json → Manim → ffmpeg.

Run it from the repository root:

    uv run python examples/manim_demo/run_demo.py            # real engine + voice
    uv run python examples/manim_demo/run_demo.py --engine mock

What it does, in order:
  1. asks agent-voice for a two-beat script (`speak_script`, pause_after_sec=0.35)
  2. reads the returned `.timing.json`
  3. writes `timings.json` beside the scene so Manim can time its animations
  4. renders the scene with `manim`
  5. muxes the narration onto the silent video with `ffmpeg`
  6. reports the audio duration, video duration, and the drift between them

Steps 4 and 5 are skipped with a clear message when `manim` is not installed, so
this stays runnable in a lean environment.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

BEATS = [
    "A circle means the set of everything inside it.",
    "A square means a fixed boundary that never moves.",
]

PAUSE_AFTER_SEC = 0.35
"""The same call-level default speak_script() applies to beats without their own."""


# The example runs from the repo without an install, so make the package
# importable before anything touches it.
sys.path.insert(0, str(REPO_ROOT / "src"))


def render_narration(engine: str | None, quality: str) -> dict:
    """Call speak_script in-process and return the parsed .timing.json."""
    import asyncio

    from agent_voice.api.app import AppContext
    from agent_voice.config import get_settings
    from agent_voice.schemas import BeatSpec, ScriptRequest
    from agent_voice.services.script_service import render_script

    base = get_settings()
    # A copy, not a fresh Settings(): the latter would silently fall back to the
    # class defaults (engine=mock, no default voice) and ignore .env entirely.
    settings = base.model_copy(update={"default_engine": engine} if engine else {})
    settings.ensure_dirs()
    ctx = AppContext(settings)

    payload = ScriptRequest(
        beats=[
            BeatSpec(text=text, pause_after_sec=PAUSE_AFTER_SEC) for text in BEATS
        ],
        quality=quality,  # type: ignore[arg-type]
        format="wav",
        export_srt=True,
    )

    async def run():
        await ctx.workers.start()
        try:
            return await render_script(
                ctx,
                payload,
                on_beat=lambda done, total, message: print(f"  {message}"),
            )
        finally:
            await ctx.workers.stop()

    result = asyncio.run(run())
    timing_path = Path(result.timing_json_path or "")
    if not timing_path.is_file():
        raise SystemExit("agent-voice did not write a .timing.json")
    return json.loads(timing_path.read_text("utf-8"))


def write_scene_timings(timing: dict) -> Path:
    target = HERE / "timings.json"
    target.write_text(
        json.dumps({"beats": timing["beats"]}, indent=2, ensure_ascii=False), "utf-8"
    )
    return target


def _find_manim() -> list[str] | None:
    """Locate the manim entry point, preferring the interpreter running us.

    `shutil.which("manim")` misses the case this example is usually run from: a
    venv that is not activated, where the script sits beside `sys.executable` but
    is not on PATH. Falling back to `python -m manim` covers the rest.
    """
    sibling = Path(sys.executable).with_name("manim")
    if sibling.is_file():
        return [str(sibling)]
    found = shutil.which("manim")
    if found is not None:
        return [found]
    try:
        import manim  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "manim"]


def render_video() -> Path | None:
    """Run manim. Returns the produced mp4, or None when manim is absent."""
    manim = _find_manim()
    if manim is None:
        print("! manim is not installed - skipping the video render")
        print("  install it with: uv pip install -e '.[demo]'")
        return None

    media_dir = HERE / "media"
    command = [
        *manim,
        "-ql",  # low quality: fast, and enough to prove the pipeline
        "--media_dir",
        str(media_dir),
        "--disable_caching",
        str(HERE / "demo_scene.py"),
        "NarratedBeats",
    ]
    print("$", " ".join(command))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stdout[-2000:])
        print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"manim failed with exit code {completed.returncode}")

    videos = sorted(media_dir.rglob("NarratedBeats.mp4"))
    if not videos:
        raise SystemExit("manim reported success but produced no NarratedBeats.mp4")
    return videos[-1]


def mux(video: Path, audio: Path, output: Path) -> Path:
    """Replace the video's (absent) audio with the narration, without re-encoding video."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is required to mux the narration")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(output),
    ]
    print("$", " ".join(command))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        print(completed.stderr[-2000:], file=sys.stderr)
        raise SystemExit("ffmpeg mux failed")
    return output


def probe_duration(path: Path) -> float:
    from agent_voice.audio_utils import probe_duration as _probe

    return _probe(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        default=None,
        help="Engine id (default: whatever .env configures; 'mock' needs no GPU)",
    )
    parser.add_argument("--quality", default="high", choices=["high", "fast"])
    args = parser.parse_args()

    print(f"1/5  narrating {len(BEATS)} beats ...")
    timing = render_narration(args.engine, args.quality)
    audio_path = Path(timing["file_path"])
    audio_seconds = float(timing["duration_sec"])

    print(f"     audio: {audio_path}")
    print(f"     duration: {audio_seconds:.3f} s across {len(timing['beats'])} beats")
    for beat in timing["beats"]:
        print(
            f"       beat {beat['index']}: {beat['start_sec']:.3f} -> "
            f"{beat['end_sec']:.3f}  {beat['text'][:40]!r}"
        )

    print("2/5  writing scene timings ...")
    timings_file = write_scene_timings(timing)
    print(f"     {timings_file}")

    print("3/5  rendering with manim ...")
    video = render_video()
    if video is None:
        print("\nnarration is ready; install the demo extra to render video.")
        print(f"  audio: {audio_path}")
        return 0

    print("4/5  muxing narration onto the video ...")
    output = REPO_ROOT / "examples" / "manim_demo" / "out" / "narrated_demo.mp4"
    mux(video, Path(audio_path).resolve(), output)

    print("5/5  verifying ...")
    video_seconds = probe_duration(output)
    drift = abs(video_seconds - audio_seconds)
    print(f"     output: {output}")
    print(f"     video duration {video_seconds:.3f} s  vs audio {audio_seconds:.3f} s")
    print(f"     drift: {drift * 1000:.0f} ms")
    if drift > 0.75:
        print(
            "     ! drift above 750 ms: the scene timing and the narration "
            "disagree, check timings.json",
            file=sys.stderr,
        )
        return 1
    print("\nOK: narrated video rendered and muxed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
