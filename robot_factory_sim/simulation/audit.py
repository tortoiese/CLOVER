"""Independent execution-state audits and energy accounting for headless evaluation."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

import config
from simulation.task_generator import TaskState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def energy_ledger(engine: SimulationEngine) -> dict[str, Any]:
    """Include live movement/work/charging and explicitly injected rescue energy."""
    rows = []
    for robot in [*engine.robots.values(), *engine.retired]:
        travel, work, charged = robot.travel_energy_units, robot.work_energy_units, robot.charged_energy_units
        distance = robot.distance_m
        if robot._segment is not None:
            x0, y0, x1, y1, start, end, drain = robot._segment
            fraction = robot._segment_fraction(start, end)
            travel += min(robot.battery, drain * fraction)
            distance += ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5 * config.METERS_PER_PIXEL * fraction
        if robot._work_started is not None:
            work += min(robot.battery, engine.plant.work_drain_per_sec * (engine.now - robot._work_started))
        if robot._charge_step is not None:
            started, rate = robot._charge_step
            charged += min(robot.capacity - robot.battery, max(0, rate * (engine.now - started)))
        balance = (
            robot.initial_energy_units
            + charged
            + robot.rescue_energy_units
            + robot.capacity_adjustment_units
            - travel
            - work
            - robot.live_battery()
        )
        rows.append(
            {
                "robot": robot.robot_id,
                "initial": robot.initial_energy_units,
                "charged": charged,
                "rescue": robot.rescue_energy_units,
                "capacity_adjustment": robot.capacity_adjustment_units,
                "travel": travel,
                "work": work,
                "remaining": robot.live_battery(),
                "distance_m": distance,
                "balance_error": balance,
            }
        )
    keys = ("initial", "charged", "rescue", "capacity_adjustment", "travel", "work", "remaining", "distance_m")
    totals = {key: sum(row[key] for row in rows) for key in keys}
    return {
        **totals,
        "consumed": totals["travel"] + totals["work"],
        "robots": rows,
        "max_balance_error": max((abs(row["balance_error"]) for row in rows), default=0),
    }


def audit_engine(engine: SimulationEngine) -> dict[str, Any]:
    """Check real queues/owners against the logical registry, plus physical budgets."""
    errors = []
    queued = Counter(t.task_id for t in engine.pending_tasks)
    active: Counter[int] = Counter()
    reserved: Counter[int] = Counter()
    robots = [*engine.robots.values(), *engine.retired]
    for robot in robots:
        task = robot.current_task
        if task is not None and task.status not in TaskState.TERMINAL:
            active[task.task_id] += 1
            if task.robot_id != robot.robot_id:
                errors.append("task owner mismatch")
        pending = robot._pending_control
        task = pending.get("task") if pending else None
        if task is not None and task.status not in TaskState.TERMINAL and task is not robot.current_task:
            reserved[task.task_id] += 1
    counts = Counter(task.status for task in engine.tasks.values())
    if (set(queued) | set(active) | set(reserved)) - set(engine.tasks):
        errors.append("live task missing from registry")
    for task in engine.tasks.values():
        locations = queued[task.task_id] + active[task.task_id] + reserved[task.task_id]
        expected = 0 if task.status in TaskState.TERMINAL else 1
        if locations != expected:
            errors.append(f"task {task.external_uid}: {locations} live locations, expected {expected}")
        if task.status == TaskState.QUEUED and queued[task.task_id] != 1:
            errors.append("logical queue mismatch")
        if task.status == TaskState.RESERVED and reserved[task.task_id] != 1:
            errors.append("logical reservation mismatch")
    for pool in engine.corridors._pools.values():
        if pool.count < 0 or any(claim.units <= 0 or claim.request.closed for claim in pool.users):
            errors.append("invalid live traffic claim")
        for claim in pool.users:
            request = claim.request
            if not all(owned in resource.users for resource, owned in request.claims.items()):
                errors.append("partially held traffic bundle")
        if pool.count > pool.capacity and not (engine.corridors._paused or pool.capacity == 0):
            errors.append("traffic capacity exceeded")
    energy = energy_ledger(engine)
    if energy["max_balance_error"] > 1e-7:
        errors.append("energy conservation mismatch")
    if any(not -1e-8 <= r.live_battery() <= r.capacity + 1e-8 for r in robots):
        errors.append("battery outside physical bounds")
    return {
        "ok": not errors,
        "errors": sorted(set(errors)),
        "offered": len(engine.tasks),
        "counts": dict(counts),
        "energy": energy,
    }


def assert_invariants(engine: SimulationEngine) -> dict[str, Any]:
    """Raise on invalid execution state and return an auditable snapshot otherwise."""
    report = audit_engine(engine)
    if not report["ok"]:
        raise AssertionError("; ".join(report["errors"]))
    return report
