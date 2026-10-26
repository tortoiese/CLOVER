"""Independent semantic validation; this code never reads solver linear rows."""

from __future__ import annotations

import math
from collections import defaultdict
from itertools import pairwise

from simulation.planning.contracts import (
    Action,
    CabinArc,
    Reservation,
    Result,
    RouteInput,
    Snapshot,
    energy_up,
)
from simulation.planning.units import decimal, gain_limit


def capacity_errors(reservations: tuple[Reservation, ...], capacities: dict[str, int],
                    fixed: tuple[Reservation, ...] = ()) -> list[str]:
    """Check half-open intervals; grandfather existing users during capacity shrink."""
    errors = []
    for resource, capacity in capacities.items():
        own = [r for r in reservations if r.resource == resource]
        old = [r for r in fixed if r.resource == resource]
        boundaries = sorted({t for r in own + old for t in (r.start, r.end)})
        for start, end in pairwise(boundaries):
            if end <= start:
                continue
            used = sum(r.amount for r in old if r.start < end and r.end > start)
            added = sum(r.amount for r in own if r.start < end and r.end > start)
            if added > max(0, capacity - used):
                errors.append(f"capacity:{resource}@{start}")
                break
    return errors


def reservations_for(actions: tuple[Action, ...], snapshot: Snapshot | None = None) -> tuple[Reservation, ...]:
    """Convert work/charge actions to committed resource intervals."""
    result = [Reservation(a.arc.resource, a.arc.robot, a.start, a.end) for a in actions if a.arc.resource]
    if snapshot is not None and snapshot.scope == "traffic":
        routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
        for action in actions:
            if action.arc.kind != "move":
                continue
            route = routes.get((action.arc.robot, action.arc.origin, action.arc.destination))
            if route is None:
                continue
            step = snapshot.settings.step
            result.extend(Reservation(u.resource, action.arc.robot, action.start + u.start * step,
                                      action.start + u.end * step, u.amount) for u in route.uses)
            result.extend(Reservation("elevator:" + r.cabin, action.arc.robot, action.start + r.start * step,
                                      action.start + r.end * step) for r in route.rides)
    return tuple(result)


def return_requirement(snapshot: Snapshot, robot_id: str, location: str) -> int | None:
    """Minimum physically reachable charger energy, or explicit unreachability."""
    robot = next(r for r in snapshot.robots if r.id == robot_id)
    routes = {(r.origin, r.destination): r for r in snapshot.routes if r.robot == robot_id}
    values = [0 if location == f.id else energy_up(decimal(routes[location, f.id].distance_m) * decimal(robot.drain))
              for f in snapshot.facilities if f.kind == "charge" and
              (location == f.id or (location, f.id) in routes)]
    return min(values) if values else None


