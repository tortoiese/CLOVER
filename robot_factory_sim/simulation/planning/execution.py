"""Atomic first-service prefix admission and execution through the real plant."""

from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any

from simulation.path_graph import Route
from simulation.planning.contracts import Action, Arc, Result, Snapshot, Use
from simulation.planning.snapshot import capture, topology_hash
from simulation.planning.validation import validate
from simulation.task_generator import TaskState
from simulation.telemetry import observe_charge, observe_wait
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


def prefixes(result: Result) -> tuple[Action, ...]:
    """Choose only the first service/continuous charge, or a bounded leading wait."""
    snapshot = result.input.snapshot
    chosen = []
    for robot in snapshot.robots:
        if not robot.available:
            continue
        actions = sorted((a for a in result.actions if a.arc.robot == robot.id), key=lambda a: a.arc.start)
        meaningful = next((a for a in actions if a.arc.kind != "wait"), None)
        boundary = max(1, int(snapshot.settings.interval / snapshot.settings.time_unit))
        if meaningful is None or meaningful.arc.start >= boundary:
            first = actions[0].arc
            end = min(
                boundary,
                meaningful.arc.start if meaningful else int(snapshot.settings.horizon / snapshot.settings.time_unit),
            )
            chosen.append(Action(Arc(robot.id, first.origin, first.origin, 0, end, "wait")))
        elif meaningful.arc.kind == "charge":
            index = actions.index(meaningful)
            last, gain = meaningful, meaningful.gain
            for following in actions[index + 1 :]:
                if (
                    following.arc.kind != "charge"
                    or following.arc.origin != meaningful.arc.destination
                    or following.arc.destination != meaningful.arc.destination
                    or following.arc.start != last.arc.end
                ):
                    break
                last, gain = following, gain + following.gain
            uses = tuple(u for u in meaningful.arc.uses if u.resource != "charge:" + meaningful.arc.destination)
            uses += (
                Use("charge:" + meaningful.arc.destination, "slot", 1, meaningful.arc.service_start, last.arc.end),
            )
            chosen.append(Action(replace(meaningful.arc, end=last.arc.end, uses=uses), gain))
        else:
            chosen.append(meaningful)
    return tuple(chosen)


def commit(engine: SimulationEngine, result: Result, *, observed_snapshot: Snapshot | None = None) -> tuple[bool, str]:
    """Reject stale input, validate independently, then stage without waking robots."""
    snapshot = result.input.snapshot
    # A D1 caller may independently revalidate the original job cohort while
    # retaining unrelated arrivals in the real queue. Verify its full current
    # observation at this atomic boundary rather than pretending jobs vanished.
    if capture(engine, snapshot.settings, snapshot.token) != (observed_snapshot or snapshot):
        return False, "STALE"
    errors = validate(result)
    if errors:
        return False, "MODEL_INVALID: " + "; ".join(errors)
    chosen = prefixes(result)
    if not chosen:
        return False, "EMPTY_PREFIX"
    pending = {task.external_uid: task for task in engine.pending_tasks}
    assigned = {a.arc.robot: pending[a.arc.job] for a in chosen if a.arc.kind == "service"}
    saved_pending = list(engine.pending_tasks)
    saved_tasks = [
        (task, {f.name: copy.deepcopy(getattr(task, f.name)) for f in fields(task)}) for task in assigned.values()
    ]
    saved_robots = {
        a.arc.robot: (engine.robots[a.arc.robot].current_task, engine.robots[a.arc.robot].planned_action)
        for a in chosen
    }
    saved_metrics = copy.deepcopy(
        {key: value for key, value in engine.metrics.__dict__.items() if key != "sample_sink"}
    )
    saved_orders = {key: order.responded for key, order in engine.orders.items()}
    try:
        with engine.ledger.transaction():
            for action in chosen:
                robot = engine.robots[action.arc.robot]
                if not robot.is_available():
                    raise ValueError("robot no longer available")
                if action.arc.kind == "service":
                    task = assigned[robot.robot_id]
                    task.station_id = action.arc.destination
                    if not engine.assign(task, robot, wake=False):
                        raise ValueError("task no longer eligible")
                robot.planned_action = (result, action)
            engine.ledger.emit(
                "planning_prefix",
                token=snapshot.token,
                actions=[
                    {"robot": a.arc.robot, "kind": a.arc.kind, "job": a.arc.job, "start": a.arc.start, "end": a.arc.end}
                    for a in chosen
                ],
            )
    except Exception as exc:
        engine.pending_tasks[:] = saved_pending
        for task, state in saved_tasks:
            for key, value in state.items():
                setattr(task, key, value)
        for rid, (task, plan) in saved_robots.items():
            engine.robots[rid].current_task, engine.robots[rid].planned_action = task, plan
        for key, value in saved_metrics.items():
            setattr(engine.metrics, key, value)
        for key, responded in saved_orders.items():
            engine.orders[key].responded = responded
        return False, "COMMIT_ROLLED_BACK: " + str(exc)
    for action in chosen:
        engine.robots[action.arc.robot].wake()
    return True, "COMMITTED"


