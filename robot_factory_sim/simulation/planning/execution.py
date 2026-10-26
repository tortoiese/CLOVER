"""Atomic prefix admission and execution through the real SimPy resources."""

from __future__ import annotations

import copy
from collections.abc import Generator
from dataclasses import fields, replace
from typing import TYPE_CHECKING, Any

from simulation.path_graph import Route
from simulation.planning.contracts import ENERGY_QUANTUM, Result
from simulation.planning.snapshot import topology_hash
from simulation.planning.validation import reservations_for, validate
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


def select_prefix(result: Result) -> Result:
    """Commit a service prefix or its leading wait, leaving later jobs unassigned."""
    chosen = []
    for robot in result.snapshot.robots:
        actions = sorted((a for a in result.actions if a.arc.robot == robot.id), key=lambda a: a.start)
        meaningful = next((a for a in actions if a.arc.kind in ("work", "charge")), None)
        first_move = next((a for a in actions if a.arc.kind != "wait"), None)
        if meaningful is None or first_move is None:
            # A fully deferred horizon needs explicit recovery, especially for
            # jobs longer than H or a charge/work chain that cannot fit H.
            continue
        boundary = result.snapshot.now + result.snapshot.settings.interval
        if first_move.start >= boundary:
            # A valid optimal decision may deliberately wait for an occupied
            # cabin/resource. Execute only the observed leading wait, without
            # reserving or assigning any job in the uncommitted suffix.
            for action in actions:
                if action.arc.kind != "wait" or action.start >= boundary:
                    break
                end = min(action.end, boundary)
                chosen.append(replace(action, end=end))
            continue
        index = actions.index(meaningful)
        if meaningful.arc.kind == "charge":
            while index + 1 < len(actions) and actions[index + 1].arc.kind == "charge":
                index += 1
        chosen.extend(actions[:index + 1])
    needed = reservations_for(tuple(chosen), result.snapshot)
    limits = {}
    for reservation in needed:
        if reservation.resource.startswith("elevator:"):
            cid = reservation.resource.removeprefix("elevator:")
            limits[cid] = max(limits.get(cid, 0), round((reservation.end - result.snapshot.now)
                                                      / result.snapshot.settings.step))
    cabins = tuple(replace(a, kind="empty") if a.kind == "ride" and not any(
        r.resource == "elevator:" + a.cabin and abs(r.start - (result.snapshot.now + a.start *
        result.snapshot.settings.step)) < 1e-6 and abs(r.end - (result.snapshot.now + a.end *
        result.snapshot.settings.step)) < 1e-6 for r in needed) else a for a in result.cabins
                   if a.cabin in limits and a.end <= limits[a.cabin])
    return replace(result, actions=tuple(chosen), cabins=cabins)


def commit(engine: SimulationEngine, result: Result) -> bool:
    """Validate all robots/jobs first, then mutate queues and reservations together."""
    if not result.actions or validate(result, prefix=True):
        return False
    scheduler = engine.scheduler
    grouped = {rid: tuple(replace(a, action_id=a.action_id or f"{result.snapshot.token}:{rid}:{i}")
                         for i, a in enumerate(result.actions) if a.arc.robot == rid)
               for rid in {a.arc.robot for a in result.actions}}
    pending = {t.task_id: t for t in engine.pending_tasks}
    assignment = {}
    for rid, actions in grouped.items():
        robot = engine.robots.get(rid)
        if robot is None or not robot.is_available():
            return False
        work = next((a for a in actions if a.arc.kind == "work"), None)
        if work:
            task = pending.get(work.arc.job)
            if task is None or work.arc.resource not in engine._usable_stations(task):
                return False
            assignment[rid] = (task, work.arc.resource)
    # Do not trigger SimPy events until every assignment and reservation is staged.
    # A fault during staging restores objects in place, including external Task refs.
    saved_pending = list(engine.pending_tasks)
    saved_reservations = list(scheduler.reservations)
    saved_lifts = copy.deepcopy(scheduler.busy_lifts)
    saved_responses = list(engine.metrics.response_times)
    saved_log_length = len(engine.task_events)
    saved_orders = {key: order.responded for key, order in engine.orders.items()}
    saved_tasks = [(task, {f.name: copy.deepcopy(getattr(task, f.name)) for f in fields(task)})
                   for task, _ in assignment.values()]
    saved_robots = {rid: (engine.robots[rid].current_task, engine.robots[rid].planned_actions,
                         engine.robots[rid].planned_snapshot) for rid in grouped}
    try:
        scheduler.reservations.extend(reservations_for(result.actions, result.snapshot))
        for rid, actions in sorted(grouped.items()):
            robot = engine.robots[rid]
            robot.planned_actions = list(actions)
            robot.planned_snapshot = result.snapshot
            if rid in assignment:
                task, station = assignment[rid]
                task.station_id = station
                engine.assign(task, robot, wake=False)
                if robot.current_task is not task:
                    raise ValueError("assignment no longer eligible")
        for cid in {c.cabin for c in result.cabins}:
            riders = {r.robot for r in reservations_for(result.actions, result.snapshot)
                      if r.resource == "elevator:" + cid}
            scheduler.busy_lifts.setdefault(cid, set()).update(riders)
    except Exception as exc:  # noqa: BLE001 — restore the full admission transaction
        engine.pending_tasks[:] = saved_pending
        scheduler.reservations[:] = saved_reservations
        scheduler.busy_lifts = saved_lifts
        engine.metrics.response_times.clear()
        engine.metrics.response_times.extend(saved_responses)
        del engine.task_events[saved_log_length:]
        for key, responded in saved_orders.items():
            engine.orders[key].responded = responded
        for task, state in saved_tasks:
            for name, value in state.items():
                setattr(task, name, value)
        for rid, state in saved_robots.items():
            robot = engine.robots[rid]
            robot.current_task, robot.planned_actions, robot.planned_snapshot = state
        scheduler.audit.append({"token": result.snapshot.token, "reason": "COMMIT_ROLLED_BACK", "error": str(exc)})
        return False
    for cid in {c.cabin for c in result.cabins}:
        engine.env.process(execute_cabins(engine, result, cid))
    for rid in grouped:
        if rid in assignment:
            engine._task_event(assignment[rid][0], "assigned", robot=rid)
        engine.robots[rid].wake()
    engine.planning_event("prefix_committed", token=result.snapshot.token, model_hash=result.report.model_hash,
                          reservations=[{"resource": r.resource, "robot": r.robot, "start": r.start, "end": r.end,
                                         "amount": r.amount} for r in reservations_for(result.actions, result.snapshot)])
    return True


