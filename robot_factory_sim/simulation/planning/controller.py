"""Rolling-horizon request, validation, prefix commitment and safe recovery."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from simulation.planning.contracts import (
    Action,
    Arc,
    CabinArc,
    Report,
    Reservation,
    Result,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    energy_down,
    energy_up,
)
from simulation.planning.execution import commit, select_prefix
from simulation.planning.process import SolverProcess, solve_request
from simulation.planning.snapshot import Request, capture, prepare, topology_hash
from simulation.planning.units import decimal
from simulation.planning.validation import (
    capacity_errors,
    reservations_for,
    return_requirement,
    validate,
)
from simulation.scheduler import BaseScheduler

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot
    from simulation.station import ChargingStation


class RollingHorizonScheduler(BaseScheduler):
    """Receding-horizon service and optional traffic/cabin planning."""

    planning_active = True

    def __init__(self, backend: str = "milp", settings: Settings | None = None, asynchronous: bool = False) -> None:
        self.backend = backend
        self.name = "rh_" + backend
        self.settings = settings or Settings()
        self.settings.validate()
        self.process = SolverProcess(backend) if asynchronous else None
        self.blocking_process: SolverProcess | None = None
        self.cancelled = None
        self.reservations: list[Reservation] = []
        self.charge_intents: set[str] = set()
        self.charging_sessions: dict[str, set[str]] = {}
        self.busy_lifts: dict[str, set[str]] = {}
        self.epoch = uuid.uuid4().hex
        self.serial = 0
        self.next_solve = 0.0
        self.dirty = True
        self.pending: Request | None = None
        self.last_result: Result | None = None
        self.last_execution = "WAITING"
        self.solves = self.accepted = self.fallbacks = self.stale = self.invalid = self.overruns = 0
        self.audit: list[dict] = []
        self.reports: list[Report] = []
        self._last_queue: tuple[int, ...] = ()
        self.candidate_count = self.total_pending = 0
        self.acceptance_events: list[dict] = []
        self.finished_requests = self.ineligible_requests = self.cancelled_requests = 0

    def on_delay(self, engine: SimulationEngine, owner: str) -> None:
        """Invalidate waiting prefixes on an overrun; retain occupied physical actions."""
        from simulation.robot import InterruptKind
        from utils.event_types import RobotState
        self.overruns += 1
        self.next_solve = 0.0
        for robot in engine.robot_list():
            if (robot.robot_id != owner and robot.planned_actions and robot.state == RobotState.WAITING and
                    robot._segment is None and robot._riding is None and robot._charge_step is None):
                robot.interrupt(InterruptKind.STATION_DOWN)

    def notify_event(self, reason: str) -> None:
        """Invalidate in-flight computation on an observed critical state change."""
        self.dirty = True
        self.next_solve = 0
        if self.pending is not None:
            self.cancelled_requests += 1
            self.audit.append({"token": self.pending.snapshot.token, "reason": reason, "action": "INVALIDATED"})
            if self.process is not None:
                self.process.close()
            self.pending = None
        self.last_execution = "INVALIDATED_" + reason.upper()
    def close(self) -> None:
        """Stop owned solver processes before discarding an engine."""
        if self.process:
            self.process.close()
        if self.blocking_process:
            self.blocking_process.close()
        self.pending = None

    def release(self, robot_id: str, charge_completed: bool = False) -> None:
        """Release a finished or interrupted prefix and re-plan on availability."""
        self.reservations = [r for r in self.reservations if r.robot != robot_id]
        self.charging_sessions.pop(robot_id, None)
        if charge_completed:
            self.charge_intents.discard(robot_id)
        for owners in self.busy_lifts.values():
            owners.discard(robot_id)
        self.dirty = True
        self.next_solve = 0.0

    def request_charge(self, robot_id: str) -> bool:
        """Queue an operator intent; never bypass prefix validation or resources."""
        self.charge_intents.add(robot_id)
        self.dirty = True
        self.next_solve = 0.0
        return True

    def planning_state(self) -> dict:
        """Expose precise model scope, phase status, bounds and acceptance counters."""
        return {"scope": self.settings.scope,
                "traffic": "planned_resources" if self.settings.scope == "traffic" else "runtime_resources",
                "pending": self.pending is not None, "execution_status": self.last_execution,
                "report": asdict(self.last_result.report) if self.last_result else None,
                "solves": self.solves, "accepted": self.accepted, "fallbacks": self.fallbacks,
                "stale": self.stale, "invalid": self.invalid, "overruns": self.overruns,
                "candidates": self.candidate_count, "total_pending": self.total_pending,
                "reservations": [asdict(r) for r in self.reservations],
                "reason": self.audit[-1].get("reason", "") if self.audit else
                self.last_result.report.message if self.last_result else "",
                "settings": asdict(self.settings),
                "quality": {"eligible_completed": self.finished_requests - self.ineligible_requests,
                            "normal_batches": self.accepted, "fallback_or_hold_batches": self.fallbacks,
                            "unsupported": self.ineligible_requests, "invalidated_requests": self.cancelled_requests,
                            "normal_acceptance_rate": self.accepted / (self.finished_requests - self.ineligible_requests)
                            if self.finished_requests > self.ineligible_requests else None,
                            "fallback_rate": self.fallbacks / (self.finished_requests - self.ineligible_requests)
                            if self.finished_requests > self.ineligible_requests else None}}

    def dispatch(self, engine: SimulationEngine) -> None:
        """Poll nonblocking GUI work, otherwise solve at a bounded simulation interval."""
        self.charge_intents.difference_update(r.robot_id for r in engine.robot_list() if r.is_available() and
                                             r.battery_pct >= max(75, r.cfg.charge_threshold) - 1e-6)
        if self.process:
            result = self.process.poll()
            if result:
                request, self.pending = self.pending, None
                self._accept(engine, request, result)
        queue = tuple(t.task_id for t in engine.pending_tasks)
        if queue and not self._last_queue:
            self.next_solve = min(self.next_solve, engine.now)
        self._last_queue = queue
        if self.pending is not None or engine.now + 1e-7 < self.next_solve:
            return
        self.next_solve = engine.now + self.settings.interval
        self.serial += 1
        request = capture(engine, self.settings, f"{self.epoch}:{self.serial}", tuple(self.reservations),
                          self.charge_intents)
        self.candidate_count = len(request.snapshot.jobs)
        self.total_pending = len(engine.pending_tasks)
        if not request.snapshot.robots:
            return
        if not queue and not self.charge_intents and all(r.battery >= r.reserve for r in request.snapshot.robots):
            return
        self.solves += 1
        if self.process:
            self.pending = request
            try:
                self.process.submit(request)
            except Exception as exc:  # noqa: BLE001 — a subprocess failure cannot stop the engine
                self.pending = None
                self._accept(engine, request, Result(request.snapshot, Report(self.backend, "PROCESS_ERROR", 0,
                                                                              message=str(exc))))
        else:
            result = (self.blocking_process.solve(request, self.cancelled) if self.blocking_process else
                      solve_request(request, self.backend))
            self._accept(engine, request, result)

    def _accept(self, engine: SimulationEngine, request: Request, result: Result) -> None:
        if request is None or request.snapshot.token != result.snapshot.token:
            self.stale += 1
            return
        self.finished_requests += 1
        self.last_result = result
        self.reports.append(result.report)
        self.reports = self.reports[-1000:]
        self.last_execution = result.report.status
        lag = engine.now - result.snapshot.now
        current = capture(engine, self.settings, request.snapshot.token, tuple(self.reservations), self.charge_intents)
        old_jobs = {j.id: (j.stations, j.order_id, j.deadline, j.priority) for j in result.snapshot.jobs}
        new_jobs = {t.task_id: (() if t.planning_blocked else tuple(engine._usable_stations(t)), t.order_id,
                              t.deadline, t.priority)
                    for t in engine.pending_tasks}
        new_critical = {t.task_id for t in engine.pending_tasks if t.priority == 0}
        stable = (request.snapshot.settings == self.settings and 0 <= lag <= self.settings.interval
                  and topology_hash(engine) == result.snapshot.topology
                  and all(r in current.snapshot.robots for r in result.snapshot.robots)
                  and all(p in current.positions for p in request.positions)
                  and (result.snapshot.scope != "traffic" or current.snapshot.cabins == result.snapshot.cabins)
                  and new_critical.issubset(old_jobs)
                  and all(new_jobs.get(j) == value for j, value in old_jobs.items()))
        if not stable:
            self.stale += 1
            self.last_execution = "STALE_REJECTED"
            self._acceptance(engine, result, "STALE_REJECTED", lag)
            self._fallback(engine, current)
            return
        if result.report.status == "UNSUPPORTED":
            self.ineligible_requests += 1
            self.audit.append({"time": engine.now, "token": result.snapshot.token,
                               "reason": result.report.message, "action": "HOLD"})
            return
        if result.report.status not in ("OPTIMAL", "FEASIBLE"):
            self._fallback(engine, current, result.snapshot)
            return
        shifted = replace(result, snapshot=replace(result.snapshot, now=engine.now, fixed=current.snapshot.fixed),
                          actions=tuple(replace(a, start=a.start + lag, end=a.end + lag) for a in result.actions))
        errors = validate(shifted)
        if errors:
            self.invalid += 1
            self.last_execution = "INVALID_REJECTED"
            self._acceptance(engine, result, "INVALID_REJECTED", lag, str(errors))
            self.audit.append({"token": result.snapshot.token, "errors": errors})
            self.audit = self.audit[-500:]
            self._fallback(engine, current, result.snapshot)
            return
        if commit(engine, select_prefix(shifted)):
            self.accepted += 1
            self.last_execution = "SHIFTED_FEASIBLE" if lag > 1e-7 else "ACCEPTED_" + result.report.status
            self._acceptance(engine, result, self.last_execution, lag)
        elif shifted.snapshot.jobs or self.charge_intents:
            self.last_execution = "NO_COMMITTABLE_PREFIX"
            self._fallback(engine, current, result.snapshot)

    def _acceptance(self, engine: SimulationEngine, result: Result, status: str,
                    shift: float = 0, reason: str = "") -> None:
        self.acceptance_events.append({"token": result.snapshot.token, "time": engine.now,
                                       "model_hash": result.report.model_hash, "status": status,
                                       "shift_seconds": shift, "reason": reason})
        self.acceptance_events = self.acceptance_events[-5000:]

    def _fallback(self, engine: SimulationEngine, request: Request, prepared: Snapshot | None = None) -> None:
        """Construct resource-checked single-job or charge recovery prefixes."""
        self.fallbacks += 1
        self.audit.append({"time": engine.now, "token": request.snapshot.token, "reason": self.last_execution,
                           "solver_status": self.last_result.report.status if self.last_result else "NONE",
                           "action": "FALLBACK_OR_HOLD"})
        self.audit = self.audit[-5000:]
        if prepared is not None and prepared.routes:
            snapshot = replace(prepared, now=engine.now, fixed=request.snapshot.fixed)
        elif self.process is not None:
            # A stale/crashed worker cannot justify rebuilding paths on SimWorker.
            # Hold safely and obtain a fresh detached request on the next tick.
            self.next_solve = engine.now
            return
        else:
            snapshot = prepare(request)
        routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
        facilities = {f.id: f for f in snapshot.facilities}
        fixed = list(snapshot.fixed)
        used = set()
        for robot in snapshot.robots:
            best = None
            if not robot.charge_only:
                for job in sorted(snapshot.jobs, key=lambda j: (-j.weight, j.created, j.id)):
                    if job.id in used or robot.emergency_order is not None and job.order_id != robot.emergency_order:
                        continue
                    for sid in job.stations:
                        route, station = routes.get((robot.id, robot.location, sid)), facilities.get(sid)
                        if route is None or station is None or any(self.busy_lifts.get(r.cabin) for r in route.rides):
                            continue
                        travel = max(snapshot.settings.step, math.ceil(route.seconds / snapshot.settings.step)
                                     * snapshot.settings.step)
                        duration = math.ceil(station.duration / snapshot.settings.step) * snapshot.settings.step
                        drain = energy_up(decimal(route.distance_m) * decimal(robot.drain))
                        work = energy_up(decimal(duration) * decimal(snapshot.work_drain))
                        back = return_requirement(snapshot, robot.id, sid)
                        if back is None or robot.battery < drain + work + robot.reserve + back:
                            continue
                        arrival = snapshot.now + travel
                        start = self._slot(tuple(fixed), sid, arrival, duration, station.capacity)
                        cost = (-job.weight, start + duration, job.id, sid)
                        if best is None or cost < best[0]:
                            best = (cost, job, station, route, travel, duration, start, drain, work)
            if best is not None:
                _, job, facility, route, travel, duration, start, drain, work = best
                actions = self._movement(snapshot.now, robot, facility.id, route, travel, drain)
                arrival = snapshot.now + travel
                if start > arrival:
                    actions.append(Action(Arc(robot.id, facility.id, facility.id, 0, 1, "wait"), arrival, start))
                actions.append(Action(Arc(robot.id, facility.id, facility.id, 0, 1, "work", facility.id, job.id, work),
                                      start, start + duration))
                used.add(job.id)
            else:
                recovery = []
                for facility in snapshot.facilities:
                    route = routes.get((robot.id, robot.location, facility.id))
                    if (facility.kind != "charge" or route is None or facility.rate <= 0 or
                            any(self.busy_lifts.get(r.cabin) for r in route.rides)):
                        continue
                    drain = energy_up(decimal(route.distance_m) * decimal(robot.drain))
                    if drain > robot.battery:
                        continue
                    target = max(robot.reserve, robot.charge_target, math.floor(robot.capacity * .75))
                    gain = min(robot.capacity, target) - (robot.battery - drain)
                    if gain <= 0:
                        continue
                    travel = max(1, math.ceil(route.seconds))
                    duration = math.ceil(gain / facility.rate)
                    start = self._slot(tuple(fixed), facility.id, snapshot.now + travel, duration, facility.capacity)
                    recovery.append((start + duration, facility.id, facility, route, travel, drain, gain, start, duration))
                if not recovery:
                    self.audit.append({"token": snapshot.token, "robot": robot.id, "reason": "HOLD_NO_SAFE_ACTION"})
                    self.audit = self.audit[-500:]
                    continue
                _, _, facility, route, travel, drain, gain, start, duration = min(recovery)
                actions = self._movement(snapshot.now, robot, facility.id, route, travel, drain)
                if start > snapshot.now + travel:
                    actions.append(Action(Arc(robot.id, facility.id, facility.id, 0, 1, "wait"),
                                          snapshot.now + travel, start))
                actions.append(Action(Arc(robot.id, facility.id, facility.id, 0, 1, "charge", facility.id),
                                      start, start + duration, gain))
            local = replace(snapshot, fixed=tuple(fixed))
            # A conservative traffic fallback shifts the entire prefix and builds
            # a genuine cabin path. It never calls a legacy charge/EV shortcut.
            candidate = self._fallback_plan(local, tuple(actions), route)
            if commit(engine, candidate):
                fixed.extend(reservations_for(candidate.actions, candidate.snapshot))

    @staticmethod
    def _movement(now: float, robot: RobotInput, destination: str, route: RouteInput,
                  travel: float, drain: int) -> list[Action]:
        return [Action(Arc(robot.id, robot.location, destination, 0, 1, "move", energy=drain,
                           distance_m=route.distance_m), now, now + travel)]

    def _fallback_plan(self, snapshot: Snapshot, actions: tuple[Action, ...], route: RouteInput) -> Result:
        report = Report(self.backend, "FALLBACK", 0, scope=snapshot.scope)
        if snapshot.scope != "traffic":
            return Result(snapshot, report, actions)
        capacities = {f.id: f.capacity for f in snapshot.facilities} | {r.id: r.capacity for r in snapshot.resources}
        step = snapshot.settings.step
        # Retrying at every reservation boundary is sufficient after all fixed
        # intervals end; include cabin repositioning time as an initial delay.
        definitions = {c.id: c for c in snapshot.cabins}
        minimum_delay = max([0, *(math.ceil(abs(definitions[r.cabin].floor - r.origin) *
                                            definitions[r.cabin].travel_seconds / step) - r.start
                                  for r in route.rides)]) * step
        starts = sorted({minimum_delay, *(max(minimum_delay, math.ceil((r.end - snapshot.now) / step) * step)
                                          for r in snapshot.fixed)})
        for delay in starts:
            shifted = tuple(replace(a, start=a.start + delay, end=a.end + delay) for a in actions)
            if delay:
                first = actions[0].arc
                shifted = (Action(Arc(first.robot, first.origin, first.origin, 0, 1, "wait"),
                                  snapshot.now, snapshot.now + delay), *shifted)
            cabins = []
            for cid in {r.cabin for r in route.rides}:
                cabin = definitions[cid]
                floor = cabin.floor
                tick = 0
                for ride in sorted((r for r in route.rides if r.cabin == cid), key=lambda r: r.start):
                    ride_start = ride.start + round(delay / step)
                    duration = math.ceil(abs(floor - ride.origin) * cabin.travel_seconds / step)
                    if duration:
                        cabins.append(CabinArc(cid, floor, ride.origin, tick, tick + duration, "empty"))
                        tick += duration
                    if tick < ride_start:
                        cabins.append(CabinArc(cid, ride.origin, ride.origin, tick, ride_start, "wait"))
                    cabins.append(replace(ride, start=ride_start, end=ride.end + round(delay / step)))
                    floor, tick = ride.destination, ride.end + round(delay / step)
            candidate = Result(snapshot, report, shifted, cabins=tuple(cabins))
            if not capacity_errors(reservations_for(shifted, snapshot), capacities, snapshot.fixed) and not validate(
                    candidate, prefix=True):
                return candidate
        return Result(snapshot, report)

    @staticmethod
    def _slot(fixed: tuple[Reservation, ...], resource: str, earliest: float, duration: float, capacity: int) -> float:
        candidates = sorted({earliest, *(r.end for r in fixed if r.resource == resource and r.end >= earliest)})
        for start in candidates:
            extra = Reservation(resource, "@new", start, start + duration)
            if not capacity_errors((extra,), {resource: capacity}, fixed):
                return start
        return max(candidates)

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        """Dispatch is implemented by the single planning entry point."""

    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """All normal and forced charging uses planning intents."""
        return False

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """Compatibility hook; direct legacy charging is disabled for this policy."""
        return max(75.0, robot.cfg.charge_threshold)

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """Compatibility hook; the common model chooses chargers."""
        return None

    def work_is_safe(self, robot: Robot, station_id: str, duration: float) -> bool:
        """Check realized service energy only when work starts, without leaking samples."""
        snapshot = robot.planned_snapshot
        if snapshot is None:
            return False
        back = return_requirement(snapshot, robot.robot_id, station_id)
        observed = next(r for r in snapshot.robots if r.id == robot.robot_id)
        needed = energy_up(decimal(duration) * decimal(snapshot.work_drain)) + observed.reserve + (back or 0)
        if back is not None and energy_down(robot.live_battery()) >= needed:
            return True
        if needed <= energy_up(robot.capacity):
            self.request_charge(robot.robot_id)
        elif robot.current_task is not None:
            robot.current_task.planning_blocked = "ACTUAL_SERVICE_EXCEEDS_BATTERY"
            self.audit.append({"job": robot.current_task.task_id, "reason": "ACTUAL_SERVICE_EXCEEDS_BATTERY"})
        return False
