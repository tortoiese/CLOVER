"""Build portable case-inspired inputs; no solver runs, GUI, network, or live DB."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "robot_factory_sim"))

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.scenario import (
    POLICIES,
    Scenario,
    fingerprint,
    integer,
    number,
)
from simulation.evaluation.study import source_manifest
from simulation.path_graph import PathGraph
from simulation.planning.contracts import Settings
from simulation.plant import PlantSettings, RobotInitial
from utils.config_models import (
    ChargingStationConfig,
    ElevatorConfig,
    FactoryConfig,
    FactoryProfile,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)

DATA = REPO / "docs/scenarios/korean_cases_20261117"
PARAMETERS = {
    "temporary_restriction",
    "speed_mix",
    "hotspot_share",
    "burst_multiplier",
    "charger_slots",
    "floors",
    "cabins",
    "distributed_charging",
}
DEFAULT_PARAMETERS = {
    "temporary_restriction": False,
    "speed_mix": False,
    "hotspot_share": 1 / 6,
    "burst_multiplier": 1,
    "charger_slots": 4,
    "floors": 1,
    "cabins": 1,
    "distributed_charging": False,
}
AXIS_PARAMETER = {
    "edge_width_at_900_to_1200": "temporary_restriction",
    "speed_distribution_same_arithmetic_mean": "speed_mix",
    "destination_distribution": "hotspot_share",
    "arrival_shape_equal_realized_count": "burst_multiplier",
    "charger_slots": "charger_slots",
    "floor_reallocation_same_total_stations_and_robots": "floors",
    "active_cabins": "cabins",
    "charger_location_same_total_slots": "distributed_charging",
}
COMMON_KEYS = {
    "floor_width_px",
    "floor_height_px",
    "robot_battery_capacity_units",
    "robot_width_m",
    "robot_length_m",
    "travel_drain_units_per_m",
    "work_drain_units_per_s",
    "charge_rate_units_per_s_per_slot",
    "initial_soc_pct_range",
    "process_time_mean_s",
    "process_time_std_s",
    "edge_width_m",
    "elevator_floor_travel_s",
    "elevator_door_s",
}
FAMILY_KEYS = {
    "id",
    "source_id",
    "robots",
    "arrival_rate_tasks_per_s",
    "urgent_fraction",
    "deadline_slack_s",
    "urgent_slack_s",
    "battery_capacity_units",
    "charge_rate_units_per_s_per_slot",
    "controlled_edge_width_m",
}


def validate_design(design: dict[str, Any], sources: dict[str, Any]) -> None:
    """Reject invalid values and undeclared multiple-factor comparisons early."""
    if (
        design.get("schema") != "clover-korean-case-design-v2"
        or design.get("all_numeric_inputs_are_synthetic") is not True
    ):
        raise ValueError("explicit synthetic pilot v2 design required")
    seeds, policies = design["seeds"], design["policies"]
    if not seeds or len(seeds) != len(set(seeds)):
        raise ValueError("unique nonempty demand seeds required")
    for seed in seeds:
        integer(seed, "demand seed")
    if (
        not policies
        or len(policies) != len(set(policies))
        or any(p not in POLICIES for p in policies)
    ):
        raise ValueError("unique supported policies required")
    duration = number(design["duration"], "duration", 0.001, 86400)
    warmup = number(design["warmup"], "warmup", 0, duration)
    if warmup >= duration:
        raise ValueError("warmup must precede observation end")
    Settings(**design["rh_settings"]).validate()
    if design["rh_settings"].get("timing_mode", "D0") != "D0":
        raise ValueError(
            "D1 uses decision settings; prototype RH D1 is unsupported here"
        )
    DecisionSettings(**design["d1_settings"]).validate()
    common = design["common"]
    if set(common) != COMMON_KEYS:
        raise ValueError("unknown or missing common input key")
    for key, value in common.items():
        if key != "initial_soc_pct_range":
            number(value, key, 0 if key == "process_time_std_s" else 0.000001)
    low, high = common["initial_soc_pct_range"]
    number(low, "initial SOC lower", 0, 100)
    number(high, "initial SOC upper", low, 100)
    families = {f["id"]: f for f in design["families"]}
    if len(families) != len(design["families"]) or not families:
        raise ValueError("unique nonempty families required")
    source_ids = {s["id"] for s in sources["sources"]}
    for family in families.values():
        if set(family) - FAMILY_KEYS:
            raise ValueError("unknown family input key")
        if family["source_id"] not in source_ids:
            raise ValueError("every family needs an established source id")
        integer(family["robots"], "robots", 1, 64)
        number(family["arrival_rate_tasks_per_s"], "arrival rate", 0.000001, 100)
        number(family["urgent_fraction"], "urgent fraction", 0, 1)
        for key in (
            "deadline_slack_s",
            "urgent_slack_s",
            "battery_capacity_units",
            "charge_rate_units_per_s_per_slot",
            "controlled_edge_width_m",
        ):
            if key in family:
                number(family[key], key, 0.000001)
        if "controlled_edge_width_m" in family and not (
            warmup < 900 and 1200 < duration
        ):
            raise ValueError(
                "control and sham updates must be inside measurement window"
            )
    rows = {c["id"]: c for c in design["conditions"]}
    if not rows or len(rows) != len(design["conditions"]):
        raise ValueError("unique nonempty conditions required")
    for condition in rows.values():
        family_id, params = condition["family"], condition["parameters"]
        if family_id not in families or set(params) - PARAMETERS:
            raise ValueError("unknown family or parameter")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", condition["id"]):
            raise ValueError(
                "condition id must be a safe identifier of at most40 characters"
            )
        p = {**DEFAULT_PARAMETERS, **params}
        for key in ("temporary_restriction", "speed_mix", "distributed_charging"):
            if type(p[key]) is not bool:
                raise ValueError(key + " must be boolean")
        integer(p["floors"], "floors", 1, 3)
        integer(p["cabins"], "cabins", 1, 2)
        integer(p["charger_slots"], "charger slots", 1, 64)
        number(p["hotspot_share"], "hotspot share", 0, 1)
        number(p["burst_multiplier"], "burst multiplier", 1, 9.999999)
        if p["speed_mix"] and families[family_id]["robots"] % 4:
            raise ValueError("speed mix requires complete four-speed groups")
        if p["distributed_charging"] and p["floors"] != 3:
            raise ValueError("three-slot distributed charging requires three floors")
        if p["temporary_restriction"] and not (warmup < 900 and 1200 < duration):
            raise ValueError(
                "restriction and recovery must be inside measurement window"
            )
        if p["temporary_restriction"] and p["floors"] != 1:
            raise ValueError(
                "restriction target is defined only on the single-floor map"
            )
        if (
            p["temporary_restriction"]
            and "controlled_edge_width_m" not in families[family_id]
        ):
            raise ValueError("restriction requires matching sham control updates")
        parent_id, axis = condition["compare_to"], condition["changed_axis"]
        if parent_id is None:
            if axis is not None:
                raise ValueError("anchor cannot declare a comparison axis")
            continue
        if (
            parent_id not in rows
            or rows[parent_id]["family"] != family_id
            or parent_id == condition["id"]
        ):
            raise ValueError("comparison needs a distinct parent in the same family")
        old = {**DEFAULT_PARAMETERS, **rows[parent_id]["parameters"]}
        delta = {k for k in p if p[k] != old[k]}
        if axis not in AXIS_PARAMETER or delta != {AXIS_PARAMETER[axis]}:
            raise ValueError("comparison must change exactly its declared axis")
    for start in rows:
        seen, current = set(), start
        while current is not None:
            if current in seen:
                raise ValueError("comparison parent cycle")
            seen.add(current)
            current = rows[current]["compare_to"]


def write_json(path: Path, value: Any) -> None:
    """Write deterministic UTF-8 bytes without overwriting an existing file."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def file_sha(path: Path) -> str:
    """Hash stored bytes rather than a different JSON representation."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def factory_for(
    family: dict[str, Any], parameters: dict[str, Any], common: dict[str, Any]
) -> FactoryConfig:
    """Construct synthetic service destinations and explicit shared corridors."""
    floors = parameters.get("floors", 1)
    if floors not in {1, 2, 3}:
        raise ValueError("pilot supports only one, two or three floors")
    profile = FactoryProfile(
        factory_name=f"{family['id']} synthetic case-inspired pilot",
        floor_width=common["floor_width_px"],
        floor_height=common["floor_height_px"],
        floor_count=floors,
        floor_travel_sec=common["elevator_floor_travel_s"],
        door_time_sec=common["elevator_door_s"],
    )
    robots = [
        RobotConfig(
            f"R{i}",
            move_speed=1.05,
            battery_capacity=family.get(
                "battery_capacity_units", common["robot_battery_capacity_units"]
            ),
            battery_drain_rate=common["travel_drain_units_per_m"],
            width=common["robot_width_m"],
            length=common["robot_length_m"],
        )
        for i in range(family["robots"])
    ]
    if parameters.get("speed_mix"):
        for i, robot in enumerate(robots):
            robot.move_speed = (0.6, 0.9, 1.2, 1.5)[i % 4]
    stations, nodes, edges, chargers, elevators = [], [], [], [], []

    def link(a: int, b: int, width: float | None = None) -> None:
        edges.append(
            PathEdgeConfig(len(edges) + 1, a, b, width=width or common["edge_width_m"])
        )

    for floor in range(1, floors + 1):
        base = 100 * floor
        if floors == 1:
            nodes.extend(
                PathNodeConfig(base + i + 1, x, 200, "waypoint")
                for i, x in enumerate((100, 300, 500))
            )
            link(base + 1, base + 2, family.get("controlled_edge_width_m"))
            link(base + 2, base + 3)
            coordinates = [(x, y) for y in (100, 300) for x in (100, 300, 500)]
        else:
            for i, x in enumerate((180, 420)):
                nodes.append(
                    PathNodeConfig(
                        base + i + 1, x, 200, "elevator", f"E{i}", floor=floor
                    )
                )
            link(base + 1, base + 2)
            coordinates = (
                [(80, 100), (520, 100)]
                if floors == 3
                else [(80, 100), (300, 100), (520, 100)]
            )
        for local, (x, y) in enumerate(coordinates):
            sid = f"S{len(stations)}"
            stations.append(
                WorkStationConfig(
                    sid,
                    f"Service destination {sid}",
                    x,
                    y,
                    floor=floor,
                    task_type="inspection",
                    avg_process_time=common["process_time_mean_s"],
                    process_time_std=common["process_time_std_s"],
                )
            )
            nid = base + 10 + local
            nodes.append(PathNodeConfig(nid, x, y, "station", sid, floor=floor))
            target = (
                base + (local % 3) + 1 if floors == 1 else base + (1 if x <= 300 else 2)
            )
            link(nid, target)
        cid = f"C{floor - 1}"
        distributed = parameters.get("distributed_charging", False)
        active = floor == 1 or distributed
        slots = (
            (1 if distributed else 3)
            if floors > 1
            else parameters.get("charger_slots", 4)
        )
        chargers.append(
            ChargingStationConfig(
                cid,
                300,
                350,
                capacity=slots,
                charge_rate=family.get(
                    "charge_rate_units_per_s_per_slot",
                    common["charge_rate_units_per_s_per_slot"],
                ),
                floor=floor,
                is_active=active,
            )
        )
        nodes.append(PathNodeConfig(base + 90, 300, 350, "charger", cid, floor=floor))
        link(base + 90, base + (2 if floors == 1 else 1))
    if floors > 1:
        elevators = [
            ElevatorConfig(
                f"E{i}",
                f"Synthetic independent cabin {i}",
                x,
                200,
                travel_time=common["elevator_floor_travel_s"],
                is_active=i < parameters.get("cabins", 1),
            )
            for i, x in enumerate((180, 420))
        ]
    return FactoryConfig(
        profile=profile,
        robots=robots,
        stations=stations,
        chargers=chargers,
        nodes=nodes,
        edges=edges,
        elevators=elevators,
    )


def burst_time(at: float, warmup: float, duration: float, multiplier: float) -> float:
    """Redistribute an equal realized job count, preserving warmup arrivals."""
    if at < warmup or multiplier == 1:
        return at
    fraction = (at - warmup) / (duration - warmup)
    off = (1 - multiplier * 0.1) / 0.9
    left, right = 0.45 * off, 0.45 * off + 0.1 * multiplier
    if fraction < left:
        mapped = fraction / off
    elif fraction < right:
        mapped = 0.45 + (fraction - left) / multiplier
    else:
        mapped = 0.55 + (fraction - right) / off
    return warmup + mapped * (duration - warmup)


def jobs_for(
    family: dict[str, Any],
    parameters: dict[str, Any],
    seed: int,
    duration: float,
    warmup: float,
) -> list[dict[str, Any]]:
    """Materialize external jobs without querying any policy or simulator result."""
    arrivals = random.Random(f"{family['id']}:{seed}:arrivals")
    attributes = random.Random(f"{family['id']}:{seed}:attributes")
    jobs = []
    at = arrivals.expovariate(family["arrival_rate_tasks_per_s"])
    while at < duration:
        destination, priority_draw, noise = (
            attributes.random(),
            attributes.random(),
            attributes.gauss(0, 1),
        )
        hotspot = parameters.get("hotspot_share", 1 / 6)
        index = (
            0
            if destination < hotspot
            else 1 + min(4, int((destination - hotspot) / (1 - hotspot) * 5))
        )
        station = f"S{index}"
        priority = 0 if priority_draw < family["urgent_fraction"] else 2
        release = burst_time(
            at, warmup, duration, parameters.get("burst_multiplier", 1)
        )
        slack = (
            family["urgent_slack_s"] if priority == 0 else family["deadline_slack_s"]
        )
        jobs.append(
            {
                "uid": f"{family['id']}:{seed}:{len(jobs)}",
                "at": release,
                "station": station,
                "allowed": [station],
                "operation": "inspection",
                "priority": priority,
                "noise": noise,
                "deadline": release + slack,
            }
        )
        at += arrivals.expovariate(family["arrival_rate_tasks_per_s"])
    return jobs


def validate_routes(case: Scenario) -> int:
    """Check every initial/service location can reach all services and an active charger."""
    graph = PathGraph(case.factory)
    checks = 0
    for robot in case.factory.robots:
        initial = case.initial_robots[robot.id]
        for station in case.factory.stations:
            if (
                graph.route_to_facility(
                    initial.x, initial.y, station.id, initial.floor, robot.width
                )
                is None
            ):
                raise ValueError(f"unreachable service: {robot.id}/{station.id}")
            checks += 1
            if not any(
                graph.route_to_facility(
                    station.x, station.y, charger.id, station.floor, robot.width
                )
                is not None
                for charger in case.factory.chargers
                if charger.is_active
            ):
                raise ValueError(f"no charging return route: {robot.id}/{station.id}")
            checks += 1
    return checks


def materialize(output: Path) -> dict[str, Any]:
    """Build a new input directory and retain hashes, treatments and limitations."""
    design = json.loads((DATA / "design.json").read_text(encoding="utf-8"))
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    validate_design(design, sources)
    seeds = design["seeds"]
    if len(seeds) != len(set(seeds)) or not seeds:
        raise ValueError("unique nonempty demand seeds required")
    for seed in seeds:
        integer(seed, "demand seed")
    Settings(**design["rh_settings"]).validate()
    DecisionSettings(**design["d1_settings"]).validate()
    families = {f["id"]: f for f in design["families"]}
    source_ids = {s["id"] for s in sources["sources"]}
    if any(f["source_id"] not in source_ids for f in families.values()):
        raise ValueError("every family needs an established source id")
    conditions = design["conditions"]
    if len({c["id"] for c in conditions}) != len(conditions):
        raise ValueError("duplicate condition")
    for condition in conditions:
        if (
            condition["family"] not in families
            or set(condition["parameters"]) - PARAMETERS
        ):
            raise ValueError("unknown family or parameter")
    output.mkdir(parents=True, exist_ok=False)
    (output / "inputs").mkdir()
    manifest = {
        "schema": "clover-korean-case-pilot-v2",
        "status": design["status"],
        "source_hash": source_manifest()["source_hash"],
        "generator_sha256": file_sha(Path(__file__)),
        "design_sha256": file_sha(DATA / "design.json"),
        "sources_sha256": file_sha(DATA / "sources.json"),
        "design_content_hash": fingerprint(design),
        "sources_content_hash": fingerprint(sources),
        "policies": design["policies"],
        "entries": [],
        "source_claim": "case-inspired synthetic; not a site replica",
        "timing": "D0 mechanism first; D1 separately retains the existing timing contract and failures",
    }
    built = {}
    for seed in seeds:
        for condition in conditions:
            family, params = families[condition["family"]], condition["parameters"]
            factory = factory_for(family, params, design["common"])
            soc = random.Random(f"{family['id']}:{seed}:soc")
            low, high = design["common"]["initial_soc_pct_range"]
            initial = {
                r.id: RobotInitial(
                    300 if factory.floor_count == 1 else 180,
                    350 if factory.floor_count == 1 else 200,
                    floor=i % factory.floor_count + 1,
                    soc_pct=soc.uniform(low, high),
                )
                for i, r in enumerate(factory.robots)
            }
            jobs = jobs_for(family, params, seed, design["duration"], design["warmup"])
            events = (
                [
                    {"at": t, "kind": "edge_width", "target": "1", "value": width}
                    for t, width in (
                        (
                            900,
                            1.3
                            if params.get("temporary_restriction")
                            else family["controlled_edge_width_m"],
                        ),
                        (
                            1200,
                            family.get(
                                "controlled_edge_width_m",
                                design["common"]["edge_width_m"],
                            ),
                        ),
                    )
                ]
                if "controlled_edge_width_m" in family
                else []
            )
            case = Scenario.from_dict(
                {
                    "schema": "clover-scenario-v1",
                    "id": f"{condition['id']}_{seed}",
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
                    "initial_robots": {r: asdict(v) for r, v in initial.items()},
                    "initial_cabins": {e.id: 1 for e in factory.elevators},
                    "plant": asdict(
                        PlantSettings(
                            work_drain_per_sec=design["common"][
                                "work_drain_units_per_s"
                            ]
                        )
                    ),
                    "workload": {"kind": "trace", "jobs": jobs},
                    "interventions": events,
                }
            )
            route_checks = validate_routes(case)
            data = case.to_dict()
            relative = f"inputs/{case.id}.json"
            write_json(output / relative, data)
            manifest["entries"].append(
                {
                    "condition": condition["id"],
                    "family": family["id"],
                    "seed": seed,
                    "source_id": family["source_id"],
                    "scenario": relative,
                    "sha256": file_sha(output / relative),
                    "scenario_hash": fingerprint(data),
                    "trace_hash": fingerprint(jobs),
                    "jobs": len(jobs),
                    "floors": factory.floor_count,
                    "robots": len(factory.robots),
                    "route_checks": route_checks,
                    "compare_to": condition["compare_to"],
                    "changed_axis": condition["changed_axis"],
                }
            )
            built[(condition["id"], seed)] = data
    # Treatment comparisons must keep the external jobs unless that is the declared axis.
    for condition in conditions:
        parent = condition["compare_to"]
        if parent is None:
            continue
        for seed in seeds:
            old, new = built[(parent, seed)], built[(condition["id"], seed)]
            axis = condition["changed_axis"]
            if axis not in {
                "destination_distribution",
                "arrival_shape_equal_realized_count",
            }:
                if old["workload"] != new["workload"]:
                    raise ValueError("non-demand treatment changed external trace")
            elif axis == "arrival_shape_equal_realized_count":
                for a, b in zip(
                    old["workload"]["jobs"], new["workload"]["jobs"], strict=True
                ):
                    if {k: v for k, v in a.items() if k not in {"at", "deadline"}} != {
                        k: v for k, v in b.items() if k not in {"at", "deadline"}
                    }:
                        raise ValueError("burst changed job attributes")
                    if not math.isclose(
                        a["deadline"] - a["at"], b["deadline"] - b["at"]
                    ):
                        raise ValueError("burst changed input-based deadline slack")
            if (
                "floor_reallocation" not in axis
                and old["initial_robots"] != new["initial_robots"]
            ):
                raise ValueError("treatment changed initial robot state")
    write_json(output / "rh_settings.json", asdict(Settings(**design["rh_settings"])))
    write_json(
        output / "d1_settings.json", DecisionSettings(**design["d1_settings"]).to_dict()
    )
    write_json(output / "design.json", design)
    write_json(output / "sources.json", sources)
    manifest["support_files"] = {
        name: file_sha(output / name)
        for name in (
            "rh_settings.json",
            "d1_settings.json",
            "design.json",
            "sources.json",
        )
    }
    manifest["planned_policy_runs_per_timing_mode"] = len(manifest["entries"]) * len(
        manifest["policies"]
    )
    write_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    """Materialize explicit pilot inputs, with failure returning nonzero."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="new directory; never overwrite"
    )
    args = parser.parse_args()
    result = materialize(args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "cases": len(result["entries"]),
                "policy_runs_per_timing_mode": result[
                    "planned_policy_runs_per_timing_mode"
                ],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
