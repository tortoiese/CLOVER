"""Observable, immutable input; no executable compatibility with pre-PR9 plans."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from simulation.resource_pool import TRAFFIC_CONTRACT
from simulation.traffic_rules import TrafficReservation

SNAPSHOT_SCHEMA = "clover-planning-observation-v6"
MODEL_VERSION = "rh-service-flow-v8"
PLAN_SCHEMA = "clover-service-plan-v8"
RH_BACKENDS = {"rh_milp_grid": "milp", "rh_cpsat_linear": "cpsat_linear", "rh_cpsat_interval": "cpsat_interval"}
ALL_RH_BACKENDS = {**RH_BACKENDS, "rh_cpsat_native": "cpsat_native"}


@dataclass(frozen=True)
class Settings:
    """Input preparation limits, independent of GUI or a selected solver."""

    horizon: float = 60
    step: float = 1
    max_tasks: int = 32
    max_routes: int = 20000
    sigma: float = 1
    interval: float = 5
    timeout: float = 1
    watchdog: float = 3
    max_arcs: int = 30000
    max_tail_ticks: int = 1200
    memory_limit_mib: int = 1024
    timing_mode: str = "D0"
    warm_start: bool = True
    warm_start_seconds: float = 0.05
    joint_cabins: bool = True
    max_cabin_states: int = 30000
    formulation: str = "grid"
    native_step: float = 0.1
    native_actions: int = 0
    native_pruning: bool = True
    native_cache: bool = True

    @property
    def time_unit(self) -> float:
        """Return seconds per recorded tick for the selected formulation."""
        return self.native_step if self.formulation == "native" else self.step

    @property
    def tail_limit(self) -> int:
        """Keep the tail's physical limit independent of native resolution."""
        return round(self.max_tail_ticks * self.step / self.time_unit)

    def validate(self) -> None:
        """Reject ambiguous units and unbounded preparation sizes."""
        if self.timing_mode not in {"D0", "D1"}:
            raise ValueError("timing_mode must be D0 or D1")
        if type(self.warm_start) is not bool:
            raise ValueError("warm_start must be boolean")
        if (
            type(self.joint_cabins) is not bool
            or type(self.max_cabin_states) is not int
            or not 1 <= self.max_cabin_states <= 200000
        ):
            raise ValueError("invalid cabin state model settings")
        if (
            isinstance(self.warm_start_seconds, bool)
            or not isinstance(self.warm_start_seconds, (int, float))
            or not math.isfinite(self.warm_start_seconds)
            or not 0 < self.warm_start_seconds <= 1
        ):
            raise ValueError("warm_start_seconds must be in (0, 1]")
        for value in (self.horizon, self.step, self.sigma):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("planning settings must be finite numeric values")
        if self.step not in (0.5, 1, 2) or not self.step <= self.horizon <= 300 or not 0 <= self.sigma <= 5:
            raise ValueError("invalid planning horizon, grid or nominal duration buffer")
        if (
            self.formulation not in {"grid", "native"}
            or isinstance(self.native_step, bool)
            or self.native_step not in (0.01, 0.1, 0.5, 1, 2)
            or self.native_step > self.step
            or abs(self.step / self.native_step - round(self.step / self.native_step)) > 1e-9
            or type(self.native_actions) is not int
            or not 0 <= self.native_actions <= 3000
            or type(self.native_pruning) is not bool
            or type(self.native_cache) is not bool
        ):
            raise ValueError("invalid native resolution/action domain")
        for value, maximum in ((self.max_tasks, 256), (self.max_routes, 100000)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError("invalid planning candidate/route limit")
        for value in (self.interval, self.timeout, self.watchdog):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("invalid control budget")
        if not self.step <= self.interval <= self.horizon or not 0 < self.timeout < self.watchdog <= 60:
            raise ValueError("require step <= interval <= horizon and 0 < timeout < watchdog <= 60")
        for value, maximum in ((self.max_arcs, 200000), (self.max_tail_ticks, 10000), (self.memory_limit_mib, 8192)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError("invalid model/memory limit")


@dataclass(frozen=True)
class RobotInput:
    """Observed instance and live battery; retired owners can still hold traffic."""

    id: str
    instance: int
    x: float
    y: float
    floor: int
    battery: int
    capacity: int
    reserve: int
    speed: float
    drain: float
    width: float
    length: float
    available: bool
    alive: bool
    current_job: str | None
    emergency_order: str | None
    forced_charge: bool


@dataclass(frozen=True)
class JobInput:
    """A visible queued logical job without actual future service noise."""

    uid: str
    task_id: int
    stations: tuple[str, ...]
    operation: str
    created: float
    deadline: float | None
    priority: int
    weight: int
    order_id: str | None


@dataclass(frozen=True)
class ExcludedJob:
    """Candidate coverage evidence for aging and starvation checks."""

    uid: str
    age: float
    reason: str


@dataclass(frozen=True)
class FacilityInput:
    """Actual graph approach point and currently observed service capability."""

    id: str
    kind: str
    x: float
    y: float
    floor: int
    active: bool
    capacity: int
    occupied: int
    waiting: int
    operation: str = ""
    nominal_seconds: float = 0
    charge_rate: float = 0
    dedicated_order: str | None = None
    current_job: str | None = None


@dataclass(frozen=True)
class ResourceInput:
    """Observed PR9 integer budget; occupancy can exceed zero during drain."""

    id: str
    unit: str
    capacity: int
    occupied: int
    contract: str = TRAFFIC_CONTRACT


@dataclass(frozen=True)
class PhysicalClaim:
    """One actual claim; an unknown release time is never invented as now+Delta."""

    request_id: int
    owner: str
    resource: str
    amount: int
    unit: str
    since: float
    end: float | None = None
    contract: str = TRAFFIC_CONTRACT


@dataclass(frozen=True)
class CabinInput:
    """Fractional observed cabin position, including empty relocation in flight."""

    id: str
    floor: float
    active: bool
    moving: bool
    occupied: int
    waiting: int
    travel_seconds: float
    door_seconds: float
    release_at: float | None = None
    release_floor: float | None = None
    release_owner: str | None = None


@dataclass(frozen=True)
class Snapshot:
    """Detached current input, serializable without engine, Qt or SimPy handles."""

    token: str
    epoch: str
    now: float
    layout_version: int
    topology: str
    factory_json: str
    settings: Settings
    robots: tuple[RobotInput, ...]
    jobs: tuple[JobInput, ...]
    facilities: tuple[FacilityInput, ...]
    resources: tuple[ResourceInput, ...]
    claims: tuple[PhysicalClaim, ...]
    cabins: tuple[CabinInput, ...]
    excluded: tuple[ExcludedJob, ...]
    pending_count: int
    work_drain: float
    traffic_paused: bool
    schema: str = SNAPSHOT_SCHEMA
    traffic_contract: str = TRAFFIC_CONTRACT
    committed: tuple[PlannedHold, ...] = ()
    holds: tuple[ResourceHold, ...] = ()


@dataclass(frozen=True)
class PlannedHold:
    """A committed prefix envelope, separate from actual acquired claims."""

    owner: str
    resource: str
    amount: int
    unit: str


@dataclass(frozen=True)
class ResourceHold:
    """Observed ownership or committed obligation in absolute simulation seconds."""

    owner: str
    resource: str
    amount: int
    unit: str
    start: float
    end: float | None
    source: str


@dataclass(frozen=True)
class CabinUse:
    """One route transition coupled to a shared cabin path by the solver."""

    cabin: str
    origin: int
    destination: int
    start: float
    end: float
    source_floor: float = 1.0
    segment_index: int = -1


@dataclass(frozen=True)
class RouteInput:
    """K=1 geometric route with complete section holds in relative seconds."""

    robot: str
    origin: str
    destination: str
    distance_m: float
    seconds: float
    points: tuple[tuple[float, float], ...]
    floors: tuple[int, ...]
    lifts: tuple[str | None, ...]
    nodes: tuple[int, ...]
    previous_nodes: tuple[int | None, ...]
    segment_starts: tuple[float, ...]
    segment_ends: tuple[float, ...]
    uses: tuple[TrafficReservation, ...]
    cabins: tuple[CabinUse, ...]
    use_sections: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class PreparedInput:
    """Worker-side routes plus explicit reasons for excluded origin/destination pairs."""

    snapshot: Snapshot
    routes: tuple[RouteInput, ...]
    unavailable_routes: tuple[tuple[str, str, str, str], ...]


@dataclass(frozen=True)
class Use:
    """A half-open relative grid interval with an integer PR9 demand."""

    resource: str
    unit: str
    amount: int
    start: int
    end: int


@dataclass(frozen=True)
class Arc:
    """One wait, composite movement/work service, or movement/charge action."""

    robot: str
    origin: str
    destination: str
    start: int
    end: int
    kind: str
    energy: int = 0
    job: str | None = None
    route: int | None = None
    service_start: int = 0
    uses: tuple[Use, ...] = ()
    cabins: tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    """A decoded integer action and validated charging gain, not a resource grant."""

    arc: Arc
    gain: int = 0


@dataclass(frozen=True)
class Report:
    """Bounded two-phase solve evidence for this snapshot model only."""

    status: str
    backend: str = "milp"
    model_hash: str = ""
    semantic_hash: str = ""
    semantic_spec_hash: str = ""
    time_unit_seconds: float = 0
    secondary_scale: int = 1
    action_scope: str = "complete grid domain"
    representation: str = "integer-linear"
    version: str = MODEL_VERSION
    phase1_status: str = "NOT_SOLVED"
    native_phase1_status: str = "NOT_SOLVED"
    native_phase2_status: str = "NOT_SOLVED"
    phase1_proof: str = "NONE"
    phase1_incumbent_source: str = "NONE"
    phase1_cost: int | None = None
    phase1_bound: float | None = None
    phase2_status: str = "NOT_SOLVED"
    phase2_cost: int | None = None
    phase2_bound: float | None = None
    variables: int = 0
    constraints: int = 0
    build_seconds: float = 0
    solve_seconds: float = 0
    total_seconds: float = 0
    model_seconds: float = 0
    hint_seconds: float = 0
    import_seconds: float = 0
    compile_seconds: float = 0
    search_seconds: float = 0
    solution_check_seconds: float = 0
    dispatch_to_worker_seconds: float = 0
    prepare_seconds: float = 0
    response_seconds: float = 0
    parent_elapsed_seconds: float = 0
    worker_cpu_seconds: float = 0
    worker_peak_rss_mib: float = 0
    worker_reused: bool = False
    hint_source: str = "SAFE_HOLD_HINT"
    hint_primary_cost: int | None = None
    hint_secondary_cost: int | None = None
    hint_trials: int = 0
    hint_budget_exhausted: bool = False
    solution_source: str = "NONE"
    backend_version: str = ""
    message: str = ""
    cabin_scope: str = "one rider; unknown releases blocked; joint scope depends on settings"
    timing_stage: str = "not_profiled"
    timing_complete: bool = False
    stage_seconds: dict[str, float] = field(default_factory=dict)
    work_counters: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    """Complete candidate schedule or explicit failure, including replay input."""

    input: PreparedInput
    report: Report
    actions: tuple[Action, ...] = ()
    deferred: tuple[str, ...] = ()
