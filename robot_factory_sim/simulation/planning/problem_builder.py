"""Time-expanded location paths with no Big-M disjunctions."""

from __future__ import annotations

import math
from collections import Counter

from simulation.planning.models import Arc, Problem, Snapshot

EPS = 1e-8
SINK = "@sink"


def build_problem(snapshot: Snapshot) -> Problem:
    """Build acyclic robot paths, fixed capacity usage and return-energy bounds.

    Args:
        snapshot: Immutable current state and nominal processing estimates.

    Returns:
        A linear model specification shared by all solver backends.

    Raises:
        ValueError: Invalid units, capacity or initial battery.
        OverflowError: Candidate arc budget is exceeded.
    """
    settings = snapshot.settings
    if settings.step <= 0 or settings.horizon < settings.step or settings.timeout <= 0:
        raise ValueError("Invalid planning time settings")
    ticks = math.floor(settings.horizon / settings.step + EPS)
    facilities = {s.location for s in snapshot.stations} | {c.location for c in snapshot.chargers}
    routes = {(r.origin, r.destination): r for r in snapshot.routes}
    stations = {s.id: s for s in snapshot.stations}
    arcs: list[Arc] = []
    returns: list[tuple[str, str, float]] = []

    def add(arc: Arc) -> None:
        arcs.append(arc)
        if len(arcs) > settings.max_arcs:
            raise OverflowError("Planning arc budget exceeded; reduce horizon or candidate count")

    for robot in snapshot.robots:
        if robot.speed <= 0 or robot.drain < 0 or not robot.reserve - EPS <= robot.battery <= robot.capacity + EPS:
            raise ValueError(f"Invalid robot energy state: {robot.id}")
        if robot.ready > ticks:
            continue
        locations = facilities | {robot.location}
        return_map: dict[str, float] = {}
        reachable_at: dict[str, int] = {robot.location: robot.ready}
        for loc in sorted(locations):
            back = [
                0.0 if loc == c.location else routes[loc, c.location].distance
                for c in snapshot.chargers
                if loc == c.location or (loc, c.location) in routes
            ]
            # A no-charger scenario remains usable, but no return-energy guarantee is claimed.
            if not snapshot.chargers:
                return_map[loc] = 0.0
            elif back:
                return_map[loc] = min(back) * snapshot.meters_per_pixel * robot.drain
            if loc != robot.location and (robot.location, loc) in routes:
                duration = routes[robot.location, loc].distance * snapshot.meters_per_pixel / robot.speed
                reachable_at[loc] = robot.ready + max(1, math.ceil(duration / settings.step - EPS))
        for loc, energy in return_map.items():
            returns.append((robot.id, loc, energy))
        for loc in sorted(locations):
            earliest = reachable_at.get(loc, ticks + 1)
            for t in range(earliest, ticks):
                add(Arc(robot.id, loc, loc, t, t + 1, "wait"))
                for charger in snapshot.chargers:
                    if charger.location == loc and charger.capacity > 0 and charger.rate > 0:
                        add(Arc(robot.id, loc, loc, t, t + 1, "charge", charger.id))
                for job in snapshot.jobs:
                    if robot.emergency_order is not None and job.order_id != robot.emergency_order:
                        continue
                    for sid in job.stations:
                        station = stations.get(sid)
                        if station is None or station.location != loc:
                            continue
                        duration = max(1, math.ceil(station.duration / settings.step - EPS))
                        if t + duration <= ticks:
                            add(
                                Arc(
                                    robot.id,
                                    loc,
                                    loc,
                                    t,
                                    t + duration,
                                    "work",
                                    sid,
                                    job.id,
                                    station.duration * snapshot.work_drain,
                                )
                            )
                for dest in sorted(facilities):
                    route = routes.get((loc, dest))
                    if dest == loc or route is None:
                        continue
                    duration = max(
                        1, math.ceil(route.distance * snapshot.meters_per_pixel / robot.speed / settings.step - EPS)
                    )
                    if t + duration <= ticks:
                        energy = route.distance * snapshot.meters_per_pixel * robot.drain
                        # A single travel leg can never consume more than usable capacity.
                        if energy <= robot.capacity - robot.reserve + EPS:
                            add(
                                Arc(
                                    robot.id, loc, dest, t, t + duration, "move", energy=energy, distance=route.distance
                                )
                            )
            if earliest <= ticks and loc in return_map:
                add(Arc(robot.id, loc, SINK, ticks, ticks + 1, "end"))

    fixed: Counter[tuple[str, int]] = Counter()
    for reservation in snapshot.fixed:
        start = max(0, math.floor((reservation.start - snapshot.now) / settings.step + EPS))
        end = min(ticks, math.ceil((reservation.end - snapshot.now) / settings.step - EPS))
        for t in range(start, end):
            fixed[reservation.resource, t] += 1
    return Problem(snapshot, tuple(arcs), tuple((s, t, n) for (s, t), n in sorted(fixed.items())), tuple(returns))
