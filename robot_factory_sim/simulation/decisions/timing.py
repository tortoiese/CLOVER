"""Charge control phases before side effects, including interrupted phases."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Generator
from typing import Any, TypeVar

T = TypeVar("T")


class DelayLedger:
    """Measure exclusive calls and reserve publication cost before applying state.

    Args:
        env: SimPy-compatible event environment owned by the plant.
        allowance: Positive publication reserve in simulation seconds.
        clock: Monotonic wall clock; deterministic tests inject a controlled clock.
        replay: Optional exact original phase receipts, never new wall measurements.
    """

    def __init__(
        self,
        env: Any,
        allowance: float,
        clock: Callable[[], float] = time.monotonic,
        replay: list[dict[str, Any]] | None = None,
    ) -> None:
        if not math.isfinite(allowance) or allowance <= 0:
            raise ValueError("publication allowance must be positive and finite")
        self.env, self.allowance, self.clock, self.replay = (
            env,
            allowance,
            clock,
            replay,
        )
        self.records: list[dict[str, Any]] = []
        self.coverage_ok = True
        self.pending: dict[str, Any] = {}

    def _record(self, name: str, elapsed: float, delay: float, at: float) -> dict[str, Any]:
        if not math.isfinite(elapsed) or elapsed < 0 or not math.isfinite(delay) or delay < 0:
            raise ValueError("non-monotonic or invalid recorded decision timing")
        row = {
            "name": name,
            "wall_seconds": elapsed,
            "injected_seconds": delay,
            "at": at,
        }
        if self.replay is not None:
            index = len(self.records)
            if index >= len(self.replay) or self.replay[index]["name"] != name:
                raise ValueError("decision timing replay phase mismatch")
            expected = self.replay[index]
            if expected["at"] != at:
                raise ValueError("decision timing replay time mismatch")
            for key in ("wall_seconds", "injected_seconds"):
                value = expected[key]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value < 0
                ):
                    raise ValueError("invalid replay phase duration")
            row = dict(expected)
        self.records.append(row)
        return row

    def call_process(self, name: str, function: Callable[[], T]) -> Generator[Any, Any, T]:
        """Delay a return (including an exception) by its measured control cost."""
        started, at = self.clock(), self.env.now
        error, value = None, None
        try:
            value = function()
        except Exception as exc:
            error = exc
        elapsed = self.clock() - started
        row = self._record(name, elapsed, elapsed, at)
        self.pending.update(name=name, at=at, due=at + row["injected_seconds"])
        yield self.env.timeout(row["injected_seconds"])
        self.pending.clear()
        if error is not None:
            raise error
        return value

    def publish_process(self, function: Callable[[], T]) -> Generator[Any, Any, T]:
        """Reserve latency first, then atomically check current state and publish."""
        at = self.env.now
        self.pending.update(name="publication", at=at, due=at + self.allowance)
        yield self.env.timeout(self.allowance)
        started = self.clock()
        error, value = None, None
        try:
            value = function()
        except Exception as exc:
            error = exc
        elapsed = self.clock() - started
        row = self._record("publication", elapsed, self.allowance, at)
        self.pending.clear()
        if row["injected_seconds"] != self.allowance:
            raise ValueError("publication replay reserve differs from settings")
        self.coverage_ok &= row["wall_seconds"] <= self.allowance
        if error is not None:
            raise error
        return value

    def verify_consumed(self) -> None:
        """Reject replay that quietly ignores a trailing original control phase."""
        if self.replay is not None and len(self.records) != len(self.replay):
            raise ValueError("unconsumed decision timing replay phases")
