"""Immutable contracts shared by planning workers and the simulation thread."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """Planning units, scope and total backend computation budget."""

    horizon: float = 60.0
    step: float = 1.0
    interval: float = 5.0
    timeout: float = 2.0
    max_tasks: int = 8
    sigma: float = 1.0
    max_arcs: int = 60000


@dataclass(frozen=True)
class RobotInput:
    """A robot's state at its next uncommitted availability boundary."""

    id: str
    location: str
    ready: int
    battery: float
    capacity: float
    reserve: float
    speed: float
    drain: float
    emergency_order: str | None = None


@dataclass(frozen=True)
class JobInput:
    """An unassigned job and its current eligibility and urgency."""

    id: int
    stations: tuple[str, ...]
    created: float
    deadline: float | None
    weight: int
    order_id: str | None = None


@dataclass(frozen=True)
class StationInput:
    """One single-capacity work facility."""

    id: str
    location: str
    duration: float


@dataclass(frozen=True)
class ChargerInput:
    """A capacity-constrained charger with a constant energy rate."""

    id: str
    location: str
    capacity: int
    rate: float


@dataclass(frozen=True)
class RouteInput:
    """Reachable shortest route; coordinates and distance are in pixels."""

    origin: str
    destination: str
    points: tuple[tuple[float, float], ...]
    distance: float


@dataclass(frozen=True)
class Reservation:
    """A fixed, half-open interval owned by a committed robot."""

    resource: str
    robot_id: str
    start: float
    end: float
    kind: str


@dataclass(frozen=True)
class Snapshot:
    """Complete solver input; never contains live engine or Qt objects."""

    token: str
    revision: int
    now: float
    settings: Settings
    robots: tuple[RobotInput, ...]
    jobs: tuple[JobInput, ...]
    stations: tuple[StationInput, ...]
    chargers: tuple[ChargerInput, ...]
    routes: tuple[RouteInput, ...]
    fixed: tuple[Reservation, ...]
    meters_per_pixel: float
    work_drain: float


@dataclass(frozen=True)
class Arc:
    """One time-expanded decision, including private source and sink links."""

    robot_id: str
    origin: str
    destination: str
    start: int
    end: int
    kind: str
    resource: str = ""
    job_id: int | None = None
    energy: float = 0.0
    distance: float = 0.0


@dataclass(frozen=True)
class Problem:
    """Backend-independent arcs and precomputed fixed capacity usage."""

    snapshot: Snapshot
    arcs: tuple[Arc, ...]
    fixed_usage: tuple[tuple[str, int, int], ...]
    return_energy: tuple[tuple[str, str, float], ...]


@dataclass(frozen=True)
class Action:
    """An executable absolute-time action, with a planned terminal battery."""

    robot_id: str
    kind: str
    start: float
    end: float
    origin: str
    destination: str
    resource: str = ""
    job_id: int | None = None
    energy: float = 0.0
    gain: float = 0.0
    distance: float = 0.0
    points: tuple[tuple[float, float], ...] = ()
    battery_after: float = 0.0


@dataclass(frozen=True)
class SolveReport:
    """Incumbent and bound for each phase; a timeout is never called optimal."""

    status: str
    backend: str
    seconds: float
    variables: int = 0
    constraints: int = 0
    phase1_status: str = "NOT_SOLVED"
    phase1_cost: float | None = None
    phase1_bound: float | None = None
    phase2_status: str = "NOT_SOLVED"
    phase2_cost: float | None = None
    phase2_bound: float | None = None
    message: str = ""


@dataclass(frozen=True)
class Plan:
    """A solver result ready for independent validation and prefix commitment."""

    snapshot: Snapshot
    actions: tuple[Action, ...]
    deferred: tuple[int, ...]
    report: SolveReport


@dataclass(frozen=True)
class SolveResult:
    """Either a feasible plan or an explicit failure report."""

    token: str
    report: SolveReport
    plan: Plan | None = None


class PlanningBackend(ABC):
    """Interchangeable solver interface with an immutable input contract."""

    @abstractmethod
    def solve(self, snapshot: Snapshot) -> SolveResult:
        """Return a plan within the configured budget or an explicit failure."""


@dataclass
class PlanningStats:
    """Simulation-thread diagnostic counters; not shared with solver workers."""

    solves: int = 0
    accepted: int = 0
    fallbacks: int = 0
    stale: int = 0
    invalid: int = 0
    overruns: int = 0
    last_report: SolveReport | None = None
    last_shift: float = 0.0
    audit: list[dict[str, object]] = field(default_factory=list)
    reports: list[SolveReport] = field(default_factory=list)
