"""Prespecified synthetic layout distribution, demand streams and main-study cells."""

from __future__ import annotations

import copy
from dataclasses import asdict, replace
from typing import Any

import numpy as np

from simulation.evaluation.design import _burst_time
from simulation.evaluation.design_references import synthetic_references
from simulation.evaluation.scenario import Intervention, Scenario, fingerprint, make_trace

CORE = ("S00", "S02", "S04", "S06", "S08", "S10")
CORE_POLICIES = ("baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear")
NATIVE_FAMILIES = ("S01", "S04", "S08", "S11")
CONDITIONS = (
    ("S00_low", "S00", .25),
    ("S02_width_1p3", "S02", .9), ("S02_width_2p2", "S02", .9), ("S02_width_3p1", "S02", .9),
    ("S04_slots_1", "S04", .9), ("S04_slots_2", "S04", .9), ("S04_slots_4", "S04", .9),
    ("S06_smooth", "S06", .7), ("S06_burst_2", "S06", .7), ("S06_burst_4", "S06", .7),
    ("S08_cabins_1", "S08", .9), ("S08_cabins_2", "S08", .9),
    ("S10_load_1p1", "S10", 1.1), ("S10_load_1p3", "S10", 1.3),
    ("S01_joint", "S01", .7), ("S03_intersection", "S03", .9), ("S05_hotspot", "S05", .9),
    ("S07_heterogeneous", "S07", .9), ("S09_recovery", "S09", .8), ("S11_grid64", "S11", .9),
)
PHASE_INDEX = {"calibration": 1, "training": 2, "validation": 3, "heldout": 4, "stress": 5}


def seed_for(family: str, phase: str, draw: int, repeat: int) -> int:
    """Use disjoint phase/layout demand streams; share a seed only within a treatment block."""
    return PHASE_INDEX[phase] * 100_000_000 + int(family[1:]) * 100_000 + draw * 1000 + repeat


def layout(family: str, phase: str, draw: int = 0, *, large: bool = False) -> tuple[Scenario, dict[str, Any]]:
    """Draw independent monotone grid-gap warps, retaining connectivity and shaft alignment."""
    reference = synthetic_references()
    raw = reference["variants"]["S11_graph_256"] if large else reference["cases"][family]
    case = Scenario.from_dict(copy.deepcopy(raw))
    if family == "S02":
        # Two stations on either side; no wide bypass around the central corridor.
        for station in case.factory.stations[:2]:
            station.x = 50
            node = next(n for n in case.factory.nodes if n.linked_station_id == station.id)
            node.x = station.x
            edge = next(e for e in case.factory.edges if e.to_node == node.id)
            edge.from_node = 2
    rng_seed = 1_100_000_000 + PHASE_INDEX[phase] * 10_000 + int(family[1:]) * 100 + draw
    rng = np.random.default_rng(rng_seed)
    factory = case.factory
    transforms = {}
    for axis, bound in (("x", "floor_width"), ("y", "floor_height")):
        points = [0.0, float(getattr(factory.profile, bound))]
        for group in (factory.nodes, factory.stations, factory.chargers, factory.elevators):
            points.extend(float(getattr(p, axis)) for p in group)
        knots = sorted(set(points))
        multipliers = rng.uniform(.8, 1.25, len(knots) - 1)
        warped = [0.0, *np.cumsum(np.diff(knots) * multipliers).tolist()]
        transforms[axis] = {"knots": knots, "warped": warped, "multipliers": multipliers.tolist()}
        for group in (factory.nodes, factory.stations, factory.chargers, factory.elevators):
            for point in group:
                setattr(point, axis, float(np.interp(getattr(point, axis), knots, warped)))
        setattr(factory.profile, bound, warped[-1])
    case.initial_robots = {rid: replace(v,
        x=float(np.interp(v.x, transforms["x"]["knots"], transforms["x"]["warped"])),
        y=float(np.interp(v.y, transforms["y"]["knots"], transforms["y"]["warped"])),
        soc_pct=float(rng.uniform(35, 80)))
        for rid, v in case.initial_robots.items()}
    if family == "S01":
        for station in factory.stations:
            station.process_time_std = .1 * station.avg_process_time
    if family == "S07":
        for robot, speed in zip(factory.robots, (.6, .9, 1.2, 1.5)):
            robot.move_speed = speed
    case.id = f"{phase}_{family}_L{draw}"
    case.duration, case.warmup = 2100, 300
    case.check_interval = 5
    case.workload = {"kind": "poisson", "rate": .1, "fixed_station": True, "deadline_slack": 120}
    case.validate()
    return case, {"distribution": "independent_monotone_grid_gap_uniform_0.8_1.25_v1",
                  "family": family, "phase": phase, "draw": draw, "rng_seed": rng_seed,
                  "transforms": transforms, "large_graph": large,
                  "initial_soc_pct": {k: v.soc_pct for k, v in case.initial_robots.items()},
                  "scope": "conditional synthetic topology distribution; no real-factory population claim"}


