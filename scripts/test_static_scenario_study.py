"""Static experiment controls, semantic audits, gates and censoring regressions."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from audit_static_scenario_study import audit_bundle
from materialize_static_scenario_study import (
    DESIGN,
    case_for,
    file_sha,
    fingerprint,
    materialize,
    planned_inputs,
    validate_design,
    validate_routes,
)
from run_static_scenario_study import check_gate, run_phase, sync_job
from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import run_case
from static_scenario_metrics import summarize


class StaticStudyTests(unittest.TestCase):
    """Use small deterministic fixtures; no native solver or timing experiment is launched."""

    def setUp(self) -> None:
        self.design = json.loads(DESIGN.read_text(encoding="utf-8"))

    def test_full_matrix_seed_separation_and_routes(self) -> None:
        validate_design(self.design)
        rows = planned_inputs(self.design)
        self.assertEqual(len(rows), 155)
        self.assertEqual(Counter(r["phase"] for r in rows), {"M1_D1": 3, "M1_D0": 2, "M2a": 45, "M2b": 45, "M3": 60})
        structures = {}
        for row in rows:
            case = case_for(self.design, row)
            self.assertEqual(validate_routes(case), 96)
            self.assertFalse(case.interventions)
            self.assertEqual(len(case.factory.robots), 8)
            self.assertEqual(len(case.factory.stations), 6)
            self.assertEqual(case.factory.floor_count, 1)
            if row["condition"] == "ADV_L06_C4_A1":
                from simulation.evaluation.study import _geometry

                structures[row["layout"]] = _geometry(case)
        self.assertEqual(len(set(structures.values())), 4)

    def test_slot_and_burst_controls(self) -> None:
        base = {"layout": "DEV_GRID_A", "seed": 2026111901, "condition": "ADV_L13_C4_A1"}
        smooth = case_for(self.design, base)
        fewer = case_for(self.design, {**base, "condition": "ADV_L13_C1_A1"})
        burst = case_for(self.design, {**base, "condition": "ADV_L13_C4_A4"})
        self.assertEqual(smooth.workload, fewer.workload)
        self.assertEqual(smooth.initial_robots, fewer.initial_robots)
        self.assertEqual(smooth.initial_robots, burst.initial_robots)
        changes = 0
        for a, b in zip(smooth.workload["jobs"], burst.workload["jobs"], strict=True):
            self.assertEqual(
                {k: v for k, v in a.items() if k not in {"at", "deadline"}},
                {k: v for k, v in b.items() if k not in {"at", "deadline"}},
            )
            self.assertAlmostEqual(a["deadline"] - a["at"], b["deadline"] - b["at"])
            if a["at"] < smooth.warmup:
                self.assertEqual(a["at"], b["at"])
            changes += a["at"] != b["at"]
        self.assertGreater(changes, 0)

    def test_reject_leaked_seed_dynamic_layout_and_bool(self) -> None:
        for key, value in (
            ("interventions", [{"kind": "edge_width"}]),
            ("duration", True),
            ("development_seeds", [2026111900, 1, 2, 3, 4]),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_design({**self.design, key: value})

    def test_materialize_audit_and_rehashed_semantic_tamper(self) -> None:
        # One whole matrix is needed to test missing/duplicate denominators.
        with tempfile.TemporaryDirectory() as parent:
            bundle = Path(parent) / "bundle"
            result = materialize(bundle)
            audit = audit_bundle(bundle)
            self.assertEqual(audit["planned_runs"], 620)
            self.assertEqual(audit["phase_runs"]["M3"], 240)
            with self.assertRaises(FileExistsError):
                materialize(bundle)
            row = result["entries"][0]
            path = bundle / row["scenario"]
            scenario = json.loads(path.read_text())
            scenario["initial_robots"]["R0"]["soc_pct"] += 1
            path.write_text(json.dumps(scenario))
            row.update(
                sha256=file_sha(path),
                scenario_hash=fingerprint(scenario),
                initial_hash=fingerprint(scenario["initial_robots"]),
            )
            (bundle / "manifest.json").write_text(json.dumps(result))
            with self.assertRaisesRegex(ValueError, "deterministic static recipe"):
                audit_bundle(bundle)

    def test_gate_and_interrupted_job_never_become_retry(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent)
            (output / "job_0").mkdir()
            (output / "job_0/manifest.json").write_text(
                json.dumps(
                    {"status": "RUNNING", "runs": [{"policy": "optimized", "status": "FAILED", "failure": "retained"}]}
                )
            )
            job = {
                "directory": "job_0",
                "status": "RUNNING",
                "arms": [
                    {"policy": "optimized", "status": "NOT_STARTED"},
                    {"policy": "baseline", "status": "NOT_STARTED"},
                ],
            }
            sync_job(job, output)
            self.assertEqual(job["status"], "INTERRUPTED")
            self.assertEqual(job["arms"][0]["status"], "FAILED")
            self.assertEqual(job["arms"][1]["status"], "NOT_STARTED")
            with self.assertRaises(FileNotFoundError):
                check_gate({"identity": {}, "jobs": []}, output, "M2a")
            receipt = {
                "status": "PASS",
                "identity": {},
                "verified_runs": [{"arm": str(i), "result_sha256": "test"} for i in range(20)],
                "replays": dict.fromkeys(("baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear"), True),
            }
            receipt["receipt_hash"] = fingerprint(receipt)
            (output / "readiness.json").write_text(json.dumps(receipt))
            with self.assertRaisesRegex(ValueError, "development"):
                check_gate({"identity": {}, "jobs": [{"phase": "M2a", "status": "FAILED"}]}, output, "M3")

    def test_executor_seals_full_denominator_and_preserves_partial_block(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            bundle, output = root / "bundle", root / "runs"
            materialize(bundle)
            calls = []

            def partial_suite(scenarios, seeds, directory, **kwargs):
                calls.append((scenarios, seeds, kwargs))
                directory.mkdir()
                (directory / "manifest.json").write_text(
                    json.dumps(
                        {
                            "status": "PARTIAL",
                            "runs": [{"policy": "baseline", "status": "FAILED", "failure": "preserved diagnostic"}],
                        }
                    )
                )

            with (
                patch("run_static_scenario_study.frozen_code", return_value={"commit": "fixture"}),
                patch("run_static_scenario_study.run_suite", side_effect=partial_suite),
            ):
                result = run_phase(bundle, output, "M1_D1", limit=1)
            self.assertEqual(result["planned_runs"], 620)
            self.assertEqual(result["counts"], {"NOT_STARTED": 619, "FAILED": 1})
            self.assertEqual(result["status"], "STOPPED_REQUIRES_REVIEW")
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0][0]), 1)
            self.assertEqual(calls[0][1], (self.design["canary_seed"],))
            self.assertTrue(calls[0][2]["static_layout"])
            self.assertTrue(calls[0][2]["compress_artifacts"])

    def test_backlog_censoring_and_compute_return_commit_are_distinct(self) -> None:
        case = examples()[0]
        case.duration, case.warmup = 60, 0
        case.workload = {
            "kind": "trace",
            "jobs": [
                {
                    "uid": "urgent",
                    "at": 59,
                    "station": "S0",
                    "allowed": ["S0"],
                    "operation": "inspection",
                    "priority": 0,
                    "noise": 0,
                    "deadline": 59.5,
                }
            ],
        }
        run = run_case(case, "baseline", 9, static_layout=True)
        # A returned feasible plan need not have passed admission or committed.
        decision = {
            "request_kind": "dispatch",
            "triggered_at": 10,
            "time": 11,
            "primary_kind": "rh_plan",
            "accepted": False,
            "coverage_ok": True,
            "reason": "STALE",
            "fallback": {},
            "phases": [],
            "compute": {"status": "OK", "value": {"plan": {"report": {"status": "FEASIBLE"}}}},
        }
        run["control"] = {"settings": {"end_to_end_soft_deadline_seconds": 2}, "decisions": [decision]}
        result = summarize(run)
        self.assertEqual(result["cohorts"]["urgent"]["due"], 1)
        self.assertEqual(result["cohorts"]["urgent"]["late"], 1)
        self.assertEqual(result["backlog_60s"][-1]["backlog"], 1)
        rh = result["rh_execution"]
        self.assertEqual(
            (rh["worker_compute_attempts"], rh["returned_plans"], rh["feasible_reports"], rh["primary_prefix_commits"]),
            (1, 1, 1, 0),
        )
        self.assertEqual(rh["fallback_reasons"], {"STALE": 1})


if __name__ == "__main__":
    unittest.main()
