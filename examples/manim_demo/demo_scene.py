"""A Manim scene whose two beats are timed by agent-voice narration.

The timings are not hard-coded: `run_demo.py` writes `timings.json` next to this
file after rendering the narration, and the scene reads it. That is the whole
point of the `.timing.json` contract - the visual follows the audio, not the
other way round.

Rendered by `run_demo.py`; also runnable directly once timings.json exists:

    manim -ql demo_scene.py NarratedBeats
"""

from __future__ import annotations

import json
from pathlib import Path

from manim import (
    BLUE,
    DOWN,
    GREEN,
    UP,
    Circle,
    FadeIn,
    FadeOut,
    Scene,
    Square,
    Text,
    VGroup,
)

TIMINGS_PATH = Path(__file__).with_name("timings.json")
SAMPLE_RATE = 24_000


def load_timings() -> list[dict]:
    """Read the beat layout written by run_demo.py.

    A missing file yields a short fallback so the scene still renders (useful for
    a first `manim` smoke run before any audio exists).
    """
    if not TIMINGS_PATH.is_file():
        return [
            {"text": "beat one", "start_sec": 0.0, "end_sec": 2.0},
            {"text": "beat two", "start_sec": 2.35, "end_sec": 4.35},
        ]
    data = json.loads(TIMINGS_PATH.read_text("utf-8"))
    return data["beats"]


class NarratedBeats(Scene):
    """Two beats, each animation spanning exactly its narrated span."""

    def construct(self) -> None:
        beats = load_timings()

        for index, beat in enumerate(beats):
            # run() advances the scene clock, so the next beat starts exactly when
            # the narration for it starts. The gap between end_sec and the next
            # start_sec is the pause the script asked for, and it plays as a held
            # frame rather than being collapsed.
            if index > 0:
                previous_end = beats[index - 1]["end_sec"]
                gap = max(0.0, beat["start_sec"] - previous_end)
                if gap > 0:
                    self.wait(gap)

            colour = BLUE if index % 2 == 0 else GREEN
            shape = Circle(radius=1.0, color=colour) if index % 2 == 0 else Square(color=colour)
            label = Text(beat["text"][:48], font_size=34).next_to(shape, DOWN, buff=0.4)
            group = VGroup(shape, label)

            span = max(0.1, float(beat["end_sec"]) - float(beat["start_sec"]))
            # FadeIn for the first third, hold, FadeOut for the last third: the
            # total is exactly the narrated span.
            fade = min(0.4, span / 3)
            self.play(FadeIn(group, shift=UP * 0.3), run_time=fade)
            self.wait(max(0.0, span - 2 * fade))
            self.play(FadeOut(group), run_time=fade)

        # A trailing hold so the final syllable is not clipped by the video end.
        self.wait(0.4)
