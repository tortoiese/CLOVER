"""Project engine state into an immutable, nominal planning snapshot."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

import config
from simulation.planning.models import (
    ChargerInput,
    JobInput,
    Reservation,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    StationInput,
)
from simulation.planning.reservation_table import ReservationTable, merge_reservations
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


def nominal_availability(engine: SimulationEngine, robot: Robot, settings: Settings) -> tuple[float, float, str | None]:
    """Estimate readiness without reading future sampled service durations."""
    now = engine.now
    actions = robot.planned_actions
    if actions:
        return max(actions[-1].end, now + settings.step), actions[-1].battery_after, actions[-1].destination
    if robot.current_task is not None:
        station = engine.stations.get(robot.current_task.station_id)
        if station is None or not station.active:
            return now + settings.horizon + settings.step, robot.live_battery(), None
        duration = max(0.5, station.cfg.avg_process_time + settings.sigma * station.cfg.process_time_std)
        elapsed = max(0.0, now - station.busy_since) if station.busy_since is not None else 0.0
        travel = 0.0
        travel_energy = 0.0
        if robot.state == RobotState.MOVING:
            x, y = robot.position()
            distance = engine.path_graph.distance_to_facility(x, y, station.id)
            if not math.isfinite(distance):
                return now + settings.horizon + settings.step, robot.live_battery(), None
            travel = distance * config.METERS_PER_PIXEL / max(0.05, robot.cfg.move_speed)
            travel_energy = distance * config.METERS_PER_PIXEL * robot.cfg.battery_drain_rate
        remaining = max(settings.step, duration - elapsed)
        energy = robot.live_battery() - travel_energy - (elapsed + remaining) * config.WORK_DRAIN_PER_SEC
        return now + travel + remaining, max(0.0, energy), station.id
    if robot.target_id in engine.chargers:
        charger = engine.chargers[robot.target_id]
        x, y = robot.position()
        distance = engine.path_graph.distance_to_facility(x, y, charger.id)
        if not math.isfinite(distance):
            return now + settings.horizon + settings.step, robot.live_battery(), None
        travel = distance * config.METERS_PER_PIXEL / max(0.05, robot.cfg.move_speed)
        energy = robot.live_battery() - distance * config.METERS_PER_PIXEL * robot.cfg.battery_drain_rate
        target = engine.scheduler.charge_target(robot, engine) * robot.capacity / 100.0
        duration = max(0.0, target - energy) / max(0.01, charger.cfg.charge_rate)
        return now + travel + duration + settings.step, target, charger.id
    return now + settings.horizon + settings.step, robot.live_battery(), None


def build_snapshot(engine: SimulationEngine, settings: Settings, table: ReservationTable, token: str) -> Snapshot:
    """Collect current job metadata, routes, fixed prefixes and nominal readiness."""
    now = engine.now
    points: dict[str, tuple[float, float]] = {}
    canonical: dict[tuple[float, float], str] = {}

    def location(facility: str) -> str | None:
        point = engine.path_graph.facility_point(facility)
        if point is None:
            return None
        if point not in canonical:
            canonical[point] = facility
            points[facility] = point
        return canonical[point]

    stations = tuple(
        StationInput(s.id, location(s.id), max(0.5, s.cfg.avg_process_time + settings.sigma * s.cfg.process_time_std))
        for s in engine.stations.values()
        if engine.station_available(s.id) and location(s.id) is not None
    )
    chargers = tuple(
        ChargerInput(c.id, location(c.id), c.capacity, max(0.01, c.cfg.charge_rate))
        for c in engine.chargers.values()
        if c.active and location(c.id) is not None
    )
    fixed = list(table.reservations())
    robots = []
    for robot in engine.robot_list():
        if not robot.alive or robot.state == RobotState.FAULT:
            continue
        if robot.is_available():
            point = robot.position()
            origin = canonical.get(point, "@" + robot.robot_id)
            points[origin] = point
            ready, battery = 0, robot.live_battery()
        else:
            finish, battery, target = nominal_availability(engine, robot, settings)
            origin = location(target) if target else None
            ready = max(1, math.ceil((finish - now) / settings.step - 1e-8))
            # Use fixed resource calendars even for a robot not eligible for free planning.
            if robot.state in (RobotState.WORKING, RobotState.WAITING) and robot.current_task is not None:
                fixed.append(Reservation(robot.current_task.station_id, robot.robot_id, now, finish, "work"))
            elif robot.state in (RobotState.CHARGING, RobotState.CHARGE_WAIT) and robot.target_id:
                fixed.append(Reservation(robot.target_id, robot.robot_id, now, finish, "charge"))
            elif not robot.planned_actions and target:
                kind = "charge" if target in engine.chargers else "work"
                fixed.append(Reservation(target, robot.robot_id, now, finish, kind))
            if origin is None or ready * settings.step > settings.horizon:
                continue
        reserve = robot.capacity * config.BATTERY_RESERVE / 100.0
        if battery < reserve - 1e-7:
            continue  # The coordinator sends this robot through its explicit recovery path.
        robots.append(
            RobotInput(
                robot.robot_id,
                origin,
                ready,
                battery,
                robot.capacity,
                reserve,
                max(0.05, robot.cfg.move_speed),
                max(0.0, robot.cfg.battery_drain_rate),
                robot.emergency_order,
            )
        )
    jobs = []
    for task in engine.pending_tasks:
        order = engine.orders.get(task.order_id) if task.order_id else None
        if order is not None and not order.is_active:
            continue
        allowed = task.allowed_stations or (task.station_id,)
        allowed = tuple(
            s
            for s in allowed
            if engine.station_available(s) and engine.stations[s].dedicated_order in (None, task.order_id)
        )
        if not allowed:
            continue
        weight = (8, 4, 2, 1)[max(0, min(3, task.priority))] + int(max(0.0, now - task.created_at) / 30.0)
        jobs.append(
            JobInput(
                task.task_id,
                allowed,
                task.created_at,
                order.deadline if order is not None else task.deadline,
                weight,
                task.order_id,
            )
        )
    jobs.sort(key=lambda j: (-j.weight, j.deadline if j.deadline is not None else math.inf, j.created, j.id))
    jobs = jobs[: settings.max_tasks]
    routes = []
    facility_ids = {s.id for s in stations} | {c.id for c in chargers}
    destinations = {location(f): f for f in sorted(facility_ids)}
    for origin, point in sorted(points.items()):
        for dest, facility_id in sorted(destinations.items()):
            if origin == dest:
                continue
            route = engine.path_graph.route_to_facility(*point, facility_id)
            if route is not None and math.isfinite(route.length):
                routes.append(RouteInput(origin, dest, tuple(route.points), route.length))
    return Snapshot(
        token,
        engine.planning_revision,
        now,
        settings,
        tuple(robots),
        tuple(jobs),
        stations,
        chargers,
        tuple(routes),
        merge_reservations(fixed),
        config.METERS_PER_PIXEL,
        config.WORK_DRAIN_PER_SEC,
    )


def shift_plan_snapshot(snapshot: Snapshot, shift: float) -> Snapshot:
    """Change only the epoch of a delayed, still-valid result."""
    return replace(snapshot, now=snapshot.now + shift)
