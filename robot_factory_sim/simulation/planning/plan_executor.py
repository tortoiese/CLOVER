"""Commit only each robot's next complete work/charging prefix."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from simulation.planning.models import Action, Plan
from simulation.planning.reservation_table import ReservationTable

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def next_prefix(actions: tuple[Action, ...]) -> tuple[Action, ...]:
    """Include travel and waiting with its first actual work or charge session."""
    prefix = []
    for i, action in enumerate(actions):
        if action.kind == "charge" and action.gain <= 1e-7:
            prefix.append(replace(action, kind="wait", resource="", gain=0.0))
            continue
        prefix.append(action)
        if action.kind == "work":
            return tuple(prefix)
        if action.kind == "charge":
            merged = action
            for following in actions[i + 1 :]:
                if (
                    following.kind != "charge"
                    or following.resource != action.resource
                    or abs(following.start - merged.end) > 1e-6
                ):
                    break
                merged = replace(
                    merged, end=following.end, gain=merged.gain + following.gain, battery_after=following.battery_after
                )
            prefix[-1] = merged
            return tuple(prefix)
    return ()


def commit_prefix(engine: SimulationEngine, table: ReservationTable, prefix: tuple[Action, ...]) -> bool:
    """Reserve a prefix and remove its task atomically on the simulation thread."""
    if not prefix:
        return False
    robot = engine.robots.get(prefix[0].robot_id)
    if robot is None or not robot.is_available():
        return False
    work = next((a for a in prefix if a.kind == "work"), None)
    task = next((t for t in engine.pending_tasks if work and t.task_id == work.job_id), None)
    if work is not None and (task is None or not engine.station_available(work.resource)):
        return False
    if task is not None:
        if work.resource not in (task.allowed_stations or (task.station_id,)):
            return False
        engine._remove_pending(task)
        task.station_id = work.resource
        task.assigned_at = engine.now
        task.robot_id = robot.robot_id
        order = engine.orders.get(task.order_id) if task.order_id else None
        if order is not None and order.detected_at is not None and not order.responded:
            order.responded = True
            engine.metrics.record_response(engine.now - order.detected_at)
    table.commit(robot.robot_id, prefix)
    robot.assign_planned(prefix, task)
    engine.planning_revision += 1
    return True


def execute_plan(engine: SimulationEngine, table: ReservationTable, plan: Plan) -> int:
    """Commit starting actions within the short execution window, never all jobs."""
    committed = 0
    for rid in sorted({a.robot_id for a in plan.actions}):
        actions = tuple(a for a in plan.actions if a.robot_id == rid)
        prefix = next_prefix(actions)
        first = next((a for a in prefix if a.kind != "wait"), None)
        if first is not None and first.start <= engine.now + plan.snapshot.settings.interval + 1e-6:
            committed += int(commit_prefix(engine, table, prefix))
    return committed