def validate(result: Result, prefix: bool = False) -> list[str]:
    """Check location/time, jobs, energy, resources and safe prefix termination."""
    snapshot = result.snapshot
    errors = []
    facilities = {f.id: f for f in snapshot.facilities}
    jobs = {j.id: j for j in snapshot.jobs}
    robots = {r.id: r for r in snapshot.robots}
    routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
    grouped: dict[str, list[Action]] = defaultdict(list)
    checked_routes: set[tuple[str, str, str]] = set()
    done = set()
    for action in result.actions:
        if action.arc.robot not in robots:
            errors.append("unknown robot")
            continue
        grouped[action.arc.robot].append(action)
    for rid, actions in grouped.items():
        actions.sort(key=lambda a: (a.start, a.end))
        robot = robots[rid]
        location, now, battery = robot.location, snapshot.now, robot.battery
        for action in actions:
            arc = action.arc
            if arc.kind != "work" and arc.job is not None:
                errors.append("job on non-work action")
            if arc.kind in ("move", "wait") and arc.resource:
                errors.append("unexpected stationary resource")
            if not all(math.isfinite(x) for x in (action.start, action.end)):
                errors.append("non-finite time")
                continue
            if abs(action.start - now) > 1e-6 or arc.origin != location or action.end <= action.start:
                errors.append(f"continuity:{rid}")
            duration = action.end - action.start
            if arc.kind == "move":
                route = routes.get((rid, arc.origin, arc.destination))
                if (route is None or duration + 1e-6 < route.seconds or
                        arc.energy != energy_up(decimal(route.distance_m) * decimal(robot.drain)) or action.gain):
                    errors.append(f"route:{rid}")
                if route is not None and (rid, arc.origin, arc.destination) not in checked_routes:
                    errors.extend(route_errors(snapshot, route))
                    checked_routes.add((rid, arc.origin, arc.destination))
            elif arc.kind == "work":
                facility, job = facilities.get(arc.resource), jobs.get(arc.job)
                if (facility is None or job is None or arc.resource not in job.stations or arc.job in done
                        or robot.charge_only or robot.emergency_order is not None and job.order_id != robot.emergency_order):
                    errors.append(f"job:{arc.job}")
                elif duration + 1e-6 < facility.duration or arc.destination != arc.resource:
                    errors.append(f"work:{arc.job}")
                if arc.energy != energy_up(decimal(duration) * decimal(snapshot.work_drain)) or action.gain:
                    errors.append("work energy")
                done.add(arc.job)
            elif arc.kind == "charge":
                facility = facilities.get(arc.resource)
                if (facility is None or facility.kind != "charge" or not 0 <= action.gain <=
                        gain_limit(facility.rate, duration, facility.rate_raw)
                        or arc.energy or arc.origin != arc.resource):
                    errors.append("charge")
            elif arc.kind != "wait" or arc.energy or action.gain:
                errors.append("unknown action")
            if arc.kind != "move" and arc.origin != arc.destination:
                errors.append("stationary action changes location")
            battery += action.gain - arc.energy
            if not 0 <= battery <= robot.capacity:
                errors.append(f"battery:{rid}")
            location, now = arc.destination, action.end
            if arc.kind == "work":
                back = return_requirement(snapshot, rid, location)
                if back is None or battery < robot.reserve + back:
                    errors.append(f"unsafe work boundary:{rid}")
        back = return_requirement(snapshot, rid, location)
        if back is None or battery < robot.reserve + back:
            errors.append(f"unsafe terminal:{rid}")
        if not prefix and abs(now - (snapshot.now + math.floor(snapshot.settings.horizon / snapshot.settings.step + 1e-9)
                                     * snapshot.settings.step)) > 1e-6:
            errors.append(f"horizon:{rid}")
    if not prefix:
        if set(grouped) != set(robots):
            errors.append("missing robot path")
        if (len(set(result.deferred)) != len(result.deferred) or done & set(result.deferred) or
                done | set(result.deferred) != set(jobs)):
            errors.append("job partition")
    capacities = {f.id: f.capacity for f in snapshot.facilities} | {r.id: r.capacity for r in snapshot.resources}
    errors += capacity_errors(reservations_for(result.actions, snapshot), capacities, snapshot.fixed)
    if snapshot.scope == "traffic":
        errors.extend(validate_cabins(result, prefix))
    return errors


