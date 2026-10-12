"""Independent operational validation; no solver variables are inspected."""

from __future__ import annotations

import math
from collections import defaultdict

from simulation.planning.models import Plan, Reservation

EPS = 1e-5


def capacity_errors(reservations: tuple[Reservation, ...], capacities: dict[str, int]) -> list[str]:
    """Check half-open intervals by releasing before granting at an equal time."""
    changes: dict[str, list[tuple[float, int]]] = defaultdict(list)
    errors = []
    for item in reservations:
        if item.end <= item.start + EPS:
            continue
        if item.resource not in capacities:
            errors.append(f"Unknown resource: {item.resource}")
            continue
        changes[item.resource].extend(((item.start, 1), (item.end, -1)))
    for resource, events in changes.items():
        count = 0
        for _, delta in sorted(events):
            count += delta
            if count > capacities[resource]:
                errors.append(f"Capacity exceeded: {resource}")
                break
    return errors


def validate_plan(plan: Plan) -> list[str]:
    """Check position, duration, energy, task partition and resource capacity."""
    snapshot = plan.snapshot
    robots = {r.id: r for r in snapshot.robots}
    stations = {s.id: s for s in snapshot.stations}
    chargers = {c.id: c for c in snapshot.chargers}
    jobs = {j.id: j for j in snapshot.jobs}
    routes = {(r.origin, r.destination): r for r in snapshot.routes}
    errors = []
    per_robot = defaultdict(list)
    seen = set()
    reservations = list(snapshot.fixed)
    for action in plan.actions:
        if not all(
            math.isfinite(v)
            for v in (action.start, action.end, action.energy, action.gain, action.distance, action.battery_after)
        ):
            errors.append("Non-finite action")
            continue
        if action.robot_id not in robots or action.end <= action.start:
            errors.append("Unknown robot or invalid time interval")
            continue
        per_robot[action.robot_id].append(action)
    horizon_end = (
        snapshot.now + math.floor(snapshot.settings.horizon / snapshot.settings.step + EPS) * snapshot.settings.step
    )
    for robot in snapshot.robots:
        if robot.ready * snapshot.settings.step > snapshot.settings.horizon + EPS:
            continue
        location = robot.location
        now = snapshot.now + robot.ready * snapshot.settings.step
        balance = robot.battery
        for a in sorted(per_robot[robot.id], key=lambda item: item.start):
            if abs(a.start - now) > EPS or a.origin != location:
                errors.append(f"Discontinuous path: {robot.id}")
            duration = a.end - a.start
            if a.kind == "move":
                route = routes.get((a.origin, a.destination))
                expected = route.distance * snapshot.meters_per_pixel * robot.drain if route else math.inf
                if route is None or a.points != route.points or abs(a.energy - expected) > EPS:
                    errors.append(f"Invalid route/energy: {robot.id}")
                elif duration + EPS < route.distance * snapshot.meters_per_pixel / robot.speed:
                    errors.append(f"Travel too fast: {robot.id}")
            elif a.kind == "work":
                job = jobs.get(a.job_id)
                station = stations.get(a.resource)
                if (
                    job is None
                    or station is None
                    or a.resource not in job.stations
                    or station.location != a.origin
                    or a.destination != a.origin
                ):
                    errors.append("Ineligible work")
                else:
                    if (
                        duration + EPS < station.duration
                        or abs(a.energy - station.duration * snapshot.work_drain) > EPS
                    ):
                        errors.append("Invalid processing duration/energy")
                    if robot.emergency_order is not None and job.order_id != robot.emergency_order:
                        errors.append("Emergency dedication violated")
                if a.job_id in seen:
                    errors.append("Duplicate work")
                seen.add(a.job_id)
                reservations.append(Reservation(a.resource, robot.id, a.start, a.end, "work"))
            elif a.kind == "charge":
                charger = chargers.get(a.resource)
                if (
                    charger is None
                    or charger.location != a.origin
                    or a.destination != a.origin
                    or a.gain < -EPS
                    or a.gain > duration * charger.rate + EPS
                    or abs(a.energy) > EPS
                ):
                    errors.append("Invalid charge")
                reservations.append(Reservation(a.resource, robot.id, a.start, a.end, "charge"))
            elif a.kind == "wait":
                if a.destination != a.origin or abs(a.energy) > EPS or abs(a.gain) > EPS:
                    errors.append("Invalid wait")
            else:
                errors.append("Unknown action kind")
            if a.kind != "charge" and abs(a.gain) > EPS:
                errors.append("Charging without plug")
            balance += a.gain - a.energy
            if balance < robot.reserve - EPS or balance > robot.capacity + EPS or abs(balance - a.battery_after) > EPS:
                errors.append(f"Battery violation: {robot.id}")
            location, now = a.destination, a.end
        if abs(now - horizon_end) > EPS:
            errors.append(f"Incomplete path: {robot.id}")
        returns = [
            0.0 if location == c.location else routes[location, c.location].distance
            for c in snapshot.chargers
            if location == c.location or (location, c.location) in routes
        ]
        if snapshot.chargers and (
            not returns or balance + EPS < robot.reserve + min(returns) * snapshot.meters_per_pixel * robot.drain
        ):
            errors.append(f"Terminal return reserve violated: {robot.id}")
    omitted = set(plan.deferred)
    if len(omitted) != len(plan.deferred) or seen & omitted or seen | omitted != set(jobs):
        errors.append("Invalid job partition")
    capacities = {s.id: 1 for s in snapshot.stations}
    capacities.update({c.id: c.capacity for c in snapshot.chargers})
    errors.extend(capacity_errors(tuple(reservations), capacities))
    return errors
