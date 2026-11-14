"""Materialize a reviewed design into frozen, portable experimental inputs."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import numpy as np

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.design_references import REFERENCE_SCHEMA
from simulation.evaluation.runner import source_manifest
from simulation.evaluation.scenario import (
    POLICIES,
    Intervention,
    Scenario,
    fingerprint,
    integer,
    number,
)
from simulation.path_graph import PathGraph
from simulation.planning.contracts import Settings

PREPARED_SCHEMA = "clover-prepared-design-v1"
PHASES = ("e0", "e1-candidate")
FACTOR_KEYS = frozenset({
    "arrival_rate_tasks_per_s", "reference_rho", "corridor_width_m", "eligible_station_count", "node_type",
    "charger_slots", "charge_rate_units_per_s_per_occupied_slot", "initial_soc_pct", "hotspot_destination_share",
    "urgent_fraction", "arrival_shape", "burst_multiplier", "burst_time_fraction", "speed_m_per_s", "robot_width_m",
    "active_cabins", "initial_active_cabin_floor", "interventions", "intervention_kind", "start_s", "recovery_s",
    "target", "reference_capacity", "common_end_to_end_soft_deadline_s", "nodes", "robots", "uniform_initial_soc_pct",
    "upper_floor_destination_fraction", "process_time_cv",
})


def write_json(path: Path, value: Any) -> None:
    """Write explicit UTF-8/LF so input byte hashes are portable across hosts."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")