def execute(robot: Robot) -> Generator[Any, Any, None]:
    """Run the committed prefix; physical admission remains authoritative."""
    result, action = robot.planned_action
    snapshot, arc = result.input.snapshot, action.arc
    engine, step = robot.engine, result.input.snapshot.settings.time_unit
    token = snapshot.token
    completed = False
    try:
        start = snapshot.now + arc.start * step
        if engine.now < start:
            robot._set_state(RobotState.WAITING)
            yield engine.env.timeout(start - engine.now)
        if (
            engine.execution_epoch != snapshot.epoch
            or engine.layout_version != snapshot.layout_version
            or topology_hash(engine) != snapshot.topology
        ):
            robot._requeue_current()
            return
        engine.ledger.emit("planning_action_start", token=token, robot=robot.robot_id, kind=arc.kind, uid=arc.job)
        route = None
        timings = ()
        if arc.route is not None:
            data = result.input.routes[arc.route]
            route = Route(
                list(data.points),
                data.distance_m / 0.1,
                list(data.floors),
                list(data.lifts),
                list(data.nodes),
                list(data.previous_nodes),
            )
            timings = tuple(start + t for t in data.segment_starts)
        service_start = snapshot.now + arc.service_start * step
        if arc.kind == "wait":
            robot._set_state(RobotState.WAITING)
            yield engine.env.timeout(max(0, snapshot.now + arc.end * step - engine.now))
        elif arc.kind == "service":
            yield from robot._task_process(route, timings, service_start)
        elif arc.kind == "charge":
            charger = engine.chargers.get(arc.destination)
            if charger is None or not charger.active:
                return
            robot.target_id = charger.id
            charger.incoming.add(robot.robot_id)
            try:
                robot._set_state(RobotState.MOVING)
                if route is not None:
                    yield from robot._travel_route(route, timings)
                robot._mark_planned_route_complete()
            finally:
                charger.incoming.discard(robot.robot_id)
            if engine.now < service_start:
                robot._set_state(RobotState.WAITING)
                yield engine.env.timeout(service_start - engine.now)
            robot._set_state(RobotState.CHARGE_WAIT)
            since = engine.now
            with charger.resource.request() as request:
                yield from observe_wait(robot, request, "charge", "charge:" + charger.id)
                if not charger.active:
                    return
                engine.metrics.record_charge_wait(engine.now - since)
                robot._set_state(RobotState.CHARGING)
                robot.charge_sessions += 1
                charge_started, charge_completed = engine.now, False
                observe_charge(robot, charger.id, charge_started)
                charger.occupants.add(robot.robot_id)
                try:
                    gain = min(action.gain * 0.001, robot.capacity - robot.battery)
                    robot._planned_charge_until = engine.now + gain / charger.cfg.charge_rate
                    robot._charge_step = (engine.now, charger.cfg.charge_rate)
                    yield engine.env.timeout(gain / charger.cfg.charge_rate)
                    charge_completed = True
                finally:
                    robot._settle_charge()
                    robot._planned_charge_until = None
                    observe_charge(robot, charger.id, charge_started, completed=charge_completed)
                    charger.occupants.discard(robot.robot_id)
        completed = arc.kind != "service" or engine._tasks_by_uid[arc.job].status == TaskState.COMPLETED
    finally:
        engine.ledger.emit(
            "planning_action_end",
            token=token,
            robot=robot.robot_id,
            kind=arc.kind,
            uid=arc.job,
            finished=completed,
            delayed=engine.now > snapshot.now + arc.end * step + 1e-8,
        )
        robot.planned_action = None
        robot.target_id = None
