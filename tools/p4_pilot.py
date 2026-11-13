"""Generate and run small P4 mechanism controls; never run the held-out phase."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "robot_factory_sim"
OUT = Path(os.environ.get("CLOVER_P4_OUTPUT", str(ROOT / "outputs/p4_pilot"))).resolve()
sys.path.insert(0, str(APP))

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.fixtures import examples, joint_batch
from simulation.evaluation.runner import run_suite, source_manifest
from simulation.evaluation.scenario import (
    POLICIES,
    Scenario,
    fingerprint,
    read_scenario,
)
from simulation.planning.contracts import Settings
from simulation.plant import RobotInitial
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


def burst(case: Scenario, groups: tuple[tuple[str, ...], ...], spacing: float) -> None:
    """Attach a fixed, externally reproducible arrival sequence."""
    jobs = []
    for g, stations in enumerate(groups):
        for i, station in enumerate(stations):
            jobs.append(
                {
                    "uid": f"g{g}-j{i}",
                    "at": g * spacing,
                    "station": station,
                    "allowed": [station],
                    "operation": "inspection",
                    "priority": 2,
                    "noise": 0,
                    "deadline": g * spacing + 90,
                }
            )
    case.workload = {"kind": "trace", "jobs": jobs}


def cases() -> list[tuple[str, str, Scenario]]:
    """Build six diagnostic families and matched capacity controls."""
    result = []
    basic = examples()
    open_case = copy.deepcopy(basic[0])
    open_case.id = "P4_S00_open"
    open_case.duration = 180
    open_case.factory.robots = [RobotConfig(f"R{i}", move_speed=1) for i in range(4)]
    open_case.initial_robots = {f"R{i}": RobotInitial(200, 200) for i in range(4)}
    open_case.factory.chargers[0].capacity = 4
    open_case.workload["rate"] = 0.04
    for edge in open_case.factory.edges:
        edge.width = 5
    result.append(("S00", "stream", open_case))
    joint = joint_batch()
    joint.id = "P4_S01_joint"
    for edge in joint.factory.edges:
        edge.width = 5
    result.append(("S01", "batch", joint))
    narrow = copy.deepcopy(basic[2])
    narrow.id = "P4_S02_narrow"
    narrow.duration = 180
    narrow.initial_robots = {
        rid: RobotInitial(50, 100) for rid in narrow.initial_robots
    }
    narrow.factory.edges[0].width = 1.3
    burst(narrow, (("S1", "S1"), ("S0", "S0"), ("S1", "S1")), 60)
    result.append(("S02", "burst", narrow))
    wide = copy.deepcopy(narrow)
    wide.id = "P4_S02_wide"
    wide.factory.edges[0].width = 3.1
    result.append(("S02", "burst", wide))
    junction = copy.deepcopy(basic[3])
    junction.id = "P4_S03_junction"
    junction.duration = 180
    xs = (50, 75, 100, 300, 325, 350)
    stations = [
        WorkStationConfig(
            f"S{i}",
            f"S{i}",
            x,
            200,
            task_type="inspection",
            avg_process_time=8,
            process_time_std=0,
        )
        for i, x in enumerate(xs)
    ]
    nodes = [
        PathNodeConfig(1, 200, 200, "intersection"),
        PathNodeConfig(2, 200, 300, "charger", "C"),
    ]
    nodes += [
        PathNodeConfig(10 + i, x, 200, "station", f"S{i}") for i, x in enumerate(xs)
    ]
    links = ((1, 2), (1, 12), (12, 11), (11, 10), (1, 13), (13, 14), (14, 15))
    junction.factory = FactoryConfig(
        FactoryProfile(floor_width=400, floor_height=400),
        stations,
        [ChargingStationConfig("C", 200, 300, capacity=4)],
        [RobotConfig(f"R{i}", move_speed=1) for i in range(3)],
        [],
        nodes,
        [PathEdgeConfig(i + 1, a, b, width=5) for i, (a, b) in enumerate(links)],
    )
    junction.initial_robots = {f"R{i}": RobotInitial(200, 300) for i in range(3)}
    burst(junction, (("S0", "S1", "S2"), ("S3", "S4", "S5"), ("S0", "S1", "S2")), 60)
    result.append(("S03", "burst", junction))
    free = copy.deepcopy(junction)
    free.id = "P4_S03_no_junction"
    free.factory.nodes[0].node_type = "waypoint"
    result.append(("S03", "burst", free))
    charging = copy.deepcopy(basic[4])
    charging.id = "P4_S04_one_slot"
    charging.duration = 180
    result.append(("S04", "stream", charging))
    ample = copy.deepcopy(charging)
    ample.id = "P4_S04_four_slots"
    ample.factory.chargers[0].capacity = 4
    result.append(("S04", "stream", ample))
    multi = copy.deepcopy(basic[5])
    multi.id = "P4_S08_one_cabin"
    multi.duration = 180
    multi.initial_robots = {rid: RobotInitial(100, 300) for rid in multi.initial_robots}
    for sid, floor, node in (("S2", 2, 7), ("S3", 1, 8)):
        multi.factory.stations.append(
            WorkStationConfig(
                sid,
                sid,
                300,
                300,
                floor=floor,
                task_type="inspection",
                avg_process_time=8,
                process_time_std=0,
            )
        )
        multi.factory.nodes.append(
            PathNodeConfig(node, 300, 300, "station", sid, floor=floor)
        )
    multi.factory.elevators.append(
        ElevatorConfig("E1", "E1", 220, 300, travel_time=4, is_active=False)
    )
    multi.factory.nodes.extend(
        (
            PathNodeConfig(9, 220, 300, "elevator", "E1"),
            PathNodeConfig(10, 220, 300, "elevator", "E1", floor=2),
        )
    )
    multi.factory.edges.extend(
        PathEdgeConfig(i, a, b, width=5)
        for i, a, b in (
            (5, 4, 7),
            (6, 1, 8),
            (7, 3, 9),
            (8, 6, 10),
            (9, 2, 9),
            (10, 9, 8),
            (11, 10, 7),
        )
    )
    multi.initial_cabins = {"E": 1, "E1": 1}
    burst(multi, (("S1", "S2"), ("S0", "S3")), 90)
    result.append(("S08", "burst", multi))
    two = copy.deepcopy(multi)
    two.id = "P4_S08_two_cabins"
    two.factory.elevators[-1].is_active = True
    result.append(("S08", "burst", two))
    for _, _, case in result:
        case.validate()
    return result


def prepare() -> dict:
    """Freeze code, generator, inputs and settings before any pilot run."""
    OUT.mkdir(exist_ok=True)
    directory = OUT / "inputs"
    directory.mkdir(exist_ok=True)
    rows = []
    for family, kind, case in cases():
        raw = json.dumps(case.to_dict(), ensure_ascii=False, indent=2) + "\n"
        path = directory / (case.id + ".json")
        if path.exists():
            assert path.read_text() == raw, "pilot input changed"
        else:
            path.write_text(raw)
        rows.append(
            {
                "family": family,
                "kind": kind,
                "id": case.id,
                "path": str(path.relative_to(OUT)),
                "scenario_hash": fingerprint(case.to_dict()),
                "file_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "duration": case.duration,
                "warmup": case.warmup,
                "robots": len(case.factory.robots),
                "nodes": len(case.factory.nodes),
                "edges": len(case.factory.edges),
            }
        )
    quality = asdict(
        Settings(
            horizon=40,
            step=2,
            sigma=0,
            timeout=2,
            watchdog=3,
            max_tasks=8,
            native_actions=4,
        )
    )
    decision = DecisionSettings(
        end_to_end_soft_deadline_seconds=2,
        request_watchdog_seconds=3,
        publication_allowance_seconds=0.2,
    ).to_dict()
    protocol = {
        "schema": "clover-p4-pilot-protocol-v1",
        "source_hash": source_manifest()["source_hash"],
        "generator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "seeds": [1001, 1002],
        "policies": list(POLICIES),
        "inputs": rows,
        "math_settings": quality,
        "decision_settings": decision,
        "per_run_wall_limit_seconds": 240,
        "simultaneous_solver_workers": 1,
        "scope": "small executable diagnostic graphs adapted from team families; not the team's final 64/256-node layouts",
        "interpretation": "batch makespan, finite burst and short Poisson stream are separate; no CI or general policy recommendation",
        "heldout": "not executed; P5 requires new layouts/traces and locked final protocol before opening E2",
    }
    target = OUT / "protocol.json"
    text = json.dumps(protocol, ensure_ascii=False, indent=2) + "\n"
    if target.exists():
        assert target.read_text() == text, "pilot protocol/source changed"
    else:
        target.write_text(text)
    return protocol


def run(group: str) -> None:
    """Execute a paired D0 or staged-D1 pilot with one worker at a time."""
    protocol = prepare()
    selected = [read_scenario(OUT / r["path"]) for r in protocol["inputs"]]
    settings = protocol["decision_settings"] if group == "d1" else None
    result = run_suite(
        selected,
        (1001, 1002),
        OUT / group,
        policies=POLICIES,
        planning_settings=protocol["math_settings"],
        decision_settings=settings,
        run_wall_limit=240,
    )
    print("PILOT", group, result["status"], len(result["runs"]), flush=True)


def duration_probe() -> None:
    """Explore finite observation duration without claiming steady state."""
    protocol = prepare()
    for rate in (0.04, 0.08):
        for duration in (600, 1800):
            case = examples()[4]
            case.id = f"P4_charge_rate{int(rate * 100):02}_T{duration}"
            case.duration = duration
            case.workload["rate"] = rate
            result = run_suite(
                (case,),
                (1001,),
                OUT / f"duration_{int(rate * 100)}_{duration}",
                policies=POLICIES,
                planning_settings=protocol["math_settings"],
                decision_settings=protocol["decision_settings"],
                run_wall_limit=600,
            )
            print("DURATION", case.id, result["status"], flush=True)


def budgets() -> None:
    """Compare cold-start modes and end-to-end budgets on discovery inputs."""
    protocol = prepare()
    selected = (joint_batch(), examples()[4])
    selected[1].duration = 180
    for budget in (0.5, 10):
        for startup in ("on_demand", "eager"):
            cfg = {
                **protocol["decision_settings"],
                "startup_mode": startup,
                "end_to_end_soft_deadline_seconds": budget,
                "request_watchdog_seconds": budget + 2,
            }
            math = {
                **protocol["math_settings"],
                "timeout": budget,
                "watchdog": budget + 2,
            }
            result = run_suite(
                selected,
                (1001,),
                OUT / f"budget_{budget}_{startup}",
                policies=POLICIES,
                planning_settings=math,
                decision_settings=cfg,
                run_wall_limit=240,
            )
            print("BUDGET", budget, startup, result["status"], flush=True)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "prepare"
    if action == "prepare":
        print(json.dumps(prepare(), ensure_ascii=False, indent=2))
    elif action in {"d0", "d1"}:
        run(action)
    elif action == "duration":
        duration_probe()
    elif action == "budget":
        budgets()
    else:
        raise ValueError(action)
