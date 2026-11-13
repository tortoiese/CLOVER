"""Persisted v5 control timing, settings separation and tamper rejection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.fixtures import joint_batch
from simulation.evaluation.profiling import profile_manifest
from simulation.evaluation.runner import run_suite


def test_control_profiles_separate_policy_and_keep_unknown_parent_cost(tmp_path: Path) -> None:
    root = tmp_path / "suite"
    case = joint_batch()
    case.duration = 25
    suite = run_suite(
        (case,), (1001,), root, policies=("baseline", "optimized"), decision_settings=DecisionSettings().to_dict()
    )
    assert suite["status"] == "SUCCESS"
    profile = profile_manifest(root / "manifest.json")
    assert profile["loaded_runs"] == 2 and len(profile["groups"]) == 2
    for group in profile["groups"]:
        assert group["timing_mode"] == "d1-staged-budget-v1"
        assert group["parent_seconds"]["count"] == 0 and group["parent_seconds"]["unrecorded"] == group["decisions"]
        assert group["control_measured_seconds"]["count"] == group["decisions"]
        assert group["stages"]["startup"]["count"] == 1 and group["cold_worker_decisions"] == 1
        assert group["publication_prepaid_seconds"]["mean"] >= 0.1
    path = root / suite["runs"][0]["result"]
    saved = json.loads(path.read_text())
    saved["control"]["decisions"][0]["reason"] = "changed"
    path.write_text(json.dumps(saved))
    with pytest.raises(ValueError, match="control/lifecycle"):
        profile_manifest(root / "manifest.json")


@pytest.mark.parametrize(
    "changes",
    (
        {"publication_allowance_seconds": 0},
        {"startup_timeout_seconds": True},
        {"max_worker_restarts": -1},
        {"max_admission_attempts": 0},
        {"request_watchdog_seconds": float("nan")},
        {"timing_protocol": "D1"},
        {"restart_backoff_seconds": (1,)},
        {"startup_mode": "hidden-warmup"},
    ),
)
def test_invalid_control_settings_are_rejected_before_simulation(changes: dict) -> None:
    with pytest.raises(ValueError):
        DecisionSettings(**changes).validate()
