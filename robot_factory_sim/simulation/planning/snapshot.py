"""Observe once on the plant thread; prepare detached PR9 routes in a worker."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict
from itertools import product
from typing import TYPE_CHECKING

import simpy

import config
from simulation.corridor import CorridorNetwork
from simulation.path_graph import PathGraph, Route
from simulation.planning.cabin_state import release_state, validate_releases
from simulation.planning.contracts import (
    SNAPSHOT_SCHEMA,
    CabinInput,
    CabinUse,
    ExcludedJob,
    FacilityInput,
    JobInput,
    PhysicalClaim,
    PlannedHold,
    PreparedInput,
    ResourceInput,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
)
from simulation.planning.occupancy import cabin_release, capture_holds, owner_key
from simulation.planning.units import decimal, energy_down, energy_up
from simulation.resource_pool import TRAFFIC_CONTRACT
from simulation.task_generator import TaskState
from simulation.traffic_rules import tail_clearance_seconds
from utils.config_models import FactoryConfig

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def topology_hash(engine: SimulationEngine) -> str:
    """Hash current configuration and controls without execution samples."""
    return hashlib.sha256(
        _canonical(
            {"factory": engine.factory.to_dict(), "plant": asdict(engine.plant), "params": dict(engine.params)}
        ).encode()
    ).hexdigest()


def capture(engine: SimulationEngine, settings: Settings, token: str) -> Snapshot:
    """Capture only currently observed input, without constructing any route.

    Args:
        engine: The engine owned by the calling simulation thread.
        settings: Bounded candidate and route preparation settings.
        token: Controller-owned request identity, not an executable plan token.

    Returns:
        Frozen, JSON/pickle-safe observations with no future arrival/noise data.
    """
    settings.validate()
    if not isinstance(token, str) or not token:
        raise ValueError("a nonempty planning request token is required")
    now = engine.now
    factory_json = _canonical(engine.factory.to_dict())
    topology = topology_hash(engine)
    facilities = []
    for station in sorted(engine.stations.values(), key=lambda item: item.id):
        point = engine.path_graph.facility_point(station.id) or (station.cfg.x, station.cfg.y)
        floor = engine.path_graph.facility_floor(station.id) or station.cfg.floor
        facilities.append(
            FacilityInput(
                station.id,
                "work",
                *point,
                floor,
                station.active,
                1,
                station.resource.count,
                station.waiting,
                station.cfg.task_type,
                max(0.5, station.cfg.avg_process_time + settings.sigma * station.cfg.process_time_std),
                dedicated_order=station.dedicated_order,
                current_job=station.current_task.external_uid if station.current_task else None,
            )
        )
    for charger in sorted(engine.chargers.values(), key=lambda item: item.id):
        point = engine.path_graph.facility_point(charger.id) or (charger.cfg.x, charger.cfg.y)
        floor = engine.path_graph.facility_floor(charger.id) or charger.cfg.floor
        facilities.append(
            FacilityInput(
                charger.id,
                "charge",
                *point,
                floor,
                charger.active,
                charger.resource.capacity,
                charger.resource.count,
                charger.waiting,
                charge_rate=charger.cfg.charge_rate,
            )
        )
    robots = tuple(
        RobotInput(
            r.robot_id,
            r.color_index,
            *r.position(),
            r.floor,
            energy_down(r.live_battery()),
            energy_down(r.capacity),
            energy_up(decimal(r.capacity) * decimal(config.BATTERY_RESERVE) / 100),
            max(0.05, r.cfg.move_speed),
            r.cfg.battery_drain_rate,
            r.cfg.width,
            r.cfg.length,
            r.alive and engine.robots.get(r.robot_id) is r and r.is_available(),
            r.alive,
            r.current_task.external_uid if r.current_task else None,
            r.emergency_order,
            r.forced_charge,
        )
        for r in sorted([*engine.robots.values(), *engine.retired], key=lambda r: r.color_index)
    )
    jobs = []
    for task in engine.pending_tasks:
        if task.status != TaskState.QUEUED:
            continue
        stations = tuple(
            sorted(
                sid
                for sid in task.allowed_stations
                if sid in engine.stations and engine.stations[sid].cfg.task_type == task.operation
            )
        )
        weight = (8, 4, 2, 1)[min(3, max(0, task.priority))] + math.floor(max(0, now - task.created_at) / 30)
        jobs.append(
            JobInput(
                task.external_uid,
                task.task_id,
                stations,
                task.operation,
                task.created_at,
                task.deadline,
                task.priority,
                weight,
                task.order_id,
            )
        )
    jobs.sort(key=lambda job: (-job.weight, job.created, job.uid))
    excluded = tuple(ExcludedJob(job.uid, now - job.created, "candidate_limit") for job in jobs[settings.max_tasks :])
    resources, claims = [], []
    for pool in sorted(engine.corridors._pools.values(), key=lambda item: item.resource_id):
        resources.append(ResourceInput(pool.resource_id, pool.unit, pool.capacity, pool.count))
        for claim in sorted(pool.users, key=lambda item: item.request.request_id):
            claims.append(
                PhysicalClaim(
                    claim.request.request_id,
                    claim.robot_id,
                    pool.resource_id,
                    claim.amount,
                    pool.unit,
                    claim.usage_since,
                )
            )
    cabins = []
    for e in sorted(engine.elevators.values(), key=lambda e: e.id):
        releases = [
            (r, certificate)
            for r in [*engine.robots.values(), *engine.retired]
            if (certificate := cabin_release(r, e)) is not None
        ]
        robot, certificate = releases[0] if len(releases) == 1 else (None, None)
        cabins.append(
            CabinInput(
                e.id,
                e.floor_position(),
                e.active,
                e.moving,
                e.occupied,
                e.waiting,
                e.cfg.travel_time,
                e.door_time,
                certificate[0] if certificate else None,
                certificate[1] if certificate else None,
                owner_key(robot) if robot is not None else None,
            )
        )
    committed = []
    for r in [*engine.robots.values(), *engine.retired]:
        action = getattr(r, "planned_action", None)
        if action is not None:
            for use in action[1].arc.uses:
                committed.append(PlannedHold(r.robot_id, use.resource, use.amount, use.unit))
    return Snapshot(
        token,
        engine.execution_epoch,
        now,
        engine.layout_version,
        topology,
        factory_json,
        settings,
        robots,
        tuple(jobs[: settings.max_tasks]),
        tuple(facilities),
        tuple(resources),
        tuple(claims),
        tuple(cabins),
        excluded,
        len(jobs),
        engine.plant.work_drain_per_sec,
        engine.corridors._paused,
        committed=tuple(committed),
        holds=capture_holds(engine),
    )


def _profile(
    route: Route,
    robot: RobotInput,
    origin: tuple[float, float, int],
    cabins: dict[str, CabinInput],
    net: CorridorNetwork,
    source_floors: dict[str, float] | None = None,
) -> tuple[RouteInput | None, str]:
    x, y, floor = origin
    metres = seconds = 0.0
    starts, ends, rides, sections = [], [], [], []
    cabin_floors = {
        cid: cabin.release_floor if cabin.release_at is not None else cabin.floor for cid, cabin in cabins.items()
    }
    cabin_floors.update(source_floors or {})
    index = 0
    while index < len(route.points):
        lift = route.lifts[index] if route.lifts else None
        if lift:
            cabin = cabins.get(lift)
            if cabin is None or not cabin.active:
                return None, "unavailable_cabin"
            if cabin.waiting or ((cabin.moving or cabin.occupied) and cabin.release_at is None):
                return None, "unknown_cabin_release"
            target = route.floors[index]
            start = seconds
            starts.append(start)
            seconds += (abs(cabin_floors[lift] - floor) + abs(target - floor)) * cabin.travel_seconds
            seconds += cabin.door_seconds
            ends.append(seconds)
            rides.append(CabinUse(lift, floor, target, start, seconds, cabin_floors[lift], index))
            cabin_floors[lift] = target
            x, y = route.points[index]
            floor, index = target, index + 1
            continue
        end = net.section_end(route, index)
        for i in range(index, end + 1):
            starts.append(seconds)
            tx, ty = route.points[i]
            distance = math.hypot(tx - x, ty - y) * config.METERS_PER_PIXEL
            metres += distance
            seconds += distance / robot.speed
            x, y = tx, ty
            if net.is_intersection(route.nodes[i]):
                seconds += robot.length / robot.speed + config.INTERSECTION_MARGIN_SEC
            ends.append(seconds)
        demands = net.section_demands(route, index, end, robot.width)
        sections.extend((index, end) for _ in demands)
        if demands:
            seconds += tail_clearance_seconds(robot.length, robot.speed)
        index = end + 1
    holds = net.planned_reservations(route, robot.width, robot.length, robot.speed, tuple(starts), tuple(ends))
    return RouteInput(
        robot.id,
        "",
        "",
        metres,
        seconds,
        tuple(route.points),
        tuple(route.floors),
        tuple(route.lifts),
        tuple(route.nodes),
        tuple(route.prev_nodes),
        tuple(starts),
        tuple(ends),
        holds,
        tuple(rides),
        tuple(sections),
    ), ""


def prepare(snapshot: Snapshot, *, deadline: float | None = None) -> PreparedInput:
    """Build K=1 routes from detached input, without reading the live plant.

    Args:
        snapshot: Detached observation; only certified protected releases have finite ends.
        deadline: Optional absolute monotonic wall deadline shared with a future solver.

    Returns:
        Free-flow route alternatives and explicit unavailable-pair reasons. These
        are resource coefficients, never a validated joint execution schedule.
    """
    from dataclasses import replace

    if snapshot.schema != SNAPSHOT_SCHEMA or snapshot.traffic_contract != TRAFFIC_CONTRACT:
        raise ValueError("planning traffic contract mismatch")
    snapshot.settings.validate()
    if deadline is not None and (not math.isfinite(deadline) or time.perf_counter() >= deadline):
        raise TimeoutError("route preparation exhausted decision budget")
    factory = FactoryConfig.from_json(snapshot.factory_json)
    validate_releases(snapshot, factory.floor_count)
    graph = PathGraph(factory)
    net = CorridorNetwork(simpy.Environment(), factory)
    cabins = {cabin.id: cabin for cabin in snapshot.cabins}
    chosen = {sid for job in snapshot.jobs for sid in job.stations}
    facilities = {f.id: f for f in snapshot.facilities if f.active and (f.kind == "charge" or f.id in chosen)}
    locations = {fid: (f.x, f.y, f.floor) for fid, f in facilities.items()}
    routes, unavailable = [], []
    pairs = 0
    for robot in snapshot.robots:
        if not robot.available:
            continue
        filtered = graph.for_robot(robot.width)
        origin_key = "@" + robot.id
        while origin_key in {f.id for f in snapshot.facilities}:
            origin_key = "@" + origin_key
        for origin, position in {origin_key: (robot.x, robot.y, robot.floor), **locations}.items():
            for destination in facilities:
                if destination == origin:
                    continue
                pairs += 1
                if pairs > snapshot.settings.max_routes:
                    raise ValueError("ROUTE_LIMIT: input preparation pair limit exceeded")
                if deadline is not None and time.perf_counter() >= deadline:
                    raise TimeoutError("route preparation exhausted decision budget")
                reason = "traffic_drain" if snapshot.traffic_paused else ""
                route = None if reason else filtered.route_to_facility(*position[:2], destination, position[2])
                data = None
                if route is None:
                    reason = reason or "no_width_safe_path"
                else:
                    lifts = sorted({cid for cid in route.lifts if cid is not None})
                    if any(cid not in cabins for cid in lifts):
                        unavailable.append((robot.id, origin, destination, "unavailable_cabin"))
                        continue
                    options = [
                        sorted({cabins[cid].floor, *range(1, factory.floor_count + 1)})
                        if snapshot.settings.joint_cabins
                        else [
                            release_state(cabins[cid], snapshot.now)[1]
                            if cabins[cid].release_at is not None
                            else cabins[cid].floor
                        ]
                        for cid in lifts
                    ]
                    for values in product(*options):
                        if deadline is not None and time.perf_counter() >= deadline:
                            raise TimeoutError("cabin route variants exhausted decision budget")
                        data, reason = _profile(
                            route, robot, position, cabins, net, dict(zip(lifts, values, strict=True))
                        )
                        if data is not None:
                            if len(routes) >= snapshot.settings.max_routes:
                                raise ValueError("ROUTE_LIMIT: cabin variants exceed route limit")
                            routes.append(replace(data, origin=origin, destination=destination))
                        else:
                            break
                if data is None:
                    unavailable.append((robot.id, origin, destination, reason))
    return PreparedInput(snapshot, tuple(routes), tuple(unavailable))
