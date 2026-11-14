"""Study leakage, immutable phase analysis, failure accounting and charge audits."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from simulation.decisions.contracts import DEADLINE_PROTOCOL, DecisionSettings
from simulation.evaluation.calibration import calibrate, validate_window
from simulation.evaluation.comparison import compare_plan, validate_plan
from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import run_case
from simulation.evaluation.study import freeze_study, load_frozen, run_study, study_plan, validate_spec
from simulation.planning.contracts import Settings


def window(duration: float, *, sessions: int = 0, nominal: bool = True) -> dict:
    """Declare a finite-time window, without claiming stationary operation."""
    return {"mode": "finite_stream", "arrival_cutoff": duration, "drain_seconds": 0,
            "drain_in_measurement": True, "minimum_observation_seconds": duration,
            "minimum_completed_charge_sessions_per_robot": sessions, "minimum_charge_fraction": .1,
            "nominal": nominal}


def spec() -> dict:
    """Small paired input with genuinely separate held-out geometry and seed."""
    cases = []
    for i, phase in enumerate(("training", "validation", "heldout", "stress")):
        case = examples()[0]
        case.id = "case_" + phase
        case.duration = 40
        case.factory.nodes[1].x += i * 5
        case.factory.stations[0].x += i * 5
        cases.append({"id": phase, "phase": phase, "condition": "open", "layout": "geometry_" + str(i),
                      "seed": 1001 + i, "stochastic": True, "scenario": case.to_dict(),
                      "window": window(40, nominal=phase != "stress"),
                      "load": {"lambda_tasks_per_second": .04, "reference_capacity_tasks_per_second": .1,
                               "method": "predeclared synthetic analytical reference"}})
    return {"schema": "clover-study-spec-v1", "stage": "pilot",
            "provenance": {"kind": "synthetic_research", "description": "synthetic test fixture",
                           "reviewed_by": "unit test"},
            "decision_settings": DecisionSettings(timing_protocol=DEADLINE_PROTOCOL,
                primary_deadline_policy="reject_late_primary", admission_reserve_seconds=.1,
                record_planning_inputs=True, end_to_end_soft_deadline_seconds=2,
                publication_allowance_seconds=.2).to_dict(),
            "planning_settings": asdict(Settings(horizon=10, interval=2, step=2, sigma=0)),
            "selection": {"method": "a_priori", "rationale": "test profile", "heldout_used": False},
            "analysis": {"control": "optimized", "candidates": ["baseline"],
                         "metrics": ["throughput_per_min", "unfinished", "dispatch_timely_acceptance_rate"],
                         "primary_metrics": ["throughput_per_min"], "practical_difference": {"throughput_per_min": .1},
                         "guardrails": {"nominal_faults": 0}, "selection_rule": "no winner for a pilot",
                         "confidence": .95, "bootstrap_resamples": 1000, "bootstrap_seed": 1,
                         "minimum_seed_pairs": 10, "minimum_layouts": 3,
                         "layout_sampling_independent": False, "hardware_id": "test host"}, "cases": cases}


def dump(path: Path, body: dict) -> None:
    """Write test inputs with real newlines."""
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("bad", ("geometry", "renamed_geometry", "seed", "duplicate", "load", "cutoff",
                                 "steady", "source_kind"))
def test_study_rejects_leakage_and_ambiguous_windows(bad: str) -> None:
    body = spec()
    if bad in {"geometry", "renamed_geometry"}:
        body["cases"][2]["scenario"]["factory"] = copy.deepcopy(body["cases"][0]["scenario"]["factory"])
        if bad == "renamed_geometry":
            factory = body["cases"][2]["scenario"]["factory"]
            factory["profile"].update(factory_name="renamed", floor_width=500)
            for node in factory["nodes"]:
                node["id"] += 100
            for edge in factory["edges"]:
                edge["id"] += 100
                edge["from_node"] += 100
                edge["to_node"] += 100
    elif bad == "seed":
        body["cases"][2]["seed"] = 1001
    elif bad == "duplicate":
        duplicate = copy.deepcopy(body["cases"][0])
        duplicate.update(id="relabel", seed=body["cases"][0]["seed"])
        body["cases"].append(duplicate)
    elif bad == "load":
        body["cases"][0]["load"]["lambda_tasks_per_second"] = 1
    elif bad == "cutoff":
        body["cases"][0]["window"].update(arrival_cutoff=30, drain_seconds=10)
    elif bad == "steady":
        body["cases"][0]["window"]["mode"] = "steady_stream"
    else:
        body["provenance"]["kind"] = "pretend_real"
    with pytest.raises(ValueError):
        validate_spec(body)


def test_freeze_immutable_and_confirmatory_needs_evidence(tmp_path: Path) -> None:
    body = spec()
    path = tmp_path / "spec.json"
    dump(path, body)
    frozen = freeze_study(path, tmp_path / "frozen.json")
    assert frozen["scope"] == "synthetic research"
    with pytest.raises(ValueError, match="already exists"):
        freeze_study(path, tmp_path / "frozen.json")
    frozen["cases"][0]["seed"] += 1
    dump(tmp_path / "changed.json", frozen)
    with pytest.raises(ValueError, match="hash mismatch"):
        load_frozen(tmp_path / "changed.json")
    body["stage"] = "confirmatory"
    dump(path, body)
    with pytest.raises(ValueError, match="committed|calibration"):
        freeze_study(path, tmp_path / "confirmatory.json")


def test_study_saves_unstarted_and_error_arms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from simulation.evaluation import study

    body = spec()
    extra = copy.deepcopy(body["cases"][0])
    extra.update(id="second_training", seed=1101)
    body["cases"].append(extra)
    dump(tmp_path / "spec.json", body)
    freeze_study(tmp_path / "spec.json", tmp_path / "frozen.json")

    def explode(*args: object, **kwargs: object) -> None:
        checkpoint = json.loads((tmp_path / "runs/manifest.json").read_text())
        assert checkpoint["planned_runs"] == 4
        assert checkpoint["entries"][0]["status"] == "RUNNING"
        assert checkpoint["entries"][1]["status"] == "NOT_STARTED"
        raise RuntimeError("injected evaluator error")

    monkeypatch.setattr(study, "run_suite", explode)
    result = run_study(tmp_path / "frozen.json", tmp_path / "runs", phase="training")
    assert result["status"] == "PARTIAL"
    assert result["entries"][1]["status"] == "NOT_RUN_EVALUATION_ERROR"
    plan = study_plan(tmp_path / "runs/manifest.json")
    dump(tmp_path / "plan.json", plan)
    report = compare_plan(tmp_path / "plan.json")
    assert report["planned_arms"] == 4 and report["full_observation_arms"] == 0
    assert report["registered_phase_verified"]
    plan["metrics"] = ["unfinished"]
    with pytest.raises(ValueError, match="registration"):
        validate_plan(plan)


def test_calibration_actual_sessions_and_common_window(tmp_path: Path) -> None:
    case = examples()[4]
    case.duration = 1200
    case.workload["rate"] = .01
    result = run_case(case, "optimized", 1001, output=tmp_path)
    assert result["status"] == "SUCCESS", result["failure"]
    contract = window(1200, sessions=1, nominal=False)
    report = calibrate(tmp_path / f"{result['case_id']}.json", contract)
    assert report["charge_starts"] >= report["charge_ends"] > 0
    assert sum(row["completed"] for row in report["robots"].values()) > 0
    assert len(report["segments"]) == 6
    contract["minimum_completed_charge_sessions_per_robot"] = 100
    assert calibrate(tmp_path / f"{result['case_id']}.json", contract)["status"] == "FAIL"
    contract["drain_seconds"] = 5
    with pytest.raises(ValueError, match="cutoff"):
        validate_window(case, contract)
    contract["drain_seconds"] = 0
    path = tmp_path / f"{result['case_id']}.json"
    tampered = json.loads(path.read_text())
    tampered["scenario"]["factory"]["stations"][0]["x"] += 1
    dump(path, tampered)
    with pytest.raises(ValueError, match="hash mismatch"):
        calibrate(path, contract)


def test_registered_cli_execution_and_analysis(tmp_path: Path) -> None:
    from simulation.evaluation.__main__ import main

    dump(tmp_path / "spec.json", spec())
    assert main(["freeze-study", "--spec", str(tmp_path / "spec.json"),
                 "--output", str(tmp_path / "frozen.json")]) == 0
    assert main(["run-study", "--frozen", str(tmp_path / "frozen.json"), "--phase", "heldout",
                 "--output", str(tmp_path / "runs"), "--run-wall-limit", "20"]) == 0
    assert main(["study-analysis-plan", "--manifest", str(tmp_path / "runs/manifest.json"),
                 "--output", str(tmp_path / "plan.json")]) == 0
    report = compare_plan(tmp_path / "plan.json")
    assert report["registered_phase_verified"] and report["purpose"] == "e0"
    assert report["full_observation_arms"] == report["planned_arms"] == 2
    assert report["recommendation"] is None and not report["heldout_validated"]
    assert all(g["across_layouts"]["estimate"]["p_value"] is None for g in report["comparisons"])
