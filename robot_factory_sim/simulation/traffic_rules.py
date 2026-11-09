"""Shared traffic contract: metres at input, conservative integer mm in admission."""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from simulation.resource_pool import (
    TRAFFIC_CONTRACT,
    CapacityPool,
    CapacityRequest,
    TrafficBlocked,
)
from utils.config_models import FactoryProfile

LaneResource = CapacityPool
LaneRequest = CapacityRequest
__all__ = ["LaneRequest", "LaneResource", "TrafficBlocked", "can_pass", "capacity_mm", "demand_mm"]


@dataclass(frozen=True)
class TrafficDemand:
    """Immutable planner/runtime demand for the complete controlled section interval."""

    resource: str
    amount: int
    unit: str
    contract: str = TRAFFIC_CONTRACT


@dataclass(frozen=True)
class TrafficReservation:
    """Shared half-open [start, end) section reservation in simulation seconds."""

    resource: str
    amount: int
    unit: str
    start: float
    end: float
    contract: str = TRAFFIC_CONTRACT


def physical_point(floor: int, x: float, y: float) -> tuple[int, float, float]:
    """Identify a footprint without DB numbering or signed-zero aliases."""
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("traffic coordinates must be finite")
    return floor, float(x) if x else 0.0, float(y) if y else 0.0


def corridor_identity(a: tuple[int, float, float], b: tuple[int, float, float]) -> str:
    """Share one budget for both directions and duplicate physical edges."""
    return "corridor:" + repr(tuple(sorted((a, b))))


def tail_clearance_seconds(length: float, speed: float) -> float:
    """Hold after the centre reaches the exit until the rear half has cleared."""
    if not math.isfinite(length) or not math.isfinite(speed) or length <= 0 or speed <= 0:
        raise ValueError("robot length and speed must be finite positive numbers")
    return length / (2 * max(.05, speed))


def _decimal(value: float) -> Decimal:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("traffic dimensions must be finite numbers")
    return Decimal(str(value))


def required_width(robot_width: float, profile: FactoryProfile) -> float:
    """Return robot width plus total wall and per-robot side reserves."""
    values = tuple(_decimal(v) for v in (robot_width, profile.edge_clearance, profile.side_clearance))
    if values[0] <= 0 or any(value < 0 for value in values):
        raise ValueError("robot width must be positive and clearances nonnegative")
    return float(sum(values))


def capacity_mm(edge_width: float, profile: FactoryProfile) -> int:
    """Floor usable width after subtracting the total wall reserve exactly once."""
    width, wall = _decimal(edge_width), _decimal(profile.edge_clearance)
    if width < 0 or wall < 0:
        raise ValueError("edge width and wall reserve must be nonnegative")
    return max(0, int(((width - wall) * 1000).to_integral_value(rounding=ROUND_FLOOR)))


def demand_mm(robot_width: float, profile: FactoryProfile) -> int:
    """Ceil one robot's width and side separation; never clamp to capacity."""
    required_width(robot_width, profile)
    return int(((_decimal(robot_width) + _decimal(profile.side_clearance)) * 1000)
               .to_integral_value(rounding=ROUND_CEILING))


def can_pass(edge_width: float, robot_width: float, profile: FactoryProfile) -> bool:
    """Use the same integer width inequality as runtime and planning."""
    required = demand_mm(robot_width, profile)
    if not math.isfinite(edge_width) or edge_width < 0:
        return False
    return required <= capacity_mm(edge_width, profile)
