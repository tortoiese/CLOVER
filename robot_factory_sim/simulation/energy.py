"""One live physical distance/energy ledger for KPI, experiments and economics."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import config

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def measure(engine: SimulationEngine) -> dict[str, float]:
    """Include partial movement, work and charging, plus retired robot totals."""
    distance = consumed = charged = source = stored = 0.0
    for robot in (*engine.robots.values(), *engine.retired):
        distance += robot.distance_m
        consumed += robot.travel_energy_units + robot.work_energy_units
        charged += robot.charge_energy_units
        source += robot.initial_energy_units + robot.recovery_energy_units + robot.capacity_adjustment_units
        stored += robot.live_battery()
        if robot._segment is not None:
            x0, y0, x1, y1, start, end, drain = robot._segment
            fraction = robot._segment_fraction(start, end)
            distance += math.hypot(x1 - x0, y1 - y0) * config.METERS_PER_PIXEL * fraction
            consumed += min(robot.battery, drain * fraction)
        if robot._work_started is not None:
            consumed += min(robot.battery, config.WORK_DRAIN_PER_SEC * (engine.now - robot._work_started))
        if robot._charge_step is not None:
            start, rate = robot._charge_step
            charged += min(robot.capacity - robot.battery, rate * (engine.now - start))
    return {"distance_m": distance, "battery_units": consumed, "charged_units": charged,
            "stored_units": stored, "energy_balance_error": source + charged - consumed - stored,
            **{"robot_energy:" + rid: value for rid, value in per_robot(engine).items()}}


def per_robot(engine: SimulationEngine) -> dict[str, float]:
    """Expose consumed units by stable robot ID, including partial and retired work."""
    values = {}
    for robot in (*engine.robots.values(), *engine.retired):
        units = robot.travel_energy_units + robot.work_energy_units
        if robot._segment is not None:
            *_, start, end, drain = robot._segment
            units += min(robot.battery, drain * robot._segment_fraction(start, end))
        if robot._work_started is not None:
            units += min(robot.battery, config.WORK_DRAIN_PER_SEC * (engine.now - robot._work_started))
        values[robot.robot_id] = values.get(robot.robot_id, 0) + units
    return values
