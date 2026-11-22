"""A reviewed failure never becomes success and cannot waive a future failure."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from materialize_static_scenario_study import file_sha, fingerprint
from resume_static_scenario_study import phase_valid, supervise
from review_static_timing_failure import STATUS, check_timing_failure, create_review, load_review, run_reviewed_validation
from simulation.evaluation.fixtures import examples


def failed_run() -> dict:
    """Represent a fully observed coverage failure with no physical fault."""
    return {
        "status": STATUS, "failure": "publication cost exceeded prepaid allowance",
        "source": {"source_hash": "same"}, "source_hash_at_end": "same", "environment": {},
        "scenario": {"duration": 10}, "observed_seconds": 10,
        "final_audit": {"ok": True}, "static_layout_audit": {"ok": True},
        "faults": 0, "energy": {"rescue": 0}, "max_energy_error": 0,
        "offered": 1, "scheduled_arrivals": 1, "trace": [{}], "state_counts": {"completed": 1},
        "control": {
            "coverage_ok": False, "disposed": {"worker": True},
            "settings": {"publication_allowance_seconds": .2},
            "decisions": [{"token": "d1", "coverage_ok": False, "phases": [
                {"name": "compute", "wall_seconds": .1, "injected_seconds": .1},
                {"name": "publication", "wall_seconds": .202, "injected_seconds": .2},
            ]}],
        },
    }


def campaign() -> dict:
    """Keep one failed arm and one successfully applied RH arm in a closed block."""
    return {"identity": {}, "jobs": [{
        "index": 81, "phase": "M2b", "directory": "job_081", "status": "PARTIAL", "arms": [
            {"policy": "baseline", "status": "SUCCESS"},
            {"policy": "rh_milp_grid", "status": STATUS, "result_sha256": "original",
             "metrics": {"rh_execution": {"reports_with_native_search": 1, "primary_prefix_commits": 1}}},
        ],
    }]}


class TimingReviewTests(unittest.TestCase):
    """Prove explicit progression never alters labels, samples or timing settings."""

    def test_original_failure_stays_invalid_while_exact_review_permits_progress(self) -> None:
        current = campaign()
        original = copy.deepcopy(current)
        reviewed = {(81, "rh_milp_grid"): {"status": STATUS, "result_sha256": "original"}}
        self.assertFalse(phase_valid(current, "M2b"))
        self.assertTrue(phase_valid(current, "M2b", reviewed))
        self.assertEqual(current, original)

    def test_review_does_not_allow_replacement_bytes_another_policy_or_unstarted_arm(self) -> None:
        reviewed = {(81, "rh_milp_grid"): {"status": STATUS, "result_sha256": "original"}}
        for change in ("result", "new_failure", "unstarted", "missing_native", "wrong_job"):
            current = campaign()
            job, arm = current["jobs"][0], current["jobs"][0]["arms"][1]
            if change == "result":
                arm["result_sha256"] = "replacement"
            elif change == "new_failure":
                job["arms"][0]["status"] = STATUS
            elif change == "unstarted":
                job["arms"][0]["status"] = "NOT_STARTED"
            elif change == "missing_native":
                arm["metrics"]["rh_execution"]["reports_with_native_search"] = 0
            else:
                job["index"] = 82
            with self.subTest(change=change):
                self.assertFalse(phase_valid(current, "M2b", reviewed))

    def test_only_fully_observed_same_source_safe_failures_are_eligible(self) -> None:
        run = failed_run()
        identity = {"source_hash": "same", "environment": {}}
        design = {"d1_settings": {"publication_allowance_seconds": .2}}
        check_timing_failure(run, identity, design)
        changes = (
            ("status", "SUCCESS"), ("observed_seconds", 9), ("faults", 1),
            ("source_hash_at_end", "new"), ("max_energy_error", 1),
        )
        for key, value in changes:
            bad = copy.deepcopy(run)
            bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                check_timing_failure(bad, identity, design)
        for key, value in (("coverage_ok", True), ("disposed", {"worker": False})):
            bad = copy.deepcopy(run)
            bad["control"][key] = value
            with self.subTest(control=key), self.assertRaises(ValueError):
                check_timing_failure(bad, identity, design)

    def test_nonpublication_underaccounting_and_hidden_violation_are_rejected(self) -> None:
        for change in ("compute", "coverage", "allowance", "no_violation"):
            run = failed_run()
            row = run["control"]["decisions"][0]
            if change == "compute":
                row["phases"][0]["injected_seconds"] = 0
            elif change == "coverage":
                row["coverage_ok"] = True
            elif change == "allowance":
                row["phases"][1]["injected_seconds"] = .5
            else:
                row["phases"][1]["wall_seconds"] = .1
                row["coverage_ok"] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                check_timing_failure(run, {"source_hash": "same", "environment": {}},
                                     {"d1_settings": {"publication_allowance_seconds": .2}})

    def test_review_binds_original_result_events_and_replay_proof(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            job_root = root / "job_081"
            job_root.mkdir()
            result, events, proof = job_root / "run.json", job_root / "run.events", root / "proof.json"
            result.write_bytes(b"unchanged original failed result")
            events.write_bytes(b"unchanged events")
            proof.write_bytes(b"recorded replay proof")
            current = campaign()
            arm = current["jobs"][0]["arms"][1]
            arm.update(result="run.json", events="run.events", result_sha256=file_sha(result))
            row = {"job": 81, "policy": "rh_milp_grid", "status": STATUS,
                   "result": "run.json", "result_sha256": file_sha(result),
                   "events": "run.events", "events_sha256": file_sha(events)}
            receipt = {
                "schema": "clover-static-reviewed-timing-v1", "identity": {},
                "reviewed_failures": [row], "replay_proof": str(proof),
                "replay_proof_sha256": file_sha(proof), "settings_changed": False, "native_retries_allowed": 0,
            }
            receipt["receipt_hash"] = fingerprint(receipt)
            path = root / "review.json"
            path.write_text(json.dumps(receipt))
            self.assertIn((81, "rh_milp_grid"), load_review(path, current, root))
            for target in (result, events, proof, path):
                before = target.read_bytes()
                target.write_bytes(before + b" ")
                if target == path:  # JSON whitespace alone is not a content change.
                    changed = json.loads(before)
                    changed["native_retries_allowed"] = 1
                    target.write_text(json.dumps(changed))
                with self.subTest(target=target.name), self.assertRaises(ValueError):
                    load_review(path, current, root)
                target.write_bytes(before)
            changed = copy.deepcopy(current)
            changed["jobs"][0]["arms"][0]["status"] = STATUS
            with self.assertRaisesRegex(ValueError, "unreviewed failures"):
                load_review(path, changed, root)

    def test_review_creates_parent_directory_and_never_overwrites_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            bundle = root / "bundle"
            bundle.mkdir()
            design = {"d1_settings": {"publication_allowance_seconds": .2}}
            (bundle / "design.json").write_text(json.dumps(design))
            run = failed_run()
            run["scenario"]["id"], run["event_digest"] = "case", "physical-events"
            current = campaign()
            current["identity"] = {"source_hash": "same", "environment": {}}
            current["jobs"][0]["seed"] = 123
            current["jobs"][0]["arms"][1].update(result="run.json", events="run.events")
            proof = root / "proof.json"
            proof.write_text(json.dumps({
                "schema": "clover-static-timing-diagnostic-v1", "native_solver_calls": 0,
                "matches": True, "expected_digest": "physical-events", "actual_digest": "physical-events",
                "status": STATUS,
            }))
            destination = root / "new-reviews" / "review.json"
            before = copy.deepcopy(current)
            with (
                patch("review_static_timing_failure.execution_lock"),
                patch("review_static_timing_failure.open_campaign", return_value=current),
                patch("review_static_timing_failure.read_run", return_value=(run, "original")),
                patch("review_static_timing_failure.load_arm", return_value={
                    "full_observation": False, "status": STATUS, "result_sha256": "original",
                    "events_sha256": "events",
                }),
            ):
                create_review(bundle, root, destination, proof, 81, "rh_milp_grid")
                original = destination.read_bytes()
                with self.assertRaises(FileExistsError):
                    create_review(bundle, root, destination, proof, 81, "rh_milp_grid")
            self.assertEqual(current, before)
            self.assertEqual(destination.read_bytes(), original)

    def test_validation_never_starts_before_development_closes(self) -> None:
        current = campaign()
        current["jobs"][0]["arms"][0]["status"] = "NOT_STARTED"
        with (
            patch("review_static_timing_failure.execution_lock"),
            patch("review_static_timing_failure.open_campaign", return_value=current),
            patch("review_static_timing_failure.load_review", return_value={}),
            patch("review_static_timing_failure.check_gate") as gate,
            patch("review_static_timing_failure.run_suite") as run,
            self.assertRaisesRegex(ValueError, "all development arms must close"),
        ):
            run_reviewed_validation(Path("bundle"), Path("output"), Path("review"))
        gate.assert_called_once_with(current, Path("output"), "M2b")
        run.assert_not_called()

    def test_validation_runs_only_unstarted_inputs_with_original_limits(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            bundle, output, review = root / "bundle", root / "output", root / "review.json"
            bundle.mkdir()
            output.mkdir()
            case = examples()[0]
            (bundle / "input.json").write_text(json.dumps(case.to_dict()))
            design = {
                "run_limits": {"disk_reserve_bytes": 0, "run_wall_seconds": 900,
                               "job_wall_seconds": 3600, "archive_codec": "xz"},
                "rh_settings": {"timeout": 2}, "d1_settings": {"publication_allowance_seconds": .2},
            }
            (bundle / "design.json").write_text(json.dumps(design))
            current = campaign()
            before = copy.deepcopy(current["jobs"][0])
            policies = ("baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear")
            current["jobs"].append({
                "index": 95, "phase": "M3", "directory": "job_095", "status": "NOT_STARTED",
                "scenario": "input.json", "seed": 123,
                "arms": [{"policy": p, "status": "NOT_STARTED"} for p in policies],
            })
            reviewed = {(81, "rh_milp_grid"): {"status": STATUS, "result_sha256": "original"}}

            def complete(cases: tuple, seeds: tuple, destination: Path, **kwargs: object) -> None:
                self.assertEqual(cases[0].to_dict(), case.to_dict())
                self.assertEqual(seeds, (123,))
                self.assertEqual(tuple(kwargs["policies"]), policies)
                self.assertEqual(kwargs["run_wall_limit"], 900)
                self.assertEqual(kwargs["suite_wall_limit"], 3600)
                self.assertEqual(kwargs["planning_settings"], design["rh_settings"])
                self.assertEqual(kwargs["decision_settings"], design["d1_settings"])
                self.assertTrue(kwargs["static_layout"] and kwargs["compress_artifacts"])
                self.assertEqual(kwargs["archive_codec"], "xz")
                destination.mkdir()
                rows = [{"policy": p, "status": "SUCCESS", "metrics": {
                    "rh_execution": {"reports_with_native_search": 1, "primary_prefix_commits": 1}
                }} for p in policies]
                (destination / "manifest.json").write_text(json.dumps({"status": "SUCCESS", "runs": rows}))

            with (
                patch("review_static_timing_failure.execution_lock"),
                patch("review_static_timing_failure.open_campaign", return_value=current),
                patch("review_static_timing_failure.load_review", return_value=reviewed),
                patch("review_static_timing_failure.check_gate"),
                patch("review_static_timing_failure.audit_bundle"),
                patch("review_static_timing_failure.frozen_code"),
                patch("review_static_timing_failure.record_metrics"),
                patch("review_static_timing_failure.run_suite", side_effect=complete) as run,
            ):
                run_reviewed_validation(bundle, output, review)
            run.assert_called_once()
            self.assertEqual(current["jobs"][0], before)
            self.assertTrue(phase_valid(current, "M3"))

    def test_supervisor_routes_reviewed_validation_and_reports_failure_at_completion(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            bundle, output, review = root / "bundle", root / "output", root / "review.json"
            bundle.mkdir()
            output.mkdir()
            review.write_text("immutable review content")
            (bundle / "design.json").write_text(json.dumps({"run_limits": {"run_wall_seconds": 900}}))
            current = campaign()
            (output / "campaign.json").write_text(json.dumps(current))
            (output / "driver.json").write_text(json.dumps({"pid": 999, "process_created_at": 0,
                                                          "status": "STOPPED"}))
            reviewed = {(81, "rh_milp_grid"): {"status": STATUS, "result_sha256": "original"}}
            process, child = Mock(), Mock(pid=321)
            process.create_time.return_value = 12
            child.wait.return_value = 0
            with (
                patch("resume_static_scenario_study.open_campaign", return_value=current),
                patch("resume_static_scenario_study.frozen_code", return_value={"commit": "fixture"}),
                patch("review_static_timing_failure.load_review", return_value=reviewed),
                patch("resume_static_scenario_study.psutil.Process", return_value=process),
                patch("resume_static_scenario_study.subprocess.Popen", return_value=child) as launch,
            ):
                supervise(bundle, output, output / "resume_sessions/test", review=review)
            commands = [call.args[0] for call in launch.call_args_list]
            validation = [cmd for cmd in commands if "validation" in cmd]
            self.assertEqual(len(validation), 1)
            self.assertTrue(any(x.endswith("review_static_timing_failure.py") for x in validation[0]))
            driver = json.loads((output / "driver.json").read_text())
            self.assertEqual(driver["status"], "COMPLETE_WITH_REVIEWED_FAILURES")
            saved = json.loads((output / "campaign.json").read_text())
            self.assertEqual(saved["jobs"][0]["arms"][1]["status"], STATUS)


if __name__ == "__main__":
    unittest.main()
