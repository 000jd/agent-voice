"""Injectable fake clock for tests.

Never imported by production code. Lets tests jump the 15-minute idle reaper,
engine residency hysteresis and job timeouts forward instantly.
"""

from __future__ import annotations


class FakeClock:
    """Deterministic clock. Time only moves when a test moves it."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = start
        self._monotonic = 0.0

    def now(self) -> float:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        self._now += seconds
        self._monotonic += seconds

    def set(self, epoch_seconds: float) -> None:
        delta = epoch_seconds - self._now
        self.advance(delta)
