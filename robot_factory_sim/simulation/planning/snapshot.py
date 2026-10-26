"""Observe on the simulation thread, build routes only in the solver worker."""

from __future__ import annotations

import hashlib
import json
import math
import pickle
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

import config
from simulation.corridor import CorridorNetwork
from simulation.path_graph import PathGraph
from simulation.planning.contracts import (
    CabinArc,
    CabinInput,
    Facility,
    JobInput,
    Reservation,
    ResourceInput,
    ResourceUse,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    energy_down,
    energy_up,
)

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


@dataclass(frozen=True)
class Request:
    """Detached factory bytes and scalar observations, safe to send to a process."""

    snapshot: Snapshot
    factory: bytes
    positions: tuple[tuple[str, float, float, int, float, float], ...]


def topology_hash(engine: SimulationEngine) -> str:
    """Hash layout, parameters and active entity configuration deterministically."""
    return hashlib.sha256(json.dumps(asdict(engine.factory), sort_keys=True).encode()).hexdigest()


def capture(engine: SimulationEngine, settings: Settings, token: str,
            reservations: tuple[Reservation, ...], charge_intents: set[str]) -> Request:
    """Capture observed input without paths, process noise or future arrivals."""
    settings.validate()
    now = engine.now
    facilities = tuple(
        [Facility(s.id, s.cfg.x, s.cfg.y, s.cfg.floor, "work", 1,
                  max(.5, s.cfg.avg_process_time + settings.sigma * s.cfg.process_time_std))
         for s in engine.stations.values() if s.active]
        + [Facility(c.id, c.cfg.x, c.cfg.y, c.cfg.floor, "charge", c.cfg.capacity,
                    rate=energy_down(c.cfg.charge_rate), rate_raw=c.cfg.charge_rate)
           for c in engine.chargers.values() if c.active])
    robots = tuple(RobotInput(r.robot_id, "@" + r.robot_id, energy_down(r.live_battery()),
                              energy_down(r.capacity), energy_up(r.capacity * config.BATTERY_RESERVE / 100),
                              max(.05, r.cfg.move_speed), r.cfg.battery_drain_rate, r.emergency_order,
                              r.robot_id in charge_intents,
                              energy_up(r.capacity * max(75, r.cfg.charge_threshold) / 100)
                              if r.robot_id in charge_intents else 0)
                   for r in engine.robot_list() if r.is_available())
    positions = tuple((r.robot_id, *r.position(), r.floor, r.cfg.width, r.cfg.length)
                      for r in engine.robot_list() if r.is_available())
    jobs = [JobInput(t.task_id, () if getattr(t, "planning_blocked", None) else tuple(engine._usable_stations(t)),
                     t.created_at, t.deadline,
                     (8, 4, 2, 1)[min(3, max(0, t.priority))] + math.floor(max(0, now - t.created_at) / 30),
                     t.order_id, t.priority) for t in engine.pending_tasks if not t.cancelled]
    jobs.sort(key=lambda j: (-j.weight, j.created, j.id))
    jobs = jobs[:settings.max_tasks]
    fixed = [r for r in reservations if r.end > now]
    # Extend an executing prefix when its nominal completion has passed. Actual
    # resources remain authoritative even when stochastic work overruns the plan.
    committed = {r.robot for r in reservations}
    for robot in engine.robot_list():
        if robot.robot_id in committed and not robot.is_available():
            for r in reservations:
                if r.robot == robot.robot_id and r.resource == robot.target_id:
                    extension = replace(r, start=min(r.start, now), end=max(r.end, now + settings.interval))
                    fixed = [x for x in fixed if x != r]
                    fixed.append(extension)
    if settings.scope == "traffic":
        observed = {}
        for edge_id, resource in engine.corridors._corridor_resource.items():
            observed[engine.corridors.resource_id(edge_id)] = resource
        for node_id, resource in engine.corridors._intersection_resource.items():
            observed[engine.corridors.intersection_id(node_id)] = resource
        for rid, resource in observed.items():
            for user in resource.users:
                owner = next((r.robot_id for r in engine.robot_list() if r.process is user.proc), "@external")
                amount = getattr(user, "amount", 1)
                covered = [r for r in fixed if r.resource == rid and r.robot == owner and r.start <= now < r.end]
                end = max([now + settings.interval, *(r.end for r in covered)])
                fixed = [r for r in fixed if r not in covered]
                fixed.append(Reservation(rid, owner, min([now, *(r.start for r in covered)]), end, amount))
    cabins = tuple(CabinInput(e.id, round(e.floor_position()), tuple(range(1, engine.factory.floor_count + 1)),
                              e.cfg.travel_time, e.door_time,
                              e.active and not e.moving and e.occupied == 0 and
                              not getattr(engine.scheduler, "busy_lifts", {}).get(e.id))
                   for e in engine.elevators.values())
    snapshot = Snapshot(token, topology_hash(engine), now, settings, robots, tuple(jobs), facilities, (),
                        tuple(fixed), config.WORK_DRAIN_PER_SEC, settings.scope, cabins=cabins,
                        positions=positions, geometry=tuple((n.id, n.x, n.y, n.floor, n.node_type)
                                                           for n in engine.factory.nodes),
                        edges=tuple((e.id, e.from_node, e.to_node, e.width, e.is_bidirectional)
                                    for e in engine.factory.edges), clearance=engine.factory.profile.edge_clearance,
                        obstacles=tuple((o.id, o.obstacle_type, o.x1, o.y1, o.x2, o.y2, o.is_passable, o.label, o.floor)
                                        for o in engine.factory.obstacles))
    return Request(snapshot, pickle.dumps(engine.factory, protocol=5), positions)


