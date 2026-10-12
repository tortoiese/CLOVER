"""Reservations of committed actions, owned by the simulation thread."""

from __future__ import annotations

from collections import defaultdict

from simulation.planning.models import Action, Reservation


class ReservationTable:
    """Keep only committed prefixes; future tentative plans are replaceable."""

    def __init__(self) -> None:
        """Create an empty resource calendar."""
        self._by_robot: dict[str, tuple[Reservation, ...]] = {}

    def commit(self, robot_id: str, actions: tuple[Action, ...]) -> None:
        """Register the work or charge intervals of an indivisible prefix."""
        self._by_robot[robot_id] = tuple(
            Reservation(a.resource, robot_id, a.start, a.end, a.kind) for a in actions if a.kind in ("work", "charge")
        )

    def release(self, robot_id: str) -> None:
        """Release a completed, cancelled or interrupted commitment."""
        self._by_robot.pop(robot_id, None)

    def reservations(self) -> tuple[Reservation, ...]:
        """Return an immutable copy without sharing calendar state."""
        return tuple(item for robot in sorted(self._by_robot) for item in self._by_robot[robot])


def merge_reservations(items: list[Reservation]) -> tuple[Reservation, ...]:
    """Union intervals belonging to the same resource and robot.

    This prevents an actual overrun extension from double-counting its owner.
    """
    groups: dict[tuple[str, str, str], list[Reservation]] = defaultdict(list)
    for item in items:
        groups[item.resource, item.robot_id, item.kind].append(item)
    result: list[Reservation] = []
    for (resource, robot, kind), intervals in sorted(groups.items()):
        intervals.sort(key=lambda r: (r.start, r.end))
        start, end = intervals[0].start, intervals[0].end
        for interval in intervals[1:]:
            if interval.start <= end + 1e-8:
                end = max(end, interval.end)
            else:
                result.append(Reservation(resource, robot, start, end, kind))
                start, end = interval.start, interval.end
        result.append(Reservation(resource, robot, start, end, kind))
    return tuple(result)
