"""Append-only observable events; callers may stream them without bounded sampling."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class EventLedger:
    """Emit ordered, detached rows without retaining an unbounded event list."""

    def __init__(self, now: Callable[[], float], sink: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.now = now
        self.sink = sink
        self.sequence = 0
        self.counts: Counter[str] = Counter()
        self._buffer: list[dict[str, Any]] | None = None

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Publish staged events only after atomic queue/robot admission succeeds."""
        if self._buffer is not None:
            raise RuntimeError("nested ledger transaction")
        sequence, counts = self.sequence, self.counts.copy()
        self._buffer = []
        try:
            yield
        except BaseException:
            self.sequence, self.counts, self._buffer = sequence, counts, None
            raise
        rows, self._buffer = self._buffer, None
        if self.sink is not None:
            for row in rows:
                self.sink(row)

    def emit(self, event_name: str, **values: Any) -> None:
        """Write one immutable-by-convention JSON-compatible event row."""
        row = {"sequence": self.sequence, "time": self.now(), "event": event_name, **values}
        self.sequence += 1
        self.counts[event_name] += 1
        if self._buffer is not None:
            self._buffer.append(row)
        elif self.sink is not None:
            self.sink(row)
