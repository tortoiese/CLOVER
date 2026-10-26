"""Immutable, pickle-safe planning contracts. No engine, Qt or future samples."""

from __future__ import annotations

import math
from dataclasses import dataclass

from simulation.planning.units import (
    COST_SCALE,
    OBJECTIVE_PROFILE,
    UNIT_PROFILE,
    energy_down,
    energy_up,
)

__all__ = ["COST_SCALE", "OBJECTIVE_PROFILE", "UNIT_PROFILE", "energy_down", "energy_up"]

ENERGY_QUANTUM = 0.001
MODEL_VERSION = "rh-int-flow-v2"
PLAN_SCHEMA = "clover-plan-v2"


@dataclass(frozen=True)
class Settings:
    """Simulation seconds and wall-clock solver limits."""

    horizon: float = 60
    step: float = 1
    interval: float = 5
    timeout: float = 2.0
    watchdog: float = 3.0
    max_tasks: int = 8
    max_arcs: int = 60_000
    sigma: float = 1.0
    scope: str = "station_charge"
    memory_limit_mib: int = 1024

    def validate(self) -> None:
        """Reject unsafe sizes and inconsistent control intervals."""
        if not all(math.isfinite(v) for v in (self.step, self.interval, self.horizon, self.timeout, self.watchdog)):
            raise ValueError("non-finite planning setting")
        if not (.5 <= self.step <= self.interval <= self.horizon <= 300) or self.step not in (.5, 1, 2):
            raise ValueError("require step in {0.5,1,2} and step <= interval <= horizon <= 300")
        if not (1 <= self.max_tasks <= 32 and 0 < self.timeout < self.watchdog <= 30):
            raise ValueError("invalid candidates or computation budgets")
        if self.max_arcs < 1 or not 0 <= self.sigma <= 5:
            raise ValueError("invalid graph limit or duration buffer")
        if self.scope not in ("station_charge", "traffic"):
            raise ValueError("unsupported planning scope")
        if not 16 <= self.memory_limit_mib <= 4096:
            raise ValueError("solver memory limit must be 16..4096 MiB")


@dataclass(frozen=True)
class RobotInput:
    """An idle robot at an observed position, battery in integer ticks."""

    id: str
    location: str
    battery: int
    capacity: int
    reserve: int
    speed: float
    drain: float
    emergency_order: str | None = None
    charge_only: bool = False
    charge_target: int = 0


@dataclass(frozen=True)
class JobInput:
    """An observed queued job; process noise is intentionally excluded."""

    id: int
    stations: tuple[str, ...]
    created: float
    deadline: float | None
    weight: int
    order_id: str | None = None
    priority: int = 3


@dataclass(frozen=True)
class Facility:
    """A work or charge location with observable nominal service data."""

    id: str
    x: float
    y: float
    floor: int
    kind: str
    capacity: int = 1
    duration: float = 0.0
    rate: int = 0
    rate_raw: float = 0


@dataclass(frozen=True)
class RouteInput:
    """A width-safe route; physical metres exclude elevator surrogate costs."""

    robot: str
    origin: str
    destination: str
    points: tuple[tuple[float, float], ...]
    floors: tuple[int, ...]
    lifts: tuple[str | None, ...]
    nodes: tuple[int, ...]
    prev_nodes: tuple[int | None, ...]
    distance_m: float
    seconds: float
    uses: tuple[ResourceUse, ...] = ()
    rides: tuple[CabinArc, ...] = ()
    segment_starts: tuple[int, ...] = ()


@dataclass(frozen=True)
class ResourceUse:
    """A route's relative integer-time physical resource demand."""

    resource: str
    start: int
    end: int
    amount: int = 1


@dataclass(frozen=True)
class ResourceInput:
    """A stable physical resource identity and integer capacity."""

    id: str
    capacity: int
    kind: str


@dataclass(frozen=True)
class CabinInput:
    """One serialized cabin; loaded trips and empty reposition share its path."""

    id: str
    floor: int
    floors: tuple[int, ...]
    travel_seconds: float
    door_seconds: float
    available: bool = True


@dataclass(frozen=True)
class CabinArc:
    """A cabin's relative tick transition, including empty repositioning."""

    cabin: str
    origin: int
    destination: int
    start: int
    end: int
    kind: str


@dataclass(frozen=True)
class Reservation:
    """A half-open resource reservation in absolute simulation seconds."""

    resource: str
    robot: str
    start: float
    end: float
    amount: int = 1


@dataclass(frozen=True)
class Snapshot:
    """Complete immutable input made from currently observable state only."""

    token: str
    topology: str
    now: float
    settings: Settings
    robots: tuple[RobotInput, ...]
    jobs: tuple[JobInput, ...]
    facilities: tuple[Facility, ...]
    routes: tuple[RouteInput, ...]
    fixed: tuple[Reservation, ...]
    work_drain: float
    scope: str = "station_charge"
    resources: tuple[ResourceInput, ...] = ()
    cabins: tuple[CabinInput, ...] = ()
    model_version: str = MODEL_VERSION
    objective_profile: str = OBJECTIVE_PROFILE
    unit_profile: str = UNIT_PROFILE
    positions: tuple[tuple[str, float, float, int, float, float], ...] = ()
    geometry: tuple[tuple[int, float, float, int, str], ...] = ()
    edges: tuple[tuple[int, int, int, float, bool], ...] = ()
    clearance: float = 0
    obstacles: tuple[tuple[int, str, float, float, float, float, bool, str, int], ...] = ()


@dataclass(frozen=True)
class Arc:
    """A robot's decision on a time-expanded location graph."""

    robot: str
    origin: str
    destination: str
    start: int
    end: int
    kind: str
    resource: str = ""
    job: int | None = None
    energy: int = 0
    distance_m: float = 0.0


@dataclass(frozen=True)
class Action:
    """A validated, executable nominal action with absolute timestamps."""

    arc: Arc
    start: float
    end: float
    gain: int = 0
    action_id: str = ""


@dataclass(frozen=True)
class Report:
    """Each phase's incumbent and bound, plus the model identity and scope."""

    backend: str
    status: str
    seconds: float
    model_hash: str = ""
    scope: str = "station_charge"
    phase1_status: str = "NOT_SOLVED"
    phase1_cost: int | None = None
    phase1_bound: float | None = None
    phase2_status: str = "NOT_SOLVED"
    phase2_cost: int | None = None
    phase2_bound: float | None = None
    variables: int = 0
    constraints: int = 0
    message: str = ""
    backend_version: str = ""
    build_seconds: float = 0
    phase1_seconds: float = 0
    phase2_seconds: float = 0
    model_version: str = MODEL_VERSION
    objective_profile: str = OBJECTIVE_PROFILE
    unit_profile: str = UNIT_PROFILE
    rounding: str = "Decimal:consumption-ceil/available-floor/cost-half-up/distance-0.001m"
    solver_seed: int = 42
    worker_count: int = 1
    peak_rss_mib: float = 0
    total_wall_seconds: float = 0
    hint_used: bool = False
    phase1_proof: str = "SOLVER"


@dataclass(frozen=True)
class Result:
    """A complete incumbent or an explicit failure; never a partial solution."""

    snapshot: Snapshot
    report: Report
    actions: tuple[Action, ...] = ()
    deferred: tuple[int, ...] = ()
    cabins: tuple[CabinArc, ...] = ()
