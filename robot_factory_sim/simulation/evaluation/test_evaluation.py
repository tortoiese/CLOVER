"""Replay, input fairness, real CLI operation and retained partial-result acceptance."""

from __future__ import annotations

import copy
import csv
import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import replay, run_case, run_suite
from simulation.evaluation.scenario import (
    Demand,
    Intervention,
    Scenario,
    fingerprint,
    make_trace,
)
from simulation.plant import PlantSettings, RobotInitial


def tiny() -> Scenario:
    """Short but real physical case for execution acceptance."""
    scenario = examples()[0]
    scenario.duration = 40
    scenario.workload = {"kind": "poisson", "rate": 0.4, "fixed_station": True}
    return scenario


def test_paired_inputs_and_physical_replay_are_exact(tmp_path: Path) -> None:
    scenario = tiny()
    before = fingerprint(scenario.to_dict())
    trace = make_trace(scenario, 1001)
    results = [run_case(scenario, policy, 1001, trace=trace, output=tmp_path) for policy in ("baseline", "optimized")]
    assert all(r["status"] == "SUCCESS" and r["final_audit"]["ok"] for r in results)
    assert results[0]["trace_hash"] == results[1]["trace_hash"]
    assert results[0]["initial_hash"] == results[1]["initial_hash"]
    assert fingerprint(scenario.to_dict()) == before
    assert all(r["offered"] == len(trace) for r in results)
    loaded = json.loads((tmp_path / "open__baseline__1001.json").read_text())
    assert replay(loaded)["matches"]
    events = [json.loads(line) for line in (tmp_path / "open__baseline__1001.events.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "task_offered" for row in events) == len(trace)
    assert [row["sequence"] for row in events] == list(range(len(events)))
    tampered = copy.deepcopy(loaded)
    tampered["trace"][0]["noise"] += 1
    with pytest.raises(ValueError, match="trace hash"):
        replay(tampered)


def test_empty_trace_has_no_internal_demand() -> None:
    scenario = tiny()
    scenario.workload = {"kind": "trace", "jobs": []}
    result = run_case(scenario, "optimized", 1)
    assert result["status"] == "SUCCESS" and result["offered"] == result["completed_total"] == 0
    assert result["arrival_to_start"]["p95"] is None and result["late_due_rate"] is None


@pytest.mark.parametrize("fixed_station", [True, False])
def test_initial_station_outage_does_not_change_external_offered_demand(fixed_station: bool) -> None:
    scenario = tiny()
    scenario.workload["fixed_station"] = fixed_station
    expected = make_trace(scenario, 1001)
    assert {job.station for job in expected} == {"S0", "S1"}
    scenario.factory.stations[0].is_active = False
    assert make_trace(scenario, 1001) == expected
    scenario.factory.stations[1].is_active = False
    assert make_trace(scenario, 1001) == expected
    scenario.interventions = (Intervention(10, "station_active", "S0", True),)
    result = run_case(scenario, "optimized", 1001)
    assert result["status"] == "SUCCESS" and result["offered"] == len(expected)


@pytest.mark.parametrize("station_count", [1, 3])
def test_destination_count_cannot_change_arrival_or_service_noise_stream(station_count: int) -> None:
    scenario = tiny()
    expected = make_trace(scenario, 1001)
    if station_count == 1:
        scenario.factory.stations = scenario.factory.stations[:1]
    else:
        scenario.factory.stations.append(replace(scenario.factory.stations[0], id="S2", label="third"))
    actual = make_trace(scenario, 1001)
    assert [(job.uid, job.at, job.noise) for job in actual] == [(job.uid, job.at, job.noise) for job in expected]


def test_unfinished_due_work_counts_as_missed_not_success() -> None:
    scenario = tiny()
    scenario.duration = 2
    scenario.workload = {"kind": "trace", "jobs": [asdict(Demand("due", 0, "S0", ("S0",), "inspection", deadline=1))]}
    result = run_case(scenario, "baseline", 1)
    assert result["status"] == "SUCCESS" and result["unfinished"] == 1
    assert result["due_jobs"] == result["late_due_jobs"] == 1 and result["late_due_rate"] == 1
    assert result["censored_start_count"] == 1


def test_cancelled_inflight_job_keeps_energy_and_cannot_complete() -> None:
    scenario = tiny()
    scenario.workload = {"kind": "trace", "jobs": [asdict(Demand("cancel", 0, "S0", ("S0",), "inspection"))]}
    scenario.interventions = (Intervention(1, "cancel_task", "cancel"),)
    result = run_case(scenario, "baseline", 1)
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["cancelled"] == 1 and result["completed_total"] == result["unfinished"] == 0
    assert result["energy"]["consumed"] > 0 and result["max_energy_error"] < 1e-8


def test_layout_drain_cancels_waiting_bundle_and_preserves_jobs(tmp_path: Path) -> None:
    scenario = examples()[2]
    scenario.duration = 60
    scenario.workload = {
        "kind": "trace",
        "jobs": [
            asdict(Demand("east", 0, "S1", ("S1",), "inspection")),
            asdict(Demand("west", 0, "S0", ("S0",), "inspection")),
        ],
    }
    scenario.interventions = (Intervention(1, "edge_width", "1", 2.2),)
    result = run_case(scenario, "baseline", 1, output=tmp_path)
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["offered"] == result["completed_total"] == 2
    events = [json.loads(line) for line in (tmp_path / "narrow__baseline__1.events.jsonl").read_text().splitlines()]
    assert any(row["event"] == "traffic" and row["action"] == "invalidated" for row in events)
    assert all(
        r["occupied"] <= r["capacity"]
        for row in events
        if row["event"] == "traffic" and row["action"] == "granted"
        for r in row["bundle"]
    )


def test_real_fault_rescue_with_fixed_trace_preserves_jobs_and_energy() -> None:
    scenario = tiny()
    scenario.factory.robots = scenario.factory.robots[:1]
    scenario.factory.robots[0].charge_threshold = 0
    scenario.initial_robots = {"R0": RobotInitial(100, 200, soc_pct=0.6)}
    scenario.duration = 90
    scenario.plant = PlantSettings(0.05, 5, 15)
    scenario.workload = {
        "kind": "trace",
        "jobs": [
            asdict(Demand("first", 0, "S0", ("S0",), "inspection")),
            asdict(Demand("second", 2, "S1", ("S1",), "inspection")),
        ],
    }
    result = run_case(scenario, "baseline", 1)
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["faults"] > 0 and result["energy"]["rescue"] > 0
    assert result["max_energy_error"] < 1e-8 and result["final_audit"]["ok"]


def test_warmup_window_uses_exact_wip_integral_and_excludes_early_completions() -> None:
    scenario = tiny()
    scenario.duration, scenario.warmup = 5, 1
    scenario.workload = {"kind": "trace", "jobs": [asdict(Demand("wip", 0, "S0", ("S0",), "inspection"))]}
    result = run_case(scenario, "baseline", 1)
    assert result["status"] == "SUCCESS"
    assert result["wip_integral"] == pytest.approx(4) and result["mean_wip"] == pytest.approx(1)


def test_timeout_and_unstarted_budget_cases_are_not_dropped(tmp_path: Path) -> None:
    scenario = tiny()
    timed = run_case(scenario, "baseline", 1, wall_limit=0)
    assert timed["status"] == "TIMEOUT" and timed["scheduled_arrivals"] > 0
    manifest = run_suite((scenario,), (1, 2), tmp_path / "not_started", suite_wall_limit=0)
    assert manifest["status"] == "PARTIAL" and len(manifest["runs"]) == 4
    assert all(row["status"] == "NOT_RUN_BUDGET" for row in manifest["runs"])
    with (tmp_path / "not_started/summary.csv").open(newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 4


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(units={}),
        lambda r: r.update(workload="not-an-object"),
        lambda r: r.update(duration=float("nan")),
        lambda r: r.update(initial_robots={}),
        lambda r: r["factory"]["robots"][0].update(move_speed=0),
        lambda r: r["factory"]["edges"][0].update(to_node=999),
        lambda r: r["factory"]["chargers"][0].update(capacity=True),
    ],
)
def test_invalid_inputs_fail_before_running(mutation: Callable[[dict], None]) -> None:
    raw = tiny().to_dict()
    mutation(raw)
    with pytest.raises(ValueError):
        Scenario.from_dict(raw)


def test_cli_runs_without_gui_rag_or_scipy_imports(tmp_path: Path) -> None:
    scenario_path = tmp_path / "input.json"
    scenario = tiny()
    scenario.duration = 5
    scenario_path.write_text(json.dumps(scenario.to_dict()))
    script = """
import sys
class Block:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PySide6', 'pyqtgraph', 'chromadb', 'scipy', 'matplotlib'}:
            raise ImportError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, Block())
from simulation.evaluation.__main__ import main
raise SystemExit(main(sys.argv[1:]))
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            script,
            "run",
            "--scenario",
            str(scenario_path),
            "--output",
            str(tmp_path / "cli"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=20,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["status"] == "SUCCESS"
    assert "Exception ignored" not in proc.stderr and "closed file" not in proc.stderr
