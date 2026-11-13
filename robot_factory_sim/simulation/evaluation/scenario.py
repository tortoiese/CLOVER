"""Versioned scenario input and policy-independent demand generation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

import config
from simulation.plant import PlantSettings, RobotInitial, validate_cabins
from utils.config_models import FactoryConfig

SCHEMA = "clover-scenario-v1"
UNITS = {
    "coordinates": "px",
    "metres_per_pixel": config.METERS_PER_PIXEL,
    "dimensions": "m",
    "time": "sim-s",
    "energy": "battery-unit",
}
FOUNDATION_POLICIES = ("baseline", "optimized")
GRID_RH_POLICIES = ("rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_interval")
RH_POLICIES = (*GRID_RH_POLICIES, "rh_cpsat_native")
POLICIES = (*FOUNDATION_POLICIES, "assignment_milp_legacy", *RH_POLICIES)
MAX_JOBS = 100_000


def fingerprint(value: Any) -> str:
    """Hash canonical, finite JSON values."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def number(value: Any, name: str, minimum: float = 0, maximum: float = math.inf) -> float:
    """Validate finite numbers without treating bool as an integer."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        raise ValueError(f"{name}: finite number in [{minimum}, {maximum}] required")
    return float(value)


def integer(value: Any, name: str, minimum: int = 0, maximum: int = 2**32 - 1) -> int:
    """Validate bounded integer settings."""
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name}: integer in [{minimum}, {maximum}] required")
    return value


@dataclass(frozen=True)
class Demand:
    """Plant-only input; future arrivals/noise are never given to a scheduler."""

    uid: str
    at: float
    station: str
    allowed: tuple[str, ...]
    operation: str
    priority: int = 2
    noise: float = 0
    deadline: float | None = None

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Demand:
        """Decode a trace row without changing uid or event ordering."""
        return cls(**{**row, "allowed": tuple(row["allowed"])})


@dataclass(frozen=True)
class Intervention:
    """An exogenous cancellation, availability change, retirement or safe layout edit."""

    at: float
    kind: str
    target: str
    value: Any = True


@dataclass
class Scenario:
    """A complete independent experiment definition; no GUI or live DB reference."""

    id: str
    factory: FactoryConfig
    duration: float
    warmup: float
    initial_robots: dict[str, RobotInitial]
    initial_cabins: dict[str, int]
    plant: PlantSettings
    workload: dict[str, Any]
    interventions: tuple[Intervention, ...] = ()
    check_interval: float = 1

    def to_dict(self) -> dict[str, Any]:
        """Export portable input, retaining every physical unit explicitly."""
        return {
            "schema": SCHEMA,
            "id": self.id,
            "units": dict(UNITS),
            "factory": self.factory.to_dict(),
            "duration": self.duration,
            "warmup": self.warmup,
            "check_interval": self.check_interval,
            "initial_robots": {key: asdict(value) for key, value in self.initial_robots.items()},
            "initial_cabins": dict(self.initial_cabins),
            "plant": asdict(self.plant),
            "workload": self.workload,
            "interventions": [asdict(e) for e in self.interventions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Scenario:
        """Reject ambiguous input, unsupported features and incompatible units."""
        expected = {
            "schema",
            "id",
            "units",
            "factory",
            "duration",
            "warmup",
            "check_interval",
            "initial_robots",
            "initial_cabins",
            "plant",
            "workload",
            "interventions",
        }
        if not isinstance(raw, dict) or set(raw) - expected or raw.get("schema") != SCHEMA:
            raise ValueError("invalid scenario schema/fields")
        if raw.get("units") != UNITS:
            raise ValueError("explicit scenario units must match clover-scenario-v1")
        try:
            if not isinstance(raw.get("workload"), dict):
                raise ValueError("workload must be an object")
            if set(raw["factory"]) - {
                "profile",
                "stations",
                "chargers",
                "robots",
                "nodes",
                "edges",
                "elevators",
                "obstacles",
            }:
                raise ValueError("unknown factory field")
            factory_raw = raw["factory"]
            for facility in [*factory_raw.get("stations", []), *factory_raw.get("chargers", [])]:
                linked = [
                    n
                    for n in factory_raw.get("nodes", [])
                    if n.get("linked_station_id") == facility["id"] and n.get("node_type") in {"station", "charger"}
                ]
                if any(n.get("floor", 1) != facility.get("floor", 1) for n in linked):
                    raise ValueError("facility and linked node floors disagree")
            scenario = cls(
                raw["id"],
                FactoryConfig.from_dict(raw["factory"]),
                raw["duration"],
                raw.get("warmup", 0),
                {key: RobotInitial(**row) for key, row in raw["initial_robots"].items()},
                dict(raw.get("initial_cabins", {})),
                PlantSettings(**raw.get("plant", {})),
                raw["workload"],
                tuple(Intervention(**r) for r in raw.get("interventions", [])),
                raw.get("check_interval", 1),
            )
            scenario.validate()
            return scenario
        except (TypeError, KeyError) as exc:
            raise ValueError(f"malformed scenario: {exc}") from exc

    def validate(self) -> None:
        """Check model support and structural/physical inputs before starting a run."""
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id):
            raise ValueError("scenario id must be a safe 1..64 character identifier")
        number(self.duration, "duration", 0.001, 86400)
        number(self.warmup, "warmup", 0, self.duration)
        if self.warmup >= self.duration:
            raise ValueError("warmup must be shorter than duration")
        number(self.check_interval, "check_interval", 0.001, 60)
        self.plant.validate()
        f = self.factory
        validate_cabins(f)
        integer(f.profile.floor_count, "floor_count", 1, config.MAX_FLOOR_COUNT)
        for name in ("floor_width", "floor_height", "grid_cell_size"):
            number(getattr(f.profile, name), name, 0.001)
        for name in ("edge_clearance", "side_clearance", "floor_travel_sec", "door_time_sec"):
            number(getattr(f.profile, name), name)
        groups = (f.stations, f.chargers, f.robots, f.nodes, f.edges, f.elevators, f.obstacles)
        for group in groups:
            ids = [item.id for item in group]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate configuration id")
            for item in group:
                for key, value in asdict(item).items():
                    if isinstance(value, (float, int)) and not isinstance(value, bool):
                        if not math.isfinite(value):
                            raise ValueError(f"non-finite configuration field: {key}")
                if hasattr(item, "is_active") and not isinstance(item.is_active, bool):
                    raise ValueError("is_active must be boolean")
        facility_ids = [item.id for item in [*f.stations, *f.chargers, *f.elevators]]
        if any(not isinstance(value, str) or not value for value in facility_ids + [r.id for r in f.robots]):
            raise ValueError("robot/facility ids must be nonempty strings")
        if len(facility_ids) != len(set(facility_ids)):
            raise ValueError("facility identifiers must not collide")
        for item in [*f.stations, *f.chargers, *f.nodes, *f.elevators]:
            number(item.x, "x", 0, f.profile.floor_width)
            number(item.y, "y", 0, f.profile.floor_height)
            if hasattr(item, "floor"):
                integer(item.floor, "floor", 1, f.floor_count)
        for robot in f.robots:
            for key in ("battery_capacity", "width", "length"):
                number(getattr(robot, key), key, 0.000001)
            number(robot.move_speed, "move_speed", 0.05)
            number(robot.battery_drain_rate, "battery_drain_rate")
            number(robot.charge_threshold, "charge_threshold", 0, 100)
        for station in f.stations:
            number(station.avg_process_time, "avg_process_time", 0.5)
            number(station.process_time_std, "process_time_std")
            if station.task_type not in config.TASK_TYPES:
                raise ValueError("unknown station task type")
            number(station.width, "station width", 0.000001)
            number(station.height, "station height", 0.000001)
        for charger in f.chargers:
            integer(charger.capacity, "charger capacity", 1, 100)
            number(charger.charge_rate, "charge_rate", 0.000001)
        for cabin in f.elevators:
            number(cabin.travel_time, "cabin travel_time")
            number(cabin.width, "cabin width", 0.000001)
            number(cabin.height, "cabin height", 0.000001)
        node_ids = {n.id for n in f.nodes}
        for node in f.nodes:
            integer(node.id, "node id", -(2**31), 2**31 - 1)
            if node.node_type not in config.NODE_TYPES:
                raise ValueError("unknown node type")
        for edge in f.edges:
            integer(edge.id, "edge id", -(2**31), 2**31 - 1)
            integer(edge.from_node, "from_node", -(2**31), 2**31 - 1)
            integer(edge.to_node, "to_node", -(2**31), 2**31 - 1)
            if edge.from_node not in node_ids or edge.to_node not in node_ids:
                raise ValueError("edge endpoint does not exist")
            number(edge.width, "edge width", 0.000001)
            if not isinstance(edge.is_bidirectional, bool):
                raise ValueError("is_bidirectional must be boolean")
            if edge.distance is not None:
                nodes = {n.id: n for n in f.nodes}
                a, b = nodes[edge.from_node], nodes[edge.to_node]
                expected = math.hypot(a.x - b.x, a.y - b.y)
                number(edge.distance, "edge distance")
                if not math.isclose(edge.distance, expected, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("edge distance override is unsupported; geometry determines travel length")
        active_ids = {r.id for r in f.robots if r.is_active}
        if not 1 <= len(active_ids) <= 64 or not f.stations or not f.nodes:
            raise ValueError("require 1..64 active robots, stations and a route graph")
        if len(f.nodes) > 5000 or len(f.edges) > 20000:
            raise ValueError("scenario geometry exceeds supported evaluation size")
        if set(self.initial_robots) != active_ids:
            raise ValueError("provide an explicit initial state for every active robot")
        for initial in self.initial_robots.values():
            initial.validate(f)
        cabins = {e.id for e in f.elevators}
        groups = {n.linked_station_id for n in f.nodes if n.node_type == "elevator"}
        if not groups.issubset(cabins):
            raise ValueError("headless elevator nodes require explicit cabin configurations")
        if set(self.initial_cabins) != cabins:
            raise ValueError("provide an explicit floor for each cabin")
        for floor in self.initial_cabins.values():
            integer(floor, "initial cabin floor", 1, f.floor_count)
        if self.workload.get("kind") == "poisson":
            if set(self.workload) - {"kind", "rate", "priority", "deadline_slack", "fixed_station"}:
                raise ValueError("unknown poisson workload setting")
            number(self.workload["rate"], "workload rate", 0, 100)
            integer(self.workload.get("priority", 2), "priority", 0, 3)
            if not isinstance(self.workload.get("fixed_station", False), bool):
                raise ValueError("fixed_station must be boolean")
            if self.workload.get("deadline_slack") is not None:
                number(self.workload["deadline_slack"], "deadline_slack", 0)
        elif self.workload.get("kind") == "trace":
            if set(self.workload) != {"kind", "jobs"}:
                raise ValueError("trace workload requires only kind and jobs")
            validate_trace(self, tuple(Demand.from_dict(row) for row in self.workload["jobs"]))
        else:
            raise ValueError("workload kind must be trace or poisson")
        previous = -1.0
        for event in self.interventions:
            number(event.at, "intervention time", 0, self.duration)
            if event.at >= self.duration or event.at < previous:
                raise ValueError("interventions must be ordered before experiment end")
            previous = event.at
            targets = {
                "station_active": {s.id for s in f.stations},
                "charger_active": {c.id for c in f.chargers},
                "robot_retire": active_ids,
                "edge_width": {str(e.id) for e in f.edges},
            }
            if event.kind == "cancel_task":
                if not isinstance(event.target, str) or not event.target:
                    raise ValueError("cancel_task requires a task uid")
            elif event.kind not in targets or event.target not in targets[event.kind]:
                raise ValueError("unknown intervention kind/target")
            if event.kind == "edge_width":
                number(event.value, "edge width", 0.000001)
            elif not isinstance(event.value, bool):
                raise ValueError("availability/cancel/retire value must be boolean")
            elif event.kind in {"cancel_task", "robot_retire"} and event.value is not True:
                raise ValueError("cancel/retire actions require value=true")


def validate_trace(scenario: Scenario, trace: tuple[Demand, ...]) -> None:
    """Require ordered, unique, compatible exogenous jobs with finite values."""
    if len(trace) > MAX_JOBS:
        raise ValueError("trace exceeds explicit job limit")
    stations = {s.id: s for s in scenario.factory.stations}
    seen, previous = set(), -1.0
    for job in trace:
        if not isinstance(job.uid, str) or not 1 <= len(job.uid) <= 200 or job.uid in seen:
            raise ValueError("trace uid must be unique nonempty text")
        seen.add(job.uid)
        number(job.at, "arrival time", 0, scenario.duration)
        if job.at >= scenario.duration or job.at < previous:
            raise ValueError("trace must be ordered before experiment end")
        previous = job.at
        integer(job.priority, "priority", 0, 3)
        number(job.noise, "service noise", -1e6, 1e6)
        if job.deadline is not None:
            number(job.deadline, "deadline", job.at)
        if (
            not job.allowed
            or len(set(job.allowed)) != len(job.allowed)
            or job.station not in job.allowed
            or any(s not in stations or stations[s].task_type != job.operation for s in job.allowed)
        ):
            raise ValueError("trace contains incompatible/unknown eligible stations")


def make_trace(scenario: Scenario, seed: int) -> tuple[Demand, ...]:
    """Pre-generate arrivals and job-keyed service noise, independently of policy."""
    integer(seed, "seed")
    scenario.validate()
    w = scenario.workload
    if w["kind"] == "trace":
        return tuple(Demand.from_dict(row) for row in w["jobs"])
    if w["rate"] == 0:
        return ()
    rng = np.random.default_rng(np.random.SeedSequence([seed, 101]))
    destinations = np.random.default_rng(np.random.SeedSequence([seed, 102]))
    # Capability determines external demand; temporary availability determines
    # whether the plant can serve it. Never let an outage thin the input trace.
    stations = sorted(scenario.factory.stations, key=lambda s: s.id)
    if not stations:
        raise ValueError("poisson workload needs a configured station")
    jobs = []
    at = float(rng.exponential(1 / w["rate"]))
    while at < scenario.duration:
        if len(jobs) >= MAX_JOBS:
            raise ValueError("generated workload exceeds explicit job limit")
        station = stations[int(destinations.integers(len(stations)))]
        noise = float(np.random.default_rng(np.random.SeedSequence([seed, len(jobs), 202])).normal())
        allowed = (
            (station.id,)
            if w.get("fixed_station", False)
            else tuple(s.id for s in stations if s.task_type == station.task_type)
        )
        slack = w.get("deadline_slack")
        jobs.append(
            Demand(
                f"d:{len(jobs)}",
                at,
                station.id,
                allowed,
                station.task_type,
                w.get("priority", 2),
                noise,
                at + slack if slack is not None else None,
            )
        )
        at += float(rng.exponential(1 / w["rate"]))
    trace = tuple(jobs)
    validate_trace(scenario, trace)
    return trace


def read_scenario(path: Path) -> Scenario:
    """Load UTF-8 JSON; invalid numeric constants are rejected by validation."""
    return Scenario.from_dict(json.loads(path.read_text(encoding="utf-8")))
