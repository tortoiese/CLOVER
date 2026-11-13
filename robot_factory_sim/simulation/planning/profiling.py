"""Exclusive stage clocks that survive failed model construction."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable


class PhaseTimer:
    """Keep completed/current native stages outside the fallible builder object."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self.clock = clock or time.perf_counter
        self.seconds: dict[str, float] = defaultdict(float)
        self.active = ""
        self.last = "not_started"
        self.since = self.clock()
        self.read_counts: Callable[[], dict[str, int]] | None = None

    def start(self, stage: str) -> None:
        """Close the prior stage and begin another without double counting."""
        now = self.clock()
        if self.active:
            self.seconds[self.active] += max(0.0, now - self.since)
        self.active, self.last, self.since = stage, stage, now

    def stop(self) -> None:
        """Finish the active stage, preserving its name as a failure location."""
        now = self.clock()
        if self.active:
            self.seconds[self.active] += max(0.0, now - self.since)
        self.active, self.since = "", now

    def snapshot(self) -> dict[str, float]:
        """Return only stages that actually started; missing does not mean zero."""
        values = dict(self.seconds)
        if self.active:
            values[self.active] = values.get(self.active, 0.0) + max(0.0, self.clock() - self.since)
        return values
