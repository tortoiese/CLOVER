"""Independent schedule validation; no solver variable or row inspection."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict

from simulation.planning.cabin_state import ready_state, validate_releases
from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import MODEL_VERSION, Result, Use
from simulation.planning.model import initial_location
from simulation.planning.snapshot import prepare
from simulation.planning.units import decimal, energy_up, gain_limit


def validate(result: Result, *, reconstruct_routes: bool = True) -> tuple[str, ...]:
    """Check action flow, job coverage, physical intervals, cabin and energy.

    Args:
        result: Complete decoded schedule, not a sparse solver incumbent.
        reconstruct_routes: Recompute geometric coefficients from frozen input.

    Returns:
        Empty tuple on success; otherwise explicit rejection reasons.
    """
    errors = []
    data, snapshot = result.input, result.input.snapshot
    step = snapshot.settings.time_unit
    horizon = math.floor(snapshot.settings.horizon / step + 1e-9)
    if result.report.status not in ("OPTIMAL", "FEASIBLE") or result.report.version != MODEL_VERSION:
        return ("unsupported plan status/version",)
    try:
        validate_releases(snapshot, json.loads(snapshot.factory_json)["profile"]["floor_count"])
    except ValueError as exc:
        return (str(exc),)
    if reconstruct_routes and prepare(snapshot) != data:
        return ("route coefficients differ from observed geometry",)
    robots = {r.id: r for r in snapshot.robots if r.available}
    facilities = {f.id: f for f in snapshot.facilities if f.active}
    jobs = {j.uid: j for j in snapshot.jobs}
    selected = Counter()
    uses_by_resource = defaultdict(list)
    cabins = Counter()
    trip_chains = defaultdict(list)
    grouped = defaultdict(list)
    calendar = build_calendar(data)
    residual = calendar.capacities
    for action in result.actions:
        if action.arc.robot not in robots:
            errors.append("unknown/unavailable robot")
        else:
            grouped[action.arc.robot].append(action)
    for rid, robot in robots.items():
        battery = robot.battery
        location, end = initial_location(robot, data), 0
        actions = sorted(grouped[rid], key=lambda a: a.arc.start)
        for action in actions:
            arc = action.arc
            if any(
                isinstance(v, bool) or not isinstance(v, int)
                for v in (arc.start, arc.end, arc.energy, arc.service_start, action.gain)
            ):
                errors.append("noninteger action")
                continue
            if arc.origin != location or arc.start != end or not arc.start < horizon or arc.end <= arc.start:
                errors.append("discontinuous robot flow or new action in tail")
            if arc.end - horizon > snapshot.settings.tail_limit:
                errors.append("tail limit exceeded")
            route = None
            if arc.route is not None:
                if (
                    isinstance(arc.route, bool)
                    or not isinstance(arc.route, int)
                    or not 0 <= arc.route < len(data.routes)
                ):
                    errors.append("invalid route index")
                    continue
                route = data.routes[arc.route]
                if (route.robot, route.origin, route.destination) != (rid, arc.origin, arc.destination):
                    errors.append("route/robot/location mismatch")
            elif arc.origin != arc.destination:
                errors.append("movement missing route")
            travel = math.ceil(route.seconds / step - 1e-9) if route else 0
            distance = route.distance_m if route else 0
            expected_uses = (
                tuple(
                    Use(
                        u.resource,
                        u.unit,
                        u.amount,
                        arc.start + math.floor(u.start / step + 1e-9),
                        arc.start + math.ceil(u.end / step - 1e-9),
                    )
                    for u in route.uses
                )
                if route
                else ()
            )
            expected_cabins = tuple(c.cabin for c in route.cabins) if route else ()
            if route:
                expected_uses += tuple(
                    Use(
                        "cabin:" + c.cabin,
                        "slot",
                        1,
                        arc.start + math.floor(c.start / step + 1e-9),
                        max(
                            arc.start + math.floor(c.start / step + 1e-9) + 1,
                            arc.start + math.ceil(c.end / step - 1e-9),
                        ),
                    )
                    for c in route.cabins
                )
            required_energy = energy_up(decimal(distance) * decimal(robot.drain))
            facility = facilities.get(arc.destination)
            if arc.kind == "wait":
                if (
                    (snapshot.settings.formulation == "grid" and arc.end != arc.start + 1)
                    or arc.origin != arc.destination
                    or action.gain
                    or arc.job is not None
                ):
                    errors.append("invalid wait")
            elif arc.kind == "service":
                job = jobs.get(arc.job)
                if (
                    job is None
                    or facility is None
                    or facility.kind != "work"
                    or arc.destination not in job.stations
                    or facility.operation != job.operation
                    or facility.dedicated_order not in (None, job.order_id)
                    or robot.emergency_order not in (None, job.order_id)
                ):
                    errors.append("incompatible job/facility")
                    continue
                selected[arc.job] += 1
                duration = max(1, math.ceil(facility.nominal_seconds / step - 1e-9))
                if arc.service_start != arc.start + travel or arc.end != arc.start + travel + duration or action.gain:
                    errors.append("service timing/gain")
                required_energy = energy_up(
                    decimal(distance) * decimal(robot.drain)
                    + decimal(duration) * decimal(step) * decimal(snapshot.work_drain)
                )
                expected_uses += (Use("work:" + facility.id, "slot", 1, arc.service_start, arc.end),)
            elif arc.kind == "charge":
                units = arc.end - arc.start - travel
                if (
                    facility is None
                    or facility.kind != "charge"
                    or arc.job is not None
                    or arc.service_start != arc.start + travel
                    or units < 1
                    or (snapshot.settings.formulation == "grid" and units != 1)
                    or (units > 1 and arc.end > horizon)
                    or not 0 <= action.gain <= gain_limit(0, step, facility.charge_rate) * units
                ):
                    errors.append("invalid charge")
                    continue
                expected_uses += (Use("charge:" + facility.id, "slot", 1, arc.service_start, arc.end),)
            else:
                errors.append("unknown action kind")
            if arc.energy != required_energy or arc.uses != expected_uses or arc.cabins != expected_cabins:
                errors.append("incorrect energy or resource interval/unit")
            battery -= required_energy
            if battery < 0:
                errors.append("battery depleted within action")
            battery += action.gain
            if battery > robot.capacity:
                errors.append("battery capacity exceeded")
            if arc.kind == "service":
                returns = [
                    0 if arc.destination == f.id else energy_up(decimal(r.distance_m) * decimal(robot.drain))
                    for f in facilities.values()
                    if f.kind == "charge"
                    for r in (
                        [None]
                        if arc.destination == f.id
                        else [
                            r for r in data.routes if (r.robot, r.origin, r.destination) == (rid, arc.destination, f.id)
                        ]
                    )
                ]
                if not returns or battery < robot.reserve + min(returns):
                    errors.append("service return/reserve impossible")
            location, end = arc.destination, arc.end
            for use in arc.uses:
                if (
                    any(isinstance(v, bool) or not isinstance(v, int) for v in (use.amount, use.start, use.end))
                    or use.resource not in residual
                    or use.unit != residual[use.resource][0]
                    or not 0 < use.amount
                    or not arc.start <= use.start < use.end <= arc.end
                ):
                    errors.append("invalid resource demand")
                else:
                    uses_by_resource[use.resource].extend(((use.start, use.amount), (use.end, -use.amount)))
            cabins.update(arc.cabins)
            if route:
                for c in route.cabins:
                    at = arc.start + math.floor(c.start / step + 1e-9)
                    end_at = max(at + 1, arc.start + math.ceil(c.end / step - 1e-9))
                    trip_chains[c.cabin].append((at, end_at, float(c.source_floor), float(c.destination)))
        if end < horizon:
            errors.append("incomplete robot path")
        if (
            snapshot.settings.formulation == "native"
            and snapshot.settings.native_actions
            and sum(a.arc.kind != "wait" for a in actions) > snapshot.settings.native_actions
        ):
            errors.append("native action scope exceeded")
        if location != initial_location(robot, data):
            returns = [
                0 if location == f.id else energy_up(decimal(r.distance_m) * decimal(robot.drain))
                for f in facilities.values()
                if f.kind == "charge"
                for r in (
                    [None]
                    if location == f.id
                    else [r for r in data.routes if (r.robot, r.origin, r.destination) == (rid, location, f.id)]
                )
            ]
            if not returns or battery < robot.reserve + min(returns):
                errors.append("terminal return/reserve impossible")
    if (
        (set(selected) | set(result.deferred)) != set(jobs)
        or any(n != 1 for n in selected.values())
        or (set(selected) & set(result.deferred))
        or len(set(result.deferred)) != len(result.deferred)
    ):
        errors.append("job selected/deferred coverage mismatch")
    for resource, events in uses_by_resource.items():
        occupied = 0
        changes = Counter()
        for tick, delta in events:
            changes[tick] += delta
        for tick, _, _ in calendar.segments[resource]:
            changes[tick] += 0
        for tick in sorted(changes):
            occupied += changes[tick]
            if occupied > calendar.at(resource, tick):
                errors.append("resource capacity exceeded")
    if not snapshot.settings.joint_cabins and any(n > 1 for n in cabins.values()):
        errors.append("cabin restricted state path exceeded")
    cabin_inputs = {c.id: c for c in snapshot.cabins}
    for cid, trips in trip_chains.items():
        if cid not in cabin_inputs:
            errors.append("unknown cabin")
            continue
        floor, end = ready_state(cabin_inputs[cid], snapshot.now, step)
        for start, arrival, source, destination in sorted(trips):
            if source != floor or start < end:
                errors.append("cabin floor path discontinuity or overlap")
            floor, end = destination, arrival
    return tuple(sorted(set(errors)))
