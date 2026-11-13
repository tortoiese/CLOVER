"""Timing manifests must not hide unknown stages or mix distinct budgets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from simulation.evaluation.__main__ import main
from simulation.evaluation.profiling import profile_manifest
from simulation.evaluation.scenario import fingerprint


def decision(report: dict[str, Any], *, pending: bool = False) -> dict[str, Any]:
    """A small recorded decision independent of a native solver installation."""
    return {
        "result": {"report": report},
        "accepted": not pending,
        "reason": "PENDING_AT_END" if pending else "COMMITTED",
        "latency_seconds": 0.0,
        **({} if pending else {"apply_wall_seconds": 0.01}),
    }


def suite(tmp: Path, inputs: list[tuple[dict[str, Any] | None, list[dict[str, Any]]]]) -> Path:
    """Write linked result/manifest identities with decision hashes."""
    rows = []
    for i, (settings, records) in enumerate(inputs):
        case = f"case{i}"
        policy = "rh_cpsat_native" if settings is not None else "optimized"
        run = {
            "schema": "clover-headless-run-v4",
            "case_id": case,
            "policy": policy,
            "seed": i,
            "status": "SUCCESS",
            "source": {"source_hash": "source"},
            "source_hash_at_end": "source",
            "scenario_hash": "scenario",
            "planning": {
                "settings": settings,
                "decisions": records,
                "decisions_hash": fingerprint(records),
                "fallbacks": {},
            }
            if settings is not None
            else None,
        }
        path = tmp / (case + ".json")
        path.write_text(json.dumps(run))
        rows.append({"case_id": case, "policy": policy, "seed": i, "status": "SUCCESS", "result": path.name})
    manifest = {
        "schema": "clover-headless-suite-v1",
        "status": "SUCCESS",
        "source": {"source_hash": "source"},
        "planned_runs": len(rows),
        "runs": rows,
    }
    path = tmp / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def test_partial_measurements_are_counted_without_zero_imputation(tmp_path: Path) -> None:
    reports = [
        {
            "status": "NO_INCUMBENT_TIMEOUT",
            "parent_elapsed_seconds": 1.5,
            "worker_reused": False,
            "timing_stage": "native_robot_model",
            "timing_complete": False,
            "stage_seconds": {"native_import": 0.4, "native_robot_model": 0.2},
        },
        {
            "status": "OPTIMAL",
            "parent_elapsed_seconds": 0.4,
            "worker_reused": True,
            "timing_stage": "native_solution_validate",
            "timing_complete": True,
            "stage_seconds": {"native_primary_search": 0.2},
        },
    ]
    path = suite(tmp_path, [({"timeout": 1, "timing_mode": "D0"}, [decision(r) for r in reports])])
    group = profile_manifest(path)["groups"][0]
    assert group["decisions"] == 2 and group["soft_budget_observed"] == 2 and group["soft_budget_exceeded"] == 1
    assert group["cold_worker_decisions"] == 1 and group["timing_finalized_decisions"] == 1
    assert group["parent_seconds"]["mean"] == pytest.approx(0.95)
    assert group["stages"]["native_primary_search"]["count"] == 1
    assert group["stages"]["native_primary_search"]["unrecorded"] == 1
    assert group["stages"]["native_primary_search"]["p99"] == 0.2


def test_settings_budgets_and_delay_modes_are_separate_groups(tmp_path: Path) -> None:
    configs = [
        {"timeout": budget, "timing_mode": mode, "native_actions": cap}
        for budget, mode, cap in ((0.5, "D0", 4), (10, "D0", 4), (10, "D1", 4), (10, "D0", 8))
    ]
    path = suite(tmp_path, [(cfg, [decision({"status": "FEASIBLE"})]) for cfg in configs])
    result = profile_manifest(path)
    assert len(result["groups"]) == 4
    assert all(g["parent_seconds"]["mean"] is None and not g["stages"] for g in result["groups"])


def test_distinct_scenarios_are_not_pooled_into_one_timing_group(tmp_path: Path) -> None:
    path = suite(tmp_path, [({"timeout": 1}, [decision({"status": "OPTIMAL"})])] * 2)
    p = tmp_path / "case1.json"
    run = json.loads(p.read_text())
    run["scenario_hash"] = "another"
    p.write_text(json.dumps(run))
    result = profile_manifest(path)
    assert len(result["groups"]) == 2 and {g["scenario_hash"] for g in result["groups"]} == {"scenario", "another"}


def test_unstarted_budget_pending_decision_and_uninstrumented_policy_are_explicit(tmp_path: Path) -> None:
    path = suite(tmp_path, [(None, []), ({"timeout": 1}, [decision({"status": "FEASIBLE"}, pending=True)])])
    m = json.loads(path.read_text())
    m["planned_runs"] = 4
    m["status"] = "RUNNING"
    m["runs"].append({"case_id": "not-started", "status": "NOT_RUN_BUDGET"})
    path.write_text(json.dumps(m))
    result = profile_manifest(path)
    assert result["loaded_runs"] == 2 and result["not_started_budget"] == ["not-started"]
    assert result["not_yet_recorded"] == 1
    old = next(g for g in result["groups"] if g["policy"] == "optimized")
    pending = next(g for g in result["groups"] if g["policy"] == "rh_cpsat_native")
    assert not old["timing_available"] and old["parent_seconds"]["mean"] is None
    assert pending["pending_at_end"] == 1 and pending["application_wall_seconds"]["unrecorded"] == 1


def test_timing_cli_writes_reviewable_summary(tmp_path: Path) -> None:
    path = suite(
        tmp_path, [({"timeout": 1}, [decision({"status": "OPTIMAL", "stage_seconds": {"native_import": 0.2}})])]
    )
    out = tmp_path / "profile.json"
    assert main(["profile", "--manifest", str(path), "--output", str(out)]) == 0
    saved = json.loads(out.read_text())
    assert saved["schema"] == "clover-timing-analysis-v1" and saved["loaded_runs"] == 1


@pytest.mark.parametrize("field", ("decision", "source", "path", "duplicate"))
def test_timing_summary_rejects_tampered_or_escaped_inputs(tmp_path: Path, field: str) -> None:
    path = suite(tmp_path, [({"timeout": 1}, [decision({"status": "OPTIMAL"})])])
    m = json.loads(path.read_text())
    rpath = tmp_path / "case0.json"
    run = json.loads(rpath.read_text())
    if field == "decision":
        run["planning"]["decisions"][0]["result"]["report"]["status"] = "changed"
    if field == "source":
        run["source"]["source_hash"] = "other"
    if field == "path":
        m["runs"][0]["result"] = "../outside.json"
    if field == "duplicate":
        m["runs"].append(m["runs"][0])
    rpath.write_text(json.dumps(run))
    path.write_text(json.dumps(m))
    with pytest.raises(ValueError):
        profile_manifest(path)
