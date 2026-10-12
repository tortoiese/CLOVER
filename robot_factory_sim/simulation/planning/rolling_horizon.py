"""Nominal rolling-horizon control, stale-result validation and safe fallback."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

import config
from simulation.planning.milp_backend import MilpBackend
from simulation.planning.models import (
    Action,
    PlanningBackend,
    PlanningStats,
    Reservation,
    Settings,
    Snapshot,
    SolveResult,
)
from simulation.planning.plan_executor import commit_prefix, execute_plan
from simulation.planning.plan_validator import capacity_errors, validate_plan
from simulation.planning.reservation_table import ReservationTable
from simulation.planning.snapshot_builder import build_snapshot
from simulation.scheduler import BaseScheduler

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot
    from simulation.station import ChargingStation


class RollingHorizonScheduler(BaseScheduler):
    """Use MILP plans with conservative committed prefixes and physical resources."""

    name = "milp_rh"
    planning_active = True

    def __init__(self, backend: PlanningBackend | None = None) -> None:
        """Create a synchronous headless adapter, optionally supplied a backend."""
        self.backend = backend or MilpBackend()
        self.table = ReservationTable()
        self.stats = PlanningStats()
        self._epoch = uuid.uuid4().hex
        self._serial = 0
        self._next = 0.0
        self._dirty = True
        self._urgent = False
        self._empty = True
        self._waiting_token: str | None = None
        self._result: SolveResult | None = None
        self._submit: Callable[[Snapshot], None] | None = None
        self._last_accepted = False

    def set_submitter(self, submit: Callable[[Snapshot], None] | None) -> None:
        """Use a Qt signal sender for GUI runs; None restores synchronous solves."""
        self._submit = submit

    @property
    def waiting_token(self) -> str | None:
        """The current request identity; older engines' results cannot match it."""
        return self._waiting_token

    def planning_state(self) -> dict[str, object]:
        """Expose status, bounds and a recorded time shift without a false guarantee."""
        status = self.stats.last_report.status if self.stats.last_report else "WAITING"
        execution = "SHIFTED_FEASIBLE" if self._last_accepted and self.stats.last_shift > 1e-7 else status
        return {
            "pending": self._waiting_token is not None,
            "execution_status": execution,
            "report": asdict(self.stats.last_report) if self.stats.last_report else None,
            "last_shift": self.stats.last_shift,
            **self.diagnostics(),
        }

    def deliver(self, result: SolveResult) -> None:
        """Receive a worker result on the simulation thread only."""
        if result.token != self._waiting_token:
            self.stats.stale += 1
            return
        self._result = result

    def invalidate(self, reason: str, urgent: bool = False) -> None:
        """Mark future plans dirty; committed actions retain their reservations."""
        self._dirty = True
        self._urgent |= urgent or reason == "arrival" and self._empty

    def release(self, robot_id: str) -> None:
        """Release the robot's prefix and trigger planning at its new availability."""
        self.table.release(robot_id)
        self.invalidate("released", urgent=True)

    def diagnostics(self) -> dict[str, float]:
        """Expose solver counters separately from the original eight KPIs."""
        report = self.stats.last_report
        return {
            "planning_solves": float(self.stats.solves),
            "planning_accepted": float(self.stats.accepted),
            "planning_fallbacks": float(self.stats.fallbacks),
            "planning_stale": float(self.stats.stale),
            "planning_invalid": float(self.stats.invalid),
            "planning_overruns": float(self.stats.overruns),
            "solver_seconds": report.seconds if report else 0.0,
            "solver_optimal": float(report is not None and report.status == "OPTIMAL"),
        }

    def on_delay(self, robot_id: str, action: object, actual_end: float) -> None:
        """Record an overrun and re-plan rather than assuming a free resource."""
        self.stats.overruns += 1
        self.stats.audit.append({"time": actual_end, "kind": "overrun", "robot": robot_id})
        self.stats.audit = self.stats.audit[-500:]
        self.invalidate("delay", urgent=True)

    def _settings(self, engine: SimulationEngine) -> Settings:
        return Settings(
            horizon=engine.params["planning_horizon"],
            step=engine.params["planning_step"],
            interval=engine.params["planning_interval"],
            timeout=engine.params["planning_timeout"],
            max_tasks=int(engine.params["planning_max_tasks"]),
            sigma=engine.params["planning_sigma"],
        )

    def dispatch(self, engine: SimulationEngine) -> None:
        """Run all emergency and normal decisions through one prefix executor."""
        if self._result is not None:
            result, self._result = self._result, None
            self._waiting_token = None
            self._accept(engine, result)
        if self._waiting_token is not None:
            return
        settings = self._settings(engine)
        if engine.now + 1e-7 < self._next and not self._urgent:
            return
        self._urgent = False
        self._dirty = False
        self._next = engine.now + settings.interval
        self._serial += 1
        token = f"{self._epoch}:{self._serial}"
        snapshot = build_snapshot(engine, settings, self.table, token)
        # Busy robots are fixed prefixes. Re-plan them when they actually become idle,
        # rather than committing a successor based on an uncertain release estimate.
        snapshot = replace(snapshot, robots=tuple(r for r in snapshot.robots if r.ready == 0))
        if not snapshot.robots:
            self._fallback(engine, snapshot, count=False)
            return
        if not snapshot.jobs:
            self._empty = True
            self._fallback(engine, snapshot, count=False)
            return
        self._empty = False
        self.stats.solves += 1
        self._waiting_token = token
        if self._submit is not None:
            self._submit(snapshot)
        else:
            result = self.backend.solve(snapshot)
            self._waiting_token = None
            self._accept(engine, result)

    def _accept(self, engine: SimulationEngine, result: SolveResult) -> None:
        self._last_accepted = False
        self.stats.last_report = result.report
        self.stats.reports.append(result.report)
        self.stats.reports = self.stats.reports[-500:]
        settings = self._settings(engine)
        if result.plan is None:
            snapshot = build_snapshot(engine, settings, self.table, result.token)
            self._fallback(engine, snapshot)
            return
        original = result.plan
        lag = engine.now - original.snapshot.now
        current = build_snapshot(
            engine, replace(settings, max_tasks=max(1, len(engine.pending_tasks))), self.table, result.token
        )
        current_robots = {r.id: r for r in current.robots if r.ready == 0}
        current_jobs = {j.id: j for j in current.jobs}
        old = original.snapshot
        stable_robots = all(current_robots.get(r.id) == r for r in old.robots)
        stable_jobs = all(current_jobs.get(j.id) == j for j in old.jobs)
        stable_facilities = current.stations == old.stations and current.chargers == old.chargers
        route_map = {(r.origin, r.destination): r for r in current.routes}
        stable_routes = all(
            route_map.get((r.origin, r.destination)) == r
            for r in old.routes
            if r.origin in {a.origin for a in original.actions if a.kind == "move"}
        )
        if (
            lag < -1e-6
            or lag > settings.interval
            or settings != old.settings
            or not stable_robots
            or not stable_jobs
            or not stable_facilities
            or not stable_routes
        ):
            self.stats.stale += 1
            self._urgent = True
            self._fallback(engine, current)
            return
        # A time-shifted incumbent remains feasible only after capacity revalidation.
        shifted = replace(old, now=engine.now, fixed=current.fixed, revision=engine.planning_revision)
        actions = tuple(replace(a, start=a.start + lag, end=a.end + lag) for a in original.actions)
        plan = replace(original, snapshot=shifted, actions=actions)
        errors = validate_plan(plan)
        if errors:
            self.stats.invalid += 1
            self.stats.audit.append({"time": engine.now, "kind": "invalid_plan", "errors": errors})
            self.stats.audit = self.stats.audit[-500:]
            self._fallback(engine, current)
            return
        self.stats.last_shift = lag
        self._last_accepted = True
        self.stats.accepted += 1
        committed = execute_plan(engine, self.table, plan)
        if not committed and old.jobs:
            self._fallback(engine, current)

    def _slot(self, snapshot: Snapshot, resource: str, start: float, duration: float, capacity: int) -> float:
        reservations = tuple(r for r in snapshot.fixed if r.resource == resource)
        candidates = sorted({start, *(r.end for r in reservations if r.end >= start)})
        for candidate in candidates:
            extra = Reservation(resource, "@new", candidate, candidate + duration, "candidate")
            if not capacity_errors((*reservations, extra), {resource: capacity}):
                return candidate
        return max(start, max((r.end for r in reservations), default=start))

    def _fallback(self, engine: SimulationEngine, snapshot: Snapshot, count: bool = True) -> None:
        """Dispatch nominally safe prefixes; never bypass resource reservations."""
        self.stats.fallbacks += int(count)
        stations = {s.id: s for s in snapshot.stations}
        routes = {(r.origin, r.destination): r for r in snapshot.routes}
        inputs = {r.id: r for r in snapshot.robots if r.ready == 0}
        jobs = sorted(snapshot.jobs, key=lambda j: (-j.weight, j.created, j.id))
        used_jobs = set()
        fixed = list(snapshot.fixed)
        for robot in engine.robot_list():
            if not robot.is_available():
                continue
            r = inputs.get(robot.robot_id)
            best = None
            if r is not None:
                for job in jobs:
                    if job.id in used_jobs or r.emergency_order is not None and job.order_id != r.emergency_order:
                        continue
                    for sid in job.stations:
                        station = stations.get(sid)
                        if station is None:
                            continue
                        route = routes.get((r.location, station.location))
                        distance = 0.0 if r.location == station.location else route.distance if route else math.inf
                        returns = [
                            0.0 if station.location == c.location else routes[station.location, c.location].distance
                            for c in snapshot.chargers
                            if station.location == c.location or (station.location, c.location) in routes
                        ]
                        if not math.isfinite(distance) or snapshot.chargers and not returns:
                            continue
                        move_energy = distance * snapshot.meters_per_pixel * r.drain
                        work_energy = station.duration * snapshot.work_drain
                        back_energy = min(returns, default=0.0) * snapshot.meters_per_pixel * r.drain
                        if r.battery + 1e-7 < r.reserve + move_energy + work_energy + back_energy:
                            continue
                        travel = distance * snapshot.meters_per_pixel / r.speed
                        arrive = engine.now + travel
                        local = replace(snapshot, fixed=tuple(fixed))
                        start = self._slot(local, sid, arrive, station.duration, 1)
                        cost = (start + station.duration - engine.now) / job.weight
                        if best is None or cost < best[0]:
                            best = (cost, job, station, route, travel, move_energy, work_energy, start)
            if best is not None:
                _, job, station, route, travel, move_energy, work_energy, start = best
                actions = []
                balance = r.battery
                arrive = engine.now + travel
                if travel > 1e-7:
                    balance -= move_energy
                    actions.append(
                        Action(
                            r.id,
                            "move",
                            engine.now,
                            arrive,
                            r.location,
                            station.location,
                            energy=move_energy,
                            distance=route.distance,
                            points=route.points,
                            battery_after=balance,
                        )
                    )
                if start > arrive + 1e-7:
                    actions.append(
                        Action(r.id, "wait", arrive, start, station.location, station.location, battery_after=balance)
                    )
                actions.append(
                    Action(
                        r.id,
                        "work",
                        start,
                        start + station.duration,
                        station.location,
                        station.location,
                        station.id,
                        job.id,
                        work_energy,
                        battery_after=balance - work_energy,
                    )
                )
                if commit_prefix(engine, self.table, tuple(actions)):
                    used_jobs.add(job.id)
                    fixed.append(Reservation(station.id, r.id, start, start + station.duration, "work"))
            elif robot.battery_pct < config.OPPORTUNISTIC_CHARGE_LEVEL or snapshot.jobs:
                self._recover_charge(engine, robot, replace(snapshot, fixed=tuple(fixed)))
                fixed = list(build_snapshot(engine, snapshot.settings, self.table, snapshot.token).fixed)

    def _recover_charge(self, engine: SimulationEngine, robot: Robot, snapshot: Snapshot) -> bool:
        desired = config.OPTIMIZED_CHARGE_TARGET if snapshot.jobs else config.MIN_CHARGE_TARGET
        desired = min(100.0, max(desired, robot.cfg.charge_threshold))
        if robot.battery_pct >= desired - 1e-7:
            return False
        best = None
        for charger in snapshot.chargers:
            route = engine.path_graph.route_to_facility(*robot.position(), charger.id)
            if route is None:
                continue
            drain = route.length * config.METERS_PER_PIXEL * robot.cfg.battery_drain_rate
            before = robot.live_battery()
            if drain > before + 1e-7:
                continue  # Do not invent a reachable charger or force a known depletion.
            travel = route.length * config.METERS_PER_PIXEL / max(0.05, robot.cfg.move_speed)
            target = robot.capacity * desired / 100.0
            gain = max(0.0, target - (before - drain))
            if gain <= 1e-7:
                continue
            duration = gain / charger.rate
            start = self._slot(snapshot, charger.id, engine.now + travel, duration, charger.capacity)
            if best is None or start + duration < best[0]:
                best = (start + duration, charger, route, travel, drain, gain, start)
        if best is None:
            return False
        end, charger, route, travel, drain, gain, start = best
        actions = []
        origin = next((r.location for r in snapshot.robots if r.id == robot.robot_id), "@" + robot.robot_id)
        balance = robot.live_battery() - drain
        arrive = engine.now + travel
        if travel > 1e-7:
            actions.append(
                Action(
                    robot.robot_id,
                    "move",
                    engine.now,
                    arrive,
                    origin,
                    charger.location,
                    energy=drain,
                    distance=route.length,
                    points=tuple(route.points),
                    battery_after=balance,
                )
            )
        if start > arrive + 1e-7:
            actions.append(
                Action(robot.robot_id, "wait", arrive, start, charger.location, charger.location, battery_after=balance)
            )
        actions.append(
            Action(
                robot.robot_id,
                "charge",
                start,
                end,
                charger.location,
                charger.location,
                charger.id,
                gain=gain,
                battery_after=balance + gain,
            )
        )
        return commit_prefix(engine, self.table, tuple(actions))

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        """All decisions are handled in the overridden dispatch entry point."""

    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """Normal charging is explicitly scheduled; forced commands remain available."""
        return False

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """Provide a stable target for an explicit operator-forced legacy charge."""
        return min(100.0, max(config.MIN_CHARGE_TARGET, robot.cfg.charge_threshold))

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """Choose only physically reachable chargers for forced recovery commands."""
        candidates = []
        for c in engine.chargers.values():
            if c.active:
                distance = engine.path_graph.distance_to_facility(*robot.position(), c.id)
                drain = distance * config.METERS_PER_PIXEL * robot.cfg.battery_drain_rate
                if math.isfinite(distance) and drain <= robot.live_battery() + 1e-7:
                    candidates.append((distance, c.id, c))
        return min(candidates, default=(0, "", None))[2]
