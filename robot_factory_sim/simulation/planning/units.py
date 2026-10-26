"""Versioned Decimal rounding contract shared by both solver adapters."""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

ENERGY_QUANTUM = Decimal("0.001")
DISTANCE_QUANTUM = Decimal("0.001")
COST_SCALE = 1_000_000
UNIT_PROFILE = "seconds-metres-battery-0.001-cost-1e6-v2"
OBJECTIVE_PROFILE = "weighted-defer-then-wait-tardy-distance-charge-v2"


def decimal(value: float | str | Decimal) -> Decimal:
    """Convert finite public numeric values without binary float arithmetic."""
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("non-finite planning quantity")
    return result


def energy_up(value: float | Decimal) -> int:
    """Round nonnegative consumption and reserve toward positive infinity."""
    amount = decimal(value)
    if amount < 0:
        raise ValueError("negative energy")
    return int((amount / ENERGY_QUANTUM).to_integral_value(rounding=ROUND_CEILING))


def energy_down(value: float | Decimal) -> int:
    """Round nonnegative available capacity and gain downward."""
    amount = decimal(value)
    if amount < 0:
        raise ValueError("negative energy")
    return int((amount / ENERGY_QUANTUM).to_integral_value(rounding=ROUND_FLOOR))


def distance_ticks(value: float) -> int:
    """Use millimetres for cost, retaining raw metres for physical consumption."""
    amount = decimal(value)
    if amount < 0:
        raise ValueError("negative distance")
    return int((amount / DISTANCE_QUANTUM).to_integral_value(rounding=ROUND_HALF_UP))


def cost_ticks(value: Decimal) -> int:
    """Use shared half-up cost coefficients and preserve nonzero costs."""
    result = int((value * COST_SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    return max(1, result) if value > 0 else min(-1, result) if value < 0 else 0


def gain_limit(rate_ticks: int, seconds: float, raw_rate: float = 0) -> int:
    """Floor the full charging interval once, including fractional slot lengths."""
    rate = decimal(raw_rate) if raw_rate > 0 else decimal(rate_ticks) * ENERGY_QUANTUM
    return energy_down(rate * decimal(seconds))
