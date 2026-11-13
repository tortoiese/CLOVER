"""Detached release certificates for protected rides, without forecasting queues."""

from __future__ import annotations

import math

from simulation.planning.contracts import CabinInput, Snapshot


def release_state(cabin: CabinInput, now: float) -> tuple[float, float] | None:
    """Read a complete release certificate; malformed partial fields are rejected."""
    values = cabin.release_at, cabin.release_floor, cabin.release_owner
    if all(value is None for value in values):
        return None
    if (
        any(
            isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value)
            for value in values[:2]
        )
        or not isinstance(cabin.release_owner, str)
        or not cabin.release_owner
        or cabin.release_at <= now + 1e-9
        or cabin.release_floor < 1
        or not float(cabin.release_floor).is_integer()
        or cabin.occupied != 1
    ):
        raise ValueError("MODEL_INVALID: invalid cabin release certificate")
    return cabin.release_at, float(cabin.release_floor)


def validate_releases(snapshot: Snapshot, floors: int) -> None:
    """Require a finite actual hold with the same owner, cabin and release time."""
    for cabin in snapshot.cabins:
        state = release_state(cabin, snapshot.now)
        if state is None:
            continue
        end, floor = state
        if floor > floors or not any(
            h.owner == cabin.release_owner
            and h.resource == "cabin:" + cabin.id
            and h.source == "actual"
            and h.amount == 1
            and h.unit == "slot"
            and h.start <= snapshot.now
            and h.end == end
            for h in snapshot.holds
        ):
            raise ValueError("MODEL_INVALID: cabin certificate has no matching actual hold")


def ready_state(cabin: CabinInput, now: float, step: float) -> tuple[float, int]:
    """Return the floor/tick after the certified ride; queues remain unavailable."""
    state = release_state(cabin, now)
    if state is None:
        return float(cabin.floor), 0
    end, floor = state
    return floor, math.ceil((end - now) / step - 1e-9)