def execute(robot: Robot) -> Generator[Any, Any, None]:
    """Execute nominal timestamps while retaining physical resources on overruns."""
    engine = robot.engine
    snapshot = robot.planned_snapshot
    routes = {(r.origin, r.destination): r for r in snapshot.routes if r.robot == robot.robot_id}
    active_action = None
    try:
        while robot.planned_actions:
            if topology_hash(engine) != snapshot.topology:
                robot._requeue_current()
                break
            action = robot.planned_actions.pop(0)
            arc = action.arc
            if engine.now < action.start:
                robot._set_state(RobotState.WAITING)
                yield robot.env.timeout(action.start - engine.now)
            active_action = action
            engine.planning_event("action_start", token=snapshot.token, action_id=action.action_id,
                                  robot=robot.robot_id, kind=arc.kind, task_id=arc.job,
                                  planned_start=action.start, planned_end=action.end, battery=robot.live_battery())
            if arc.kind == "wait":
                robot._set_state(RobotState.WAITING)
                if engine.now < action.end:
                    yield robot.env.timeout(action.end - engine.now)
            elif arc.kind == "move":
                data = routes[arc.origin, arc.destination]
                robot.target_id = arc.destination
                robot._set_state(RobotState.MOVING)
                route = Route(list(data.points), data.distance_m, list(data.floors), list(data.lifts),
                              list(data.nodes), list(data.prev_nodes))
                timing = tuple(action.start + t * snapshot.settings.step for t in data.segment_starts)
                yield from robot._travel_route(route, timing if snapshot.scope == "traffic" else ())
            elif arc.kind == "work":
                if robot.current_task is not None:
                    yield from robot._task_process()
            elif arc.kind == "charge":
                charger = engine.chargers.get(arc.resource)
                if charger is None or not charger.active:
                    break
                robot.target_id = charger.id
                robot._set_state(RobotState.CHARGE_WAIT)
                before = engine.now
                with charger.resource.request() as request:
                    yield request
                    if not charger.active:
                        break
                    engine.metrics.record_charge_wait(engine.now - before)
                    robot._set_state(RobotState.CHARGING)
                    if charger.id not in engine.scheduler.charging_sessions.get(robot.robot_id, set()):
                        robot.charge_sessions += 1
                        engine.scheduler.charging_sessions.setdefault(robot.robot_id, set()).add(charger.id)
                    charger.occupants.add(robot.robot_id)
                    try:
                        rate = max(.01, charger.cfg.charge_rate)
                        gain = min(action.gain * ENERGY_QUANTUM, robot.capacity - robot.battery)
                        robot._charge_step = (engine.now, rate)
                        yield robot.env.timeout(gain / rate)
                        robot._charge_step = None
                        robot.battery += gain
                        robot.charge_energy_units += gain
                    finally:
                        charger.occupants.discard(robot.robot_id)
            if engine.now > action.end + 1e-6:
                engine.scheduler.on_delay(engine, robot.robot_id)
            engine.planning_event("action_end", token=snapshot.token, action_id=action.action_id,
                                  robot=robot.robot_id, kind=arc.kind, task_id=arc.job, battery=robot.live_battery())
            active_action = None
    finally:
        if active_action is not None:
            engine.planning_event("action_interrupted", token=snapshot.token, action_id=active_action.action_id,
                                  robot=robot.robot_id, kind=active_action.arc.kind, battery=robot.live_battery())
        robot.planned_actions = []
        robot.planned_snapshot = None
        robot.target_id = None
        engine.scheduler.release(robot.robot_id, robot.battery_pct >= max(75, robot.cfg.charge_threshold) - 1e-6)


def execute_cabins(engine: SimulationEngine, result: Result, cabin_id: str) -> Generator[Any, Any, None]:
    """Run only empty repositioning; robots execute linked loaded rides themselves."""
    for arc in sorted(result.cabins, key=lambda a: (a.start, a.cabin)):
        if arc.kind != "empty" or arc.cabin != cabin_id:
            continue
        if not engine.scheduler.busy_lifts.get(cabin_id):
            return
        start = result.snapshot.now + arc.start * result.snapshot.settings.step
        if engine.now < start:
            yield engine.env.timeout(start - engine.now)
        elevator = engine.elevators.get(arc.cabin)
        if elevator is None or not elevator.active:
            continue
        with elevator.resource.request() as request:
            yield request
            yield from elevator._move(float(arc.destination))