def route_errors(snapshot: Snapshot, route: RouteInput) -> list[str]:
    """Verify geometry/floor/width evidence independently of model flow rows."""
    import config
    from simulation.path_graph import edge_blocked
    from utils.config_models import ObstacleConfig
    errors = []
    places = {f.id: (f.x, f.y, f.floor) for f in snapshot.facilities}
    positions = {"@" + p[0]: (p[1], p[2], p[3]) for p in snapshot.positions}
    places.update(positions)
    origin, destination = places.get(route.origin), places.get(route.destination)
    if origin is None or destination is None:
        return ["route endpoint unavailable"]
    lengths = {len(route.points), len(route.floors), len(route.lifts), len(route.nodes), len(route.prev_nodes)}
    if len(lengths) != 1:
        return ["route metadata length"]
    geometry = {n[0]: n for n in snapshot.geometry}
    widths = {p[0]: p[4] for p in snapshot.positions}
    cabins = {c.id: c for c in snapshot.cabins}
    x, y, floor = origin
    metres = seconds = 0.0
    robot = next(r for r in snapshot.robots if r.id == route.robot)
    for point, target_floor, lift, node_id, previous in zip(
            route.points, route.floors, route.lifts, route.nodes, route.prev_nodes, strict=True):
        tx, ty = point
        if geometry:
            node = geometry.get(node_id)
            if node is None or (node[1], node[2], node[3]) != (tx, ty, target_floor):
                errors.append("route node geometry")
            if previous is not None:
                source = geometry.get(previous)
                if source is None or (source[1], source[2], source[3]) != (x, y, floor):
                    errors.append("route previous node")
        if lift is not None:
            cabin = cabins.get(lift)
            if cabin is None or target_floor == floor or math.dist((x, y), (tx, ty)) > 1e-6:
                errors.append("route lift/floor")
            else:
                seconds += abs(target_floor - floor) * cabin.travel_seconds + cabin.door_seconds
        else:
            if target_floor != floor:
                errors.append("floor change without cabin")
            horizontal = math.dist((x, y), (tx, ty)) * config.METERS_PER_PIXEL
            obstacles = [ObstacleConfig(*row) for row in snapshot.obstacles if row[-1] == floor]
            if edge_blocked((x, y), (tx, ty), obstacles):
                errors.append("route crosses obstacle")
            metres += horizontal
            seconds += horizontal / robot.speed
            if snapshot.edges and previous is not None:
                eligible = [e for e in snapshot.edges if (e[1], e[2]) == (previous, node_id) or
                            e[4] and (e[2], e[1]) == (previous, node_id)]
                if not eligible or not any(e[3] - snapshot.clearance + 1e-9 >= widths.get(route.robot, 0)
                                           for e in eligible):
                    errors.append("route edge/width")
        x, y, floor = tx, ty, target_floor
    if (x, y, floor) != destination or abs(metres - route.distance_m) > 1e-6:
        errors.append("route endpoint/distance")
    if route.seconds + 1e-6 < seconds:
        errors.append("route physical duration")
    return errors


def validate_cabins(result: Result, prefix: bool = False) -> list[str]:
    """Independently check cabin location, empty movement and exact rider links."""
    snapshot = result.snapshot
    step = snapshot.settings.step
    routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
    required = []
    errors = []
    for action in result.actions:
        if action.arc.kind == "move":
            route = routes.get((action.arc.robot, action.arc.origin, action.arc.destination))
            if route:
                offset = round((action.start - snapshot.now) / step)
                required.extend(CabinArc(r.cabin, r.origin, r.destination, r.start + offset, r.end + offset, "ride")
                                for r in route.rides)
    supplied = [r for r in result.cabins if r.kind == "ride"]
    if sorted(map(repr, required)) != sorted(map(repr, supplied)):
        errors.append("cabin rider linkage")
    definitions = {c.id: c for c in snapshot.cabins}
    for cid in {c.cabin for c in result.cabins}:
        cabin = definitions.get(cid)
        if cabin is None or not cabin.available:
            errors.append("unavailable cabin")
            continue
        floor, tick = cabin.floor, 0
        for arc in sorted((a for a in result.cabins if a.cabin == cid), key=lambda a: a.start):
            if arc.origin != floor or arc.start != tick or arc.end <= arc.start:
                errors.append(f"cabin continuity:{cid}")
            minimum = abs(arc.destination - arc.origin) * cabin.travel_seconds
            if arc.kind == "ride":
                minimum += cabin.door_seconds
            elif arc.kind == "wait":
                if arc.origin != arc.destination:
                    errors.append("cabin waiting changes floor")
            elif arc.kind != "empty":
                errors.append("unknown cabin action")
            if (arc.end - arc.start) * step + 1e-6 < minimum:
                errors.append("cabin duration")
            floor, tick = arc.destination, arc.end
        if not prefix and tick != math.floor(snapshot.settings.horizon / step + 1e-9):
            errors.append("cabin horizon")
    if not prefix and {c.id for c in snapshot.cabins if c.available} != {c.cabin for c in result.cabins}:
        errors.append("missing cabin path")
    return errors
