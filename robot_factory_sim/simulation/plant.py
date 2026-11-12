"""Explicit physical settings and initial conditions, independent of GUI and DB."""

from __future__ import annotations

import math
from dataclasses import dataclass

import config
from utils.config_models import FactoryConfig


@dataclass(frozen=True)
class PlantSettings:
    """Energy units and simulation seconds shared by every scheduling policy."""

    work_drain_per_sec: float = config.WORK_DRAIN_PER_SEC
    fault_recovery_time: float = config.FAULT_RECOVERY_TIME
    rescue_battery_pct: float = config.RESCUE_BATTERY_LEVEL

    def validate(self) -> None:
        """Reject invalid physical parameters before creating processes."""
        for value in (self.work_drain_per_sec, self.fault_recovery_time, self.rescue_battery_pct):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("plant settings must be finite nonnegative numbers")
        if self.rescue_battery_pct > 100:
            raise ValueError("rescue battery must be 0..100 percent")
        if self.fault_recovery_time <= 0 or self.rescue_battery_pct <= 0:
            raise ValueError("fault recovery requires positive duration and rescue energy")


@dataclass(frozen=True)
class RobotInitial:
    """A robot state installed before its first SimPy event."""

    x: float
    y: float
    floor: int = 1
    soc_pct: float = 100

    def validate(self, factory: FactoryConfig) -> None:
        """Check position, floor and state of charge against the factory."""
        if any(isinstance(v, bool) or not math.isfinite(v) for v in (self.x, self.y, self.soc_pct)):
            raise ValueError("initial robot coordinates/SOC must be finite numbers")
        if not 0 <= self.soc_pct <= 100:
            raise ValueError("initial robot SOC must be 0..100")
        if (
            isinstance(self.floor, bool)
            or not isinstance(self.floor, int)
            or not 1 <= self.floor <= factory.floor_count
        ):
            raise ValueError("initial robot floor is outside factory")
        if not 0 <= self.x <= factory.profile.floor_width or not 0 <= self.y <= factory.profile.floor_height:
            raise ValueError("initial robot position is outside factory")


def validate_cabins(factory: FactoryConfig) -> None:
    """Reject unsupported multi-rider cabins, including legacy node-group cabins."""
    capacities = [e.capacity for e in factory.elevators]
    capacities += [n.capacity for n in factory.nodes if n.node_type == "elevator" and n.capacity is not None]
    if any(isinstance(c, bool) or not isinstance(c, int) or c != 1 for c in capacities):
        raise ValueError("UNSUPPORTED_CABIN_CAPACITY: one rider per cabin is supported")
