"""All-policy staged D1, recorded replay and real task/energy conservation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.analysis import analyze
from simulation.evaluation.fixtures import examples, joint_batch
from simulation.evaluation.runner import replay, run_case
from simulation.evaluation.scenario import POLICIES


@pytest.mark.parametrize("policy", POLICIES)
@pytest.mark.parametrize("startup", ("on_demand", "eager"))
def test_all_policy_latency_real_execution_and_recorded_replay(policy: str, startup: str, tmp_path: Path) -> None:
    scenario = joint_batch()
    scenario.duration = 30
    result = run_case(
        scenario,
        policy,
        1001,
        output=tmp_path,
        planning_settings={
            "horizon": 40,
            "step": 2,
            "sigma": 0,
            "timeout": 5,
            "watchdog": 7,
            "native_actions": 4,
        },
        decision_settings=DecisionSettings(startup_mode=startup, publication_allowance_seconds=0.5).to_dict(),
    )
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["schema"] == "clover-headless-run-v5" and result["final_audit"]["ok"]
    if policy == "assignment_milp_legacy":
        assert "recorded validated decisions" in result["legacy_scope"]
    assert result["completed_total"] == 2, result["control"]
    decisions = result["control"]["decisions"]
    for decision in decisions:
        if decision["rh_result"] is not None:
            report = decision["rh_result"]["report"]
            assert report["worker_reused"] and report["parent_elapsed_seconds"] > 0
            assert report["stage_seconds"]["route_prepare"] > 0
    assert decisions and all(r["coverage_ok"] for r in decisions)
    assert not any(r["reason"].startswith("DECISION_ERROR") for r in decisions)
    assert result["control"]["lifecycle"][0]["reply"]["status"] == "READY"
    repeated = replay(json.loads(json.dumps(result)))
    assert repeated["matches"], repeated["result"]["failure"]
    assert repeated["result"]["physical_event_digest"] == result["physical_event_digest"]
    assert analyze(tmp_path / (result["case_id"] + ".json"))["stored_metrics_match"]


def test_pending_startup_at_end_replays_without_starting_new_worker() -> None:
    scenario = examples()[0]
    scenario.duration = 0.001
    result = run_case(scenario, "optimized", 1001, decision_settings=DecisionSettings().to_dict())
    assert result["status"] == "SUCCESS"
    assert any(r["time"] is None for r in result["control"]["decisions"])
    assert replay(result)["matches"]


def test_baseline_charges_after_work_with_a_queued_job_and_replays(tmp_path: Path) -> None:
    from dataclasses import replace

    from simulation.decisions.test_deadline_v2 import settings

    case = examples()[4]
    case.factory.robots = case.factory.robots[:1]
    case.factory.robots[0].charge_threshold = 30
    case.initial_robots = {"R0": replace(case.initial_robots["R0"], soc_pct=35)}
    case.duration, case.warmup = 240, 0
    case.workload = {"kind": "trace", "jobs": [
        {"uid": f"job{i}", "at": at, "station": "S0", "allowed": ["S0"], "operation": "inspection",
         "priority": 2, "noise": 0, "deadline": 240} for i, at in enumerate((3, 35, 140))]}
    result = run_case(case, "baseline", 1001, output=tmp_path, decision_settings=settings())
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["energy"]["charged"] > 0
    assert result["faults"] == 0 and result["energy"]["rescue"] == 0
    assert result["completed_total"] == 3
    assert replay(result, input_root=tmp_path)["matches"]