def design_cells(design: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize base and interaction cells, rejecting ambiguous duplicates."""
    if design.get("schema") != "clover-experiment-design-v3":
        raise ValueError("expected clover-experiment-design-v3, not an executable Scenario")
    rows = [dict(c) for c in design["base_cells"]]
    for block in design["interaction_blocks"]:
        rows.extend({**c, "family": block["id"], "factor_values": c["factors"],
                     "input_variation": "stochastic"} for c in block["cells"])
    ids = [r["id"] for r in rows]
    if len(set(ids)) != len(ids) or not rows:
        raise ValueError("design cell ids must be unique and nonempty")
    for row in rows:
        if set(row["factor_values"]) - FACTOR_KEYS:
            raise ValueError("unsupported design factor: " + row["id"])
        if row["input_variation"] not in {"stochastic", "deterministic"}:
            raise ValueError("unknown input variation")
        factors = row["factor_values"]
        if factors.get("arrival_shape", "smooth") not in {"smooth", "equal_realized_count_burst"}:
            raise ValueError("unknown arrival shape")
        if factors.get("interventions", []) != [] or factors.get("target", "predeclared_id") != "predeclared_id":
            raise ValueError("use declared intervention kinds and reference target aliases")
        if "common_end_to_end_soft_deadline_s" in factors:
            number(factors["common_end_to_end_soft_deadline_s"], "decision budget", .000001)
    return rows


def _rng(seed: int, family: str, stream: int) -> np.random.Generator:
    group = int.from_bytes(hashlib.sha256(family.encode()).digest()[:4], "big")
    return np.random.default_rng(np.random.SeedSequence([seed, group, stream]))


def _vector(case: Scenario, values: list[float], field: str) -> None:
    robots = sorted((r for r in case.factory.robots if r.is_active), key=lambda r: r.id)
    if len(values) != len(robots):
        raise ValueError("one physical value is required per robot")
    for robot, value in zip(robots, values):
        setattr(robot, field, value)


def _apply(case: Scenario, factors: dict[str, Any], targets: dict[str, Any]) -> None:
    factory = case.factory
    if "nodes" in factors and len(factory.nodes) != integer(factors["nodes"], "nodes", 1):
        raise ValueError("provide an explicit reference variant for this node count")
    if "robots" in factors and sum(r.is_active for r in factory.robots) != integer(factors["robots"], "robots", 1):
        raise ValueError("provide an explicit reference variant for this fleet size")
    if "corridor_width_m" in factors:
        ids = set(targets["corridor_edges"])
        if not ids or not ids.issubset({e.id for e in factory.edges}):
            raise ValueError("unknown controlled corridor edge")
        for edge in factory.edges:
            if edge.id in ids:
                edge.width = factors["corridor_width_m"]
    if "node_type" in factors:
        nodes = [n for n in factory.nodes if n.id == targets["intersection_node"]]
        if len(nodes) != 1 or factors["node_type"] not in {"waypoint", "intersection"}:
            raise ValueError("invalid controlled intersection")
        nodes[0].node_type = factors["node_type"]
    if "charger_slots" in factors or "charge_rate_units_per_s_per_occupied_slot" in factors:
        chargers = [c for c in factory.chargers if c.id == targets["charger"]]
        if len(chargers) != 1:
            raise ValueError("unknown controlled charger")
        if "charger_slots" in factors:
            chargers[0].capacity = factors["charger_slots"]
        if "charge_rate_units_per_s_per_occupied_slot" in factors:
            chargers[0].charge_rate = factors["charge_rate_units_per_s_per_occupied_slot"]
    for key, field in (("speed_m_per_s", "move_speed"), ("robot_width_m", "width")):
        if key in factors:
            _vector(case, factors[key], field)
    if "initial_soc_pct" in factors or "uniform_initial_soc_pct" in factors:
        values = factors.get("initial_soc_pct", [factors.get("uniform_initial_soc_pct")] * len(case.initial_robots))
        if len(values) != len(case.initial_robots):
            raise ValueError("one SOC is required per initial robot")
        case.initial_robots = {rid: replace(case.initial_robots[rid], soc_pct=value)
                               for rid, value in zip(sorted(case.initial_robots), values)}
    if "process_time_cv" in factors:
        cv = number(factors["process_time_cv"], "processing CV", 0)
        for station in factory.stations:
            station.process_time_std = cv * station.avg_process_time
    if "active_cabins" in factors:
        count = integer(factors["active_cabins"], "active cabins", 1, 2)
        for cabin in factory.elevators:
            cabin.is_active = cabin.id == targets["primary_cabin"] or (
                count == 2 and cabin.id == targets["secondary_cabin"])
    if "initial_active_cabin_floor" in factors:
        case.initial_cabins[targets["primary_cabin"]] = factors["initial_active_cabin_floor"]


def _burst_time(at: float, case: Scenario, multiplier: float, fraction: float) -> float:
    number(multiplier, "burst multiplier", 1)
    number(fraction, "burst fraction", 0.000001, 0.999999)
    if multiplier * fraction >= 1:
        raise ValueError("equal-mean burst requires multiplier*fraction < 1")
    if at < case.warmup:
        return at
    u = (at - case.warmup) / (case.duration - case.warmup)
    lo, hi = (1 - fraction) / 2, (1 + fraction) / 2
    off = (1 - multiplier * fraction) / (1 - fraction)
    first, peak = off * lo, multiplier * fraction
    if u < first:
        position = u / off
    elif u < first + peak:
        position = lo + (u - first) / multiplier
    else:
        position = hi + (u - first - peak) / off
    return case.warmup + position * (case.duration - case.warmup)


def _rank(values: np.ndarray) -> list[int]:
    result = [0] * len(values)
    for rank, index in enumerate(sorted(range(len(values)), key=lambda i: (values[i], i))):
        result[index] = rank
    return result


def _trace(case: Scenario, family: str, factors: dict[str, Any], references: dict[str, Any],
           seed: int, deterministic: bool, event_start: float) -> list[dict[str, Any]]:
    stations = sorted(case.factory.stations, key=lambda s: s.id)
    pool = [s for s in stations if s.floor == 2] if family == "S08" else stations
    slack = case.workload.get("deadline_slack", 120)
    if deterministic:
        if family == "S00":
            return []
        k = integer(factors.get("eligible_station_count", 1), "eligible count", 1, len(pool))
        return [{"uid": f"b:{i}", "at": 0, "station": pool[i % len(pool)].id,
                 "allowed": [pool[(i + j) % len(pool)].id for j in range(k)],
                 "operation": pool[i % len(pool)].task_type, "priority": 2, "noise": 0,
                 "deadline": slack} for i in range(8)]
    if case.workload.get("kind") != "poisson":
        raise ValueError("stream references must declare a base Poisson rate; frozen traces use run/suite directly")
    rate = factors.get("arrival_rate_tasks_per_s", case.workload["rate"])
    if "reference_rho" in factors:
        rate = (number(references["reference_mu"][family], "reference capacity", .000001)
                * number(factors["reference_rho"], "reference rho", 0))
    number(rate, "arrival rate", 0, 100)
    arrivals = []
    if rate:
        rng = _rng(seed, family, 101)
        at = float(rng.exponential() / rate)
        while at < case.duration:
            if len(arrivals) >= 99_999:
                raise ValueError("prepared demand exceeds explicit job limit")
            arrivals.append(at)
            at += float(rng.exponential() / rate)
    n = len(arrivals)
    destinations = _rng(seed, family, 102).random(n)
    destination_rank = _rank(destinations)
    priority_rank = _rank(_rng(seed, family, 203).random(n))
    urgent = number(factors.get("urgent_fraction", .05 if family == "S06" else 0), "urgent fraction", 0, 1)
    hotspot = factors.get("hotspot_destination_share")
    upper = factors.get("upper_floor_destination_fraction")
    if hotspot is not None:
        number(hotspot, "hotspot fraction", 0, 1)
        if len(stations) < 2:
            raise ValueError("hotspot comparison needs multiple configured stations")
    if upper is not None:
        number(upper, "upper floor fraction", 0, 1)
    jobs = []
    for i, at in enumerate(arrivals):
        eligible = stations
        if hotspot is not None:
            k = round(hotspot * n)
            station = stations[0 if destination_rank[i] < k else 1 + (destination_rank[i] - k) % (len(stations) - 1)]
        elif upper is not None:
            floor = 2 if destination_rank[i] < round(upper * n) else 1
            eligible = [s for s in stations if s.floor == floor]
            if not eligible:
                raise ValueError("floor mixture requires stations on both floors")
            station = eligible[i % len(eligible)]
        else:
            station = stations[min(int(destinations[i] * len(stations)), len(stations) - 1)]
        flexible = family in {"S06", "S09", "S11", "X02", "X03"}
        allowed = [s.id for s in eligible if s.task_type == station.task_type] if flexible else [station.id]
        if factors.get("arrival_shape") == "equal_realized_count_burst":
            at = _burst_time(at, case, factors["burst_multiplier"], factors.get("burst_time_fraction", .1))
        noise = float(_rng(seed, family, 202 + i * 1000).normal())
        jobs.append({"uid": f"j:{i}", "at": at, "station": station.id, "allowed": allowed,
                     "operation": station.task_type, "priority": 0 if priority_rank[i] < round(urgent * n) else 2,
                     "noise": noise, "deadline": at + slack if slack is not None else None})
    if family == "S09":
        station = next(s for s in stations if s.id == references["targets"][family]["station"])
        at = event_start - 1
        jobs.append({"uid": "probe:intervention", "at": at, "station": station.id,
                     "allowed": [s.id for s in stations if s.task_type == station.task_type],
                     "operation": station.task_type, "priority": 2, "noise": 0,
                     "deadline": at + slack if slack is not None else None})
    return sorted(jobs, key=lambda j: (j["at"], j["uid"]))


def preflight(case: Scenario) -> dict[str, Any]:
    """Validate the input and report width-aware initial reachability, not optimality."""
    case.validate()
    graph = PathGraph(case.factory)
    reachable = {}
    for robot in case.factory.robots:
        if not robot.is_active:
            continue
        initial = case.initial_robots[robot.id]
        reachable[robot.id] = {s.id for s in case.factory.stations
                               if graph.route_to_facility(initial.x, initial.y, s.id, initial.floor,
                                                          robot_width=robot.width) is not None}
        if not reachable[robot.id]:
            raise ValueError("robot cannot reach any configured station: " + robot.id)
    for allowed in {tuple(j["allowed"]) for j in case.workload["jobs"]}:
        if not any(set(allowed) & ids for ids in reachable.values()):
            raise ValueError("job has no initially reachable compatible robot")
    if any(e.at < case.warmup for e in case.interventions):
        raise ValueError("intervention is outside measurement; use a transient window or move the event")
    return {"schema_valid": True, "initial_reachable_pairs": sum(map(len, reachable.values())),
            "initial_blocked_pairs": len(reachable) * len(case.factory.stations) - sum(map(len, reachable.values())),
            "scope": "initial routes and schema only; dynamic resource exposure/energy cycles need E0/E1 runs"}


def materialize(design: dict[str, Any], references: dict[str, Any], phase: str, seeds: tuple[int, ...],
                cells: tuple[str, ...] | None = None) -> list[tuple[Scenario, dict[str, Any]]]:
    """Freeze each cell/seed once; deterministic inputs are not relabelled as replicates."""
    if references.get("schema") != REFERENCE_SCHEMA or phase not in PHASES:
        raise ValueError("unsupported reference bundle or preparation phase")
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("provide unique demand seeds")
    for seed in seeds:
        integer(seed, "seed", 0, 2**32 - 1)
    rows = design_cells(design)
    if cells is not None:
        if not cells or len(set(cells)) != len(cells) or set(cells) - {c["id"] for c in rows}:
            raise ValueError("unknown, empty or duplicate cell selection")
        rows = [r for r in rows if r["id"] in cells]
    prepared = []
    for row in rows:
        family, factors = row["family"], row["factor_values"]
        targets = references["targets"][family]
        raw = references.get("variants", {}).get(row["id"], references["cases"][family])
        for seed in seeds[:1] if row["input_variation"] == "deterministic" else seeds:
            case = Scenario.from_dict(copy.deepcopy(raw))
            if case.interventions:
                raise ValueError("reference cases must be unperturbed; the design supplies interventions")
            anchors = next((f["anchor_design"] for f in design["families"] if f["id"] == family), {})
            for key, actual in (("robots", sum(r.is_active for r in case.factory.robots)),
                                ("stations", len(case.factory.stations))):
                expected = factors.get(key, anchors.get(key))
                if expected is not None and actual != expected:
                    raise ValueError(f"reference {family} requires {expected} {key}, got {actual}")
            original_window = [case.warmup, case.duration]
            if phase == "e0":
                case.duration, case.warmup = 180, 0
            _apply(case, factors, targets)
            start, recovery = (60, 90) if phase == "e0" else (factors.get("start_s", 300),
                                                            factors.get("recovery_s", 420))
            jobs = _trace(case, family, factors, references, seed,
                          row["input_variation"] == "deterministic", start)
            case.workload = {"kind": "trace", "jobs": jobs}
            events = []
            if "intervention_kind" in factors:
                kind = factors["intervention_kind"]
                target = {"station_active": targets["station"], "charger_active": targets["charger"],
                          "robot_retire": targets["robot"], "edge_width": str(targets["edge"]),
                          "cancel_task": "probe:intervention"}[kind]
                value = 1.3 if kind == "edge_width" else kind in {"robot_retire", "cancel_task"}
                events.append(Intervention(start, kind, target, value))
                if factors.get("recovery_s") is not None:
                    old_width = next(e.width for e in case.factory.edges if e.id == targets["edge"])
                    events.append(Intervention(recovery, kind, target, old_width if kind == "edge_width" else True))
            case.interventions = tuple(events)
            fixed = row["input_variation"] == "deterministic"
            case.id = f"{phase[:2].upper()}_{row['id']}__" + ("fixed" if fixed else f"s{seed}")
            checks = preflight(case)
            policies = list(POLICIES) if phase == "e0" else list(design["core_policy_ids"])
            if phase != "e0" and family in design["native_additional_families"]:
                policies.append("rh_cpsat_native")
            prepared.append((case, {"cell": row["id"], "family": family, "run_seed": seed,
                                    "demand_seed": None if fixed else seed, "input_variation": row["input_variation"],
                                    "factors": factors, "policies": policies,
                                    "decision_soft_deadline_s": factors.get("common_end_to_end_soft_deadline_s"),
                                    "reference_window": original_window, "applied_window": [case.warmup, case.duration],
                                    "common_probe_jobs": 1 if family == "S09" else 0, "preflight": checks}))
    return prepared


def prepare_design(design: dict[str, Any], references: dict[str, Any], output: Path, *, phase: str = "e0",
                   seeds: tuple[int, ...] = (1001,), cells: tuple[str, ...] | None = None,
                   planning_settings: dict[str, Any] | None = None,
                   decision_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Prepare all inputs before publication; preserve settings, provenance and hashes."""
    source = source_manifest()
    prepared = materialize(design, references, phase, seeds, cells)
    math_settings = {**asdict(Settings(horizon=40, step=2, max_tasks=8, sigma=0,
                                      timeout=2, watchdog=3, native_actions=4)), **(planning_settings or {})}
    Settings(**math_settings).validate()
    if math_settings["timing_mode"] != "D0":
        raise ValueError("prepared studies use common staged D1; keep math timing_mode D0")
    decision = {**DecisionSettings(end_to_end_soft_deadline_seconds=2,
                                   publication_allowance_seconds=.2).to_dict(), **(decision_settings or {})}
    DecisionSettings(**decision).validate()
    if source_manifest()["source_hash"] != source["source_hash"]:
        raise ValueError("source changed during preparation")
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs").mkdir()
    for name, value in (("design.json", design), ("references.json", references),
                        ("math_settings.json", math_settings), ("decision_settings.json", decision)):
        write_json(output / name, value)
    rows = []
    for case, metadata in prepared:
        relative = f"inputs/{case.id}.json"
        write_json(output / relative, case.to_dict())
        rows.append({**metadata, "scenario": case.id, "path": relative,
                     "scenario_hash": fingerprint(case.to_dict()),
                     "file_sha256": hashlib.sha256((output / relative).read_bytes()).hexdigest(),
                     "trace_hash": fingerprint(case.workload["jobs"]),
                     "initial_hash": fingerprint({"robots": {k: asdict(v) for k, v in case.initial_robots.items()},
                                                  "cabins": case.initial_cabins}),
                     "jobs": len(case.workload["jobs"]), "nodes": len(case.factory.nodes),
                     "robots": sum(r.is_active for r in case.factory.robots)})
    manifest = {"schema": PREPARED_SCHEMA, "status": "PREPARED", "phase": phase, "source": source,
                "layout_id": references["layout_id"], "provenance": references["provenance"],
                "calibration_status": references["calibration_status"], "design_hash": fingerprint(design),
                "reference_hash": fingerprint(references), "requested_seeds": seeds, "entries": rows,
                "math_settings": math_settings, "decision_settings": decision,
                "scope": "synthetic/reference E0 or E1 candidates; no held-out or superiority conclusion",
                "heldout_enabled": False}
    if source_manifest()["source_hash"] != source["source_hash"]:
        raise ValueError("source changed while writing inputs; no prepared manifest published")
    write_json(output / "manifest.json", manifest)
    return manifest
