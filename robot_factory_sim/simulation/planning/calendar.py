"""Owner-aware capacity calendar; unknown releases remain blocked indefinitely."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass

from simulation.planning.contracts import PreparedInput, ResourceHold


@dataclass(frozen=True)
class CapacityCalendar:
    """Piecewise constant residual budgets on conservative half-open grid ticks."""

    capacities: dict[str, tuple[str, int]]
    segments: dict[str, tuple[tuple[int, int | None, int], ...]]

    def at(self, resource: str, tick: int) -> int:
        """Return usable capacity after observed and committed obligations."""
        values = self.segments[resource]
        index = bisect_right(values, tick, key=lambda row: row[0]) - 1
        return values[max(0, index)][2]

    def blocked(self, resource: str, end: int) -> tuple[tuple[int, int, int], ...]:
        """Represent reduced capacity as fixed demands for a CP cumulative."""
        capacity = self.capacities[resource][1]
        return tuple(
            (lo, min(hi, end) if hi is not None else end, capacity - residual)
            for lo, hi, residual in self.segments[resource]
            if lo < end and residual < capacity
        )


def build_calendar(data: PreparedInput) -> CapacityCalendar:
    """Merge actual/prefix claims of one owner by maximum, never double count.

    Unknown external occupancy is recovered from aggregate counts. Existing
    over-capacity drain is clipped to zero residual, not declared infeasible.
    """
    snapshot = data.snapshot
    capacities = {r.id: (r.unit, r.capacity) for r in snapshot.resources}
    observed = {r.id: r.occupied for r in snapshot.resources}
    for f in snapshot.facilities:
        capacities[f"{f.kind}:{f.id}"] = "slot", f.capacity if f.active else 0
        observed[f"{f.kind}:{f.id}"] = f.occupied
    for c in snapshot.cabins:
        capacities["cabin:" + c.id] = "slot", int(c.active)
        observed["cabin:" + c.id] = max(c.occupied, int(c.moving or c.waiting > 0))
    holds = list(snapshot.holds)
    actual = Counter()
    queued = Counter(h.resource for h in holds if h.source == "future")
    for hold in holds:
        if hold.source == "actual":
            actual[hold.resource] += hold.amount
    for resource, amount in observed.items():
        if amount > actual[resource]:
            holds.append(
                ResourceHold(
                    "opaque:" + resource,
                    resource,
                    amount - actual[resource],
                    capacities[resource][0],
                    snapshot.now,
                    None,
                    "actual",
                )
            )
    for f in snapshot.facilities:
        resource = f.kind + ":" + f.id
        missing = max(0, f.waiting - queued[resource])
        if missing:
            holds.append(
                ResourceHold("opaque-queue:" + resource, resource, missing, "slot", snapshot.now, None, "future")
            )
    events: dict[str, dict[int, list[tuple[str, int, int, bool]]]] = defaultdict(lambda: defaultdict(list))
    step = snapshot.settings.time_unit
    for hold in holds:
        if (
            hold.resource not in capacities
            or hold.unit != capacities[hold.resource][0]
            or type(hold.amount) is not int
            or hold.amount <= 0
            or hold.source not in {"actual", "prefix", "future"}
            or not math.isfinite(hold.start)
            or (hold.end is not None and (not math.isfinite(hold.end) or hold.end < hold.start))
        ):
            raise ValueError("MODEL_INVALID: invalid calendar hold")
        lo = max(0, math.floor((hold.start - snapshot.now) / step + 1e-9))
        hi = max(0, math.ceil((hold.end - snapshot.now) / step - 1e-9)) if hold.end is not None else None
        if hi is not None and hi <= lo:
            continue
        events[hold.resource][lo].append((hold.owner, hold.amount, 1, hold.source == "actual"))
        if hi is not None:
            events[hold.resource][hi].append((hold.owner, hold.amount, -1, hold.source == "actual"))
    result = {}
    for resource, (_, capacity) in capacities.items():
        changes = events[resource]
        points = sorted({0, *changes})
        owners: dict[str, Counter[int]] = defaultdict(Counter)
        actual_load: Counter[str] = Counter()
        load, segments = 0, []
        for i, tick in enumerate(points):
            for owner, amount, delta, is_actual in changes[tick]:
                before = max(actual_load[owner], max(owners[owner], default=0))
                if is_actual:
                    actual_load[owner] += amount * delta
                else:
                    owners[owner][amount] += delta
                    if owners[owner][amount] == 0:
                        del owners[owner][amount]
                load += max(actual_load[owner], max(owners[owner], default=0)) - before
            hi = points[i + 1] if i + 1 < len(points) else None
            residual = max(0, capacity - load)
            if segments and segments[-1][2] == residual:
                segments[-1] = segments[-1][0], hi, residual
            else:
                segments.append((tick, hi, residual))
        result[resource] = tuple(segments)
    return CapacityCalendar(capacities, result)
