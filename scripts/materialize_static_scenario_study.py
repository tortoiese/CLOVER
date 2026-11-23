"""Materialize the solo-owner static study without executing any simulator or solver."""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict
from pathlib import Path
from typing import Any

from materialize_korean_case_pilot import (
    COMMON_KEYS,
    REPO,
    DecisionSettings,
    Scenario,
    Settings,
    factory_for,
    file_sha,
    fingerprint,
    integer,
    jobs_for,
    number,
    source_manifest,
    validate_routes,
    write_json,
)
from simulation.evaluation.study import code_identity
from simulation.plant import PlantSettings, RobotInitial
from utils.config_models import (
    FactoryConfig,
    PathEdgeConfig,
    PathNodeConfig,
    WorkStationConfig,
)

DESIGN = REPO / "docs/scenarios/static_study_20261120/design.json"
POLICIES = ["baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear"]
TOOLS = (
    "materialize_static_scenario_study.py",
    "audit_static_scenario_study.py",
    "run_static_scenario_study.py",
    "static_scenario_metrics.py",
    "materialize_korean_case_pilot.py",
    "audit_korean_case_pilot.py",
)
KEYS = {
    "schema",
    "provenance",
    "owner",
    "confirmatory",
    "duration",
    "warmup",
    "policies",
    "common",
    "rh_settings",
    "d1_settings",
    "run_limits",
    "load_reference_tasks_per_s",
    "loads",
    "slots",
    "shapes",
    "canary_seed",
    "canary_conditions",
    "d0_conditions",
    "development_seeds",
    "validation_layouts",
    "validation_conditions",
    "interventions",
    "scope_notes",
}


def validate_design(design: dict[str, Any]) -> None:
    """Reject ambiguous factors, leaked seeds and non-static inputs before writing files."""
    if (
        set(design) != KEYS
        or design["schema"] != "clover-static-study-design-v1"
        or design["provenance"] != "synthetic_research"
        or design["confirmatory"] is not False
    ):
        raise ValueError("explicit exploratory static study schema required")
    if design["policies"] != POLICIES or design["interventions"] != []:
        raise ValueError("fixed four-policy matrix and empty interventions required")
    duration = number(design["duration"], "duration", 60, 86400)
    number(design["warmup"], "warmup", 0, duration - 60)
    if (
        design["loads"] != {"L06": 0.6, "L10": 1.0, "L13": 1.3}
        or design["slots"] != [1, 2, 4]
        or design["shapes"] != [1, 4]
    ):
        raise ValueError("the planned 3 by 3 by 2 factor matrix is required")
    for value in [*design["loads"].values(), *design["slots"], *design["shapes"]]:
        number(value, "factor", 0.000001)
    number(design["load_reference_tasks_per_s"], "synthetic lambda reference", 0.000001, 1)
    if set(design["common"]) != COMMON_KEYS:
        raise ValueError("unknown or missing plant key")
    for key, value in design["common"].items():
        if key != "initial_soc_pct_range":
            number(value, key, 0 if key == "process_time_std_s" else 0.000001)
    low, high = design["common"]["initial_soc_pct_range"]
    number(low, "initial SOC lower", 0, 100)
    number(high, "initial SOC upper", low, 100)
    if (
        design["common"]["floor_width_px"] != 600
        or design["common"]["floor_height_px"] != 400
        or design["common"]["process_time_std_s"] != 0
    ):
        raise ValueError("fixed 60 by 40 metre layout and deterministic processing required")
    Settings(**design["rh_settings"]).validate()
    decision = DecisionSettings(**design["d1_settings"])
    decision.validate()
    if (
        design["rh_settings"].get("timing_mode") != "D0"
        or decision.startup_mode != "on_demand"
        or not decision.record_planning_inputs
        or decision.timing_protocol != "d1-primary-deadline-v2"
    ):
        raise ValueError("staged D1 requires base D0 math, on-demand startup and retained snapshots")
    limits = design["run_limits"]
    if set(limits) != {"run_wall_seconds", "job_wall_seconds", "disk_reserve_bytes", "archive_codec"}:
        raise ValueError("incomplete execution limits")
    number(limits["run_wall_seconds"], "run wall", 1)
    number(limits["job_wall_seconds"], "job wall", limits["run_wall_seconds"])
    integer(limits["disk_reserve_bytes"], "disk reserve", 2**30, 2**50)
    if limits["archive_codec"] not in {"gzip", "xz"}:
        raise ValueError("unsupported archive codec")
    layouts = design["validation_layouts"]
    if set(layouts) != {"VAL_CORRIDOR_A", "VAL_BYPASS_A", "VAL_STATION_A"}:
        raise ValueError("three distinct validation structures required")
    groups = [[design["canary_seed"]], design["development_seeds"], *layouts.values()]
    flat = []
    for index, seeds in enumerate(groups):
        if len(seeds) != (1 if index == 0 else 5):
            raise ValueError("one technical seed and five independent seeds per study layout required")
        for seed in seeds:
            integer(seed, "seed")
        flat.extend(seeds)
    if len(flat) != len(set(flat)):
        raise ValueError("technical, development and validation seeds must be disjoint")
    conditions = {condition for condition, _, _, _ in matrix(design)}
    for name, count in (("canary_conditions", 3), ("d0_conditions", 2), ("validation_conditions", 4)):
        rows = design[name]
        if len(rows) != count or len(set(rows)) != count or set(rows) - conditions:
            raise ValueError("invalid planned conditions: " + name)