def free_flow_cycle(case: Scenario) -> float:
    """Compute a policy-free travel+processing index to transfer calibration across maps."""
    from simulation.engine import SimulationEngine
    from simulation.travel import estimate_trip

    engine = SimulationEngine(copy.deepcopy(case.factory), mode="optimized", plant=case.plant,
                              initial_robots=case.initial_robots, initial_cabins=case.initial_cabins, roam=False)
    values = []
    try:
        for robot in engine.robot_list():
            for origin in case.factory.stations:
                for target in case.factory.stations:
                    point = engine.path_graph.facility_point(origin.id) or (origin.x, origin.y)
                    trip = estimate_trip(engine, robot, target.id, origin=(*point, origin.floor))
                    if not np.isfinite(trip.seconds):
                        raise ValueError("unreachable reference OD pair")
                    values.append(trip.seconds + target.avg_process_time)
        return float(np.mean(values))
    finally:
        engine.close()


def treated(case: Scenario, condition: str) -> Scenario:
    """Apply exactly the registered physical axis after the common map draw."""
    case = Scenario.from_dict(case.to_dict())
    if condition.startswith("S02_width_"):
        next(e for e in case.factory.edges if e.id == 1).width = float(condition.rsplit("_", 1)[1].replace("p", "."))
    if condition.startswith("S04_slots_"):
        case.factory.chargers[0].capacity = int(condition.rsplit("_", 1)[1])
    if condition.startswith("S08_cabins_"):
        case.factory.elevators[1].is_active = condition.endswith("2")
    if condition == "S09_recovery":
        case.warmup = 0
        case.interventions = (Intervention(900, "station_active", case.factory.stations[0].id, False),
                              Intervention(1200, "station_active", case.factory.stations[0].id, True))
    return case


def demand_case(case: Scenario, family: str, condition: str, seed: int, rate: float) -> Scenario:
    """Freeze job identities/noise; treatments preserve their shared arrival stream."""
    case = treated(case, condition)
    case.workload["rate"] = rate
    jobs = [asdict(job) for job in make_trace(case, seed)]
    rng = np.random.default_rng(np.random.SeedSequence([seed, 787]))
    if family == "S01":
        case.duration, case.warmup = 300, 0
        stations = case.factory.stations
        order = rng.permutation(np.tile(np.arange(len(stations)), 2))
        jobs = [{"uid": f"batch:{i}", "at": 0, "station": stations[int(index)].id,
                 "allowed": [stations[int(index)].id], "operation": stations[int(index)].task_type,
                 "priority": 2, "noise": float(rng.normal()), "deadline": 120}
                for i, index in enumerate(order)]
    for job in jobs:
        job["allowed"] = list(job["allowed"])
        if family == "S05":
            index = 0 if rng.random() < .7 else int(rng.integers(1, len(case.factory.stations)))
            station = case.factory.stations[index]
            job.update(station=station.id, allowed=[station.id])
        if family == "S06":
            job["priority"] = 0 if rng.random() < .05 else 2
            job["allowed"] = [s.id for s in case.factory.stations]
            if "burst" in condition:
                multiplier = int(condition.rsplit("_", 1)[1])
                old = job["at"]
                job["at"] = _burst_time(old, case, multiplier, .1)
                job["deadline"] += job["at"] - old
        if family == "S11":
            job["allowed"] = [s.id for s in case.factory.stations]
    case.workload = {"kind": "trace", "jobs": sorted(jobs, key=lambda j: (j["at"], j["uid"]))}
    case.validate()
    return case


def window(case: Scenario, family: str, *, stress: bool = False) -> dict[str, Any]:
    """Declare a fixed finite observation and explicit charge exposure requirements."""
    return {"mode": "finite_batch" if family == "S01" else (
                "transient_stress" if stress or family == "S09" else "finite_stream"),
            "arrival_cutoff": case.duration, "drain_seconds": 0, "drain_in_measurement": True,
            "minimum_observation_seconds": case.duration - case.warmup,
            "minimum_completed_charge_sessions_per_robot": 2 if family == "S04" else 0,
            "minimum_charge_fraction": .1, "nominal": not stress}


def model_key(case: Scenario) -> str:
    """Identify calibration plant inputs independently of demand and case labels."""
    return fingerprint({"factory": case.factory.to_dict(), "plant": asdict(case.plant),
                        "initial_robots": {k: asdict(v) for k, v in case.initial_robots.items()},
                        "initial_cabins": case.initial_cabins})
