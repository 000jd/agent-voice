"""Injectable clock.

Production uses :class:`SystemClock`. Tests inject a fake clock so that the
15-minute idle reaper, engine residency hysteresis and job timeouts can be
exercised without sleeping.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float:
        """Wall-clock seconds since the epoch. Used for TTL bookkeeping."""

    def monotonic(self) -> float:
        """Monotonic seconds. Used for measuring durations and timeouts."""


class SystemClock:
    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


_default_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _default_clock


def set_clock(clock: Clock) -> None:
    """Test helper: swap the process-wide default clock."""
    global _default_clock
    _default_clock = clock