def matrix(design: dict[str, Any]) -> list[tuple[str, str, int, int]]:
    """List all controlled load/slot/shape combinations in deterministic order."""
    return [
        (f"ADV_{load}_C{slots}_A{shape}", load, slots, shape)
        for shape in design["shapes"]
        for load in design["loads"]
        for slots in design["slots"]
    ]


def planned_inputs(design: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep all 155 physical inputs and 620 operating arms in the denominator."""
    rows = []

    def add(phase: str, layout: str, conditions: list[str], seeds: list[int], mode: str) -> None:
        for seed in seeds:
            for condition in conditions:
                rows.append({"phase": phase, "layout": layout, "condition": condition, "seed": seed, "mode": mode})

    add("M1_D1", "DEV_GRID_A", design["canary_conditions"], [design["canary_seed"]], "D1")
    add("M1_D0", "DEV_GRID_A", design["d0_conditions"], [design["canary_seed"]], "D0")
    for shape, phase in ((1, "M2a"), (4, "M2b")):
        add(phase, "DEV_GRID_A", [r[0] for r in matrix(design) if r[3] == shape], design["development_seeds"], "D1")
    for layout, seeds in design["validation_layouts"].items():
        add("M3", layout, design["validation_conditions"], seeds, "D1")
    return rows


def build_factory(design: dict[str, Any], layout: str, slots: int) -> FactoryConfig:
    """Build fixed synthetic geometry; bypass is a topology contrast with K=1."""
    factory = factory_for({"id": layout, "robots": 8}, {"charger_slots": slots}, design["common"])
    if layout == "DEV_GRID_A":
        return factory
    factory.nodes, factory.edges, factory.stations = [], [], []

    def node(nid: int, x: float, y: float, kind: str = "waypoint", linked: str | None = None) -> None:
        factory.nodes.append(PathNodeConfig(nid, x, y, kind, linked))

    def link(a: int, b: int, width: float = 5, both: bool = True) -> None:
        factory.edges.append(PathEdgeConfig(len(factory.edges) + 1, a, b, width=width, is_bidirectional=both))

    if layout in {"VAL_CORRIDOR_A", "VAL_BYPASS_A"}:
        for nid, x in enumerate((100, 260, 340, 500), 1):
            node(nid, x, 200)
        link(1, 2)
        link(2, 3, 1.3, layout != "VAL_BYPASS_A")
        link(3, 4)
        if layout == "VAL_BYPASS_A":
            node(5, 260, 60)
            node(6, 340, 60)
            link(2, 5, 3.1)
            link(5, 6, 3.1)
            link(6, 3, 3.1)
        coords = [(90, 90, 1), (170, 90, 1), (90, 310, 1), (510, 90, 4), (430, 310, 4), (510, 310, 4)]
        charger_x = 100
    elif layout == "VAL_STATION_A":
        for nid, x in enumerate((100, 400, 500), 1):
            node(nid, x, 200)
        link(1, 2)
        link(2, 3)
        coords = [(x, y, 2 if x == 420 else 3) for y in (90, 200, 310) for x in (420, 540)]
        charger_x = 80
    else:
        raise ValueError("unknown fixed layout: " + layout)
    for i, (x, y, target) in enumerate(coords):
        sid = f"S{i}"
        factory.stations.append(
            WorkStationConfig(
                sid,
                sid,
                x,
                y,
                task_type="inspection",
                avg_process_time=design["common"]["process_time_mean_s"],
                process_time_std=0,
            )
        )
        node(10 + i, x, y, "station", sid)
        link(10 + i, target)
    factory.chargers[0].x = charger_x
    node(90, charger_x, 350, "charger", "C0")
    link(90, 1)
    return factory


def case_for(design: dict[str, Any], row: dict[str, Any]) -> Scenario:
    """Use independent arrival, attribute and SOC streams, shared across controlled factors."""
    _, load, slots, shape = next(r for r in matrix(design) if r[0] == row["condition"])
    family = {
        "id": f"{row['layout']}:{load}",
        "robots": 8,
        "arrival_rate_tasks_per_s": design["load_reference_tasks_per_s"] * design["loads"][load],
        "urgent_fraction": 0.15,
        "deadline_slack_s": 120,
        "urgent_slack_s": 45,
    }
    jobs = jobs_for(family, {"burst_multiplier": shape}, row["seed"], design["duration"], design["warmup"])
    factory = build_factory(design, row["layout"], slots)
    charger = factory.chargers[0]
    rng = random.Random(f"{row['layout']}:{row['seed']}:soc")
    low, high = design["common"]["initial_soc_pct_range"]
    initial = {r.id: asdict(RobotInitial(charger.x, charger.y, soc_pct=rng.uniform(low, high))) for r in factory.robots}
    return Scenario.from_dict(
        {
            "schema": "clover-scenario-v1",
            "id": f"{row['layout']}_{row['condition']}_{row['seed']}",
            "units": {
                "coordinates": "px",
                "metres_per_pixel": 0.1,
                "dimensions": "m",
                "time": "sim-s",
                "energy": "battery-unit",
            },
            "factory": factory.to_dict(),
            "duration": design["duration"],
            "warmup": design["warmup"],
            "check_interval": 1,
            "initial_robots": initial,
            "initial_cabins": {},
            "plant": asdict(PlantSettings(work_drain_per_sec=design["common"]["work_drain_units_per_s"])),
            "workload": {"kind": "trace", "jobs": jobs},
            "interventions": [],
        }
    )


def tool_hashes() -> dict[str, str]:
    """Seal orchestration scripts too; they are outside the application's source hash."""
    return {name: file_sha(REPO / "scripts" / name) for name in TOOLS}


def materialize(output: Path, design: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create a new portable bundle without overwriting prior input or run evidence."""
    design = design if design is not None else json.loads(DESIGN.read_text(encoding="utf-8"))
    validate_design(design)
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs").mkdir()
    support = {
        "design.json": design,
        "rh_settings.json": asdict(Settings(**design["rh_settings"])),
        "d1_settings.json": DecisionSettings(**design["d1_settings"]).to_dict(),
    }
    for name, content in support.items():
        write_json(output / name, content)
    manifest = {
        "schema": "clover-static-study-bundle-v1",
        "source": source_manifest(),
        "code": code_identity(),
        "tools": tool_hashes(),
        "policies": POLICIES,
        "planned_runs": 620,
        "support_files": {name: file_sha(output / name) for name in support},
        "entries": [],
    }
    for index, row in enumerate(planned_inputs(design)):
        case = case_for(design, row)
        route_checks = validate_routes(case)
        relative = f"inputs/{index:03d}_{row['phase']}_{case.id}.json"
        write_json(output / relative, case.to_dict())
        manifest["entries"].append(
            {
                **row,
                "index": index,
                "scenario": relative,
                "sha256": file_sha(output / relative),
                "scenario_hash": fingerprint(case.to_dict()),
                "trace_hash": fingerprint(case.workload["jobs"]),
                "initial_hash": fingerprint(case.to_dict()["initial_robots"]),
                "jobs": len(case.workload["jobs"]),
                "route_checks": route_checks,
            }
        )
    write_json(output / "manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = materialize(args.output)
    print(json.dumps({"inputs": len(result["entries"]), "planned_runs": result["planned_runs"]}))