def prepare(request: Request) -> Snapshot:
    """Compute width-safe multi-floor paths using detached configuration only."""
    factory = pickle.loads(request.factory)
    snapshot = request.snapshot
    if any(e.capacity != 1 for e in factory.elevators):
        raise ValueError("UNSUPPORTED_CABIN_CAPACITY: RH supports one robot per cabin trip; use capacity=1")
    graph = PathGraph(factory)
    if any(e.capacity != 1 for e in graph.implicit_elevators):
        raise ValueError("UNSUPPORTED_CABIN_CAPACITY: linked elevator nodes require capacity=1")
    import simpy
    net = CorridorNetwork(simpy.Environment(), factory)
    resources = {}
    cabins = {c.id: c for c in snapshot.cabins}
    for cabin in snapshot.cabins:
        resources["elevator:" + cabin.id] = ResourceInput("elevator:" + cabin.id, 1, "elevator")
    step = snapshot.settings.step
    facilities = tuple(replace(f, x=(graph.facility_point(f.id) or (f.x, f.y))[0],
                               y=(graph.facility_point(f.id) or (f.x, f.y))[1]) for f in snapshot.facilities)
    locations = {f.id: (f.x, f.y, f.floor) for f in facilities}
    robots = {r.id: r for r in snapshot.robots}
    routes = []
    for rid, x, y, floor, width, length in request.positions:
        robot = robots[rid]
        filtered = graph.for_robot(width)
        origins = {robot.location: (x, y, floor), **locations}
        for origin, (sx, sy, sfloor) in origins.items():
            for destination in locations:
                if destination == origin:
                    continue
                route = filtered.route_to_facility(sx, sy, destination, sfloor)
                if route is None:
                    continue
                px, py, pfloor = sx, sy, sfloor
                distance = seconds = 0.0
                uses, rides, segment_starts = [], [], []
                for i, ((tx, ty), tfloor, lift) in enumerate(zip(route.points, route.floors, route.lifts, strict=True)):
                    start_tick = math.ceil(seconds / step - 1e-9)
                    segment_starts.append(start_tick)
                    if lift is None and tfloor == pfloor:
                        metres = math.hypot(tx - px, ty - py) * config.METERS_PER_PIXEL
                        distance += metres
                        duration = metres / robot.speed
                        if snapshot.scope == "traffic":
                            seconds += math.ceil(duration / step - 1e-9) * step
                            edge = net.edge_between(route.prev_nodes[i], route.nodes[i])
                            if edge is not None and net.edge_kind(edge) == "corridor" and duration > 1e-9:
                                resource = net.resource_id(edge)
                                resources[resource] = ResourceInput(resource, net.edge_resource(edge).capacity, "corridor")
                                uses.append(ResourceUse(resource, start_tick, math.ceil(seconds / step),
                                                        net.width_units(width)))
                        else:
                            seconds += duration
                    else:
                        elevator = next((e for e in [*factory.elevators, *graph.implicit_elevators] if e.id == lift), None)
                        if elevator is None or not elevator.is_active:
                            seconds = math.inf
                            break
                        if snapshot.scope == "traffic":
                            cabin = cabins.get(lift)
                            if cabin is None or not cabin.available:
                                seconds = math.inf
                                break
                            duration = math.ceil((abs(tfloor - pfloor) * elevator.travel_time
                                                  + factory.profile.door_time_sec) / step)
                            seconds += max(1, duration) * step
                            rides.append(CabinArc(lift, pfloor, tfloor, start_tick, math.ceil(seconds / step), "ride"))
                        else:
                            seconds += abs(tfloor - pfloor) * elevator.travel_time + factory.profile.door_time_sec
                    node = route.nodes[i]
                    if next((n.node_type for n in factory.nodes if n.id == node), "") == "intersection":
                        duration = length / robot.speed + config.INTERSECTION_MARGIN_SEC
                        start_tick = segment_starts[-1]
                        if snapshot.scope == "traffic":
                            seconds += math.ceil(duration / step) * step
                            resource = net.intersection_id(node)
                            resources[resource] = ResourceInput(resource, 1, "intersection")
                            uses.append(ResourceUse(resource, start_tick, math.ceil(seconds / step)))
                        else:
                            seconds += duration
                    px, py, pfloor = tx, ty, tfloor
                if not math.isfinite(seconds):
                    continue
                routes.append(RouteInput(rid, origin, destination, tuple(route.points), tuple(route.floors),
                                         tuple(route.lifts), tuple(route.nodes), tuple(route.prev_nodes), distance, seconds,
                                         tuple(uses), tuple(rides), tuple(segment_starts)))
    # Extend a short horizon to fit long nominal work, up to an explicit hard cap.
    required = []
    for job in snapshot.jobs:
        choices = [f.duration + r.seconds for f in facilities if f.id in job.stations and f.kind == "work"
                   for r in routes if r.destination == f.id and r.origin.startswith("@")]
        if choices:
            required.append(min(choices))
    horizon = min(300, max(snapshot.settings.horizon, math.ceil(max(required, default=0) + snapshot.settings.step)))
    return replace(snapshot, facilities=facilities, routes=tuple(routes), resources=tuple(resources.values()),
                   settings=replace(snapshot.settings, horizon=horizon))
