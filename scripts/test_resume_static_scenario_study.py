"""Continuation must preserve completed evidence and report actual process liveness."""

from __future__ import annotations

import copy
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import psutil
from resume_static_scenario_study import (
    archive_copy,
    checkpoint,
    health,
    missing_policies,
    phase_valid,
    policy_order,
    result_row,
    supervise,
)
from simulation.artifacts import read_saved
from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import run_case, run_suite, source_manifest
from simulation.evaluation.scenario import fingerprint


class ResumeStudyTests(unittest.TestCase):
    """Use actual short baseline runs and lossless archives without starting native solvers."""

    def test_original_policy_order_and_only_missing_policies(self) -> None:
        case = examples()[0]
        job = {
            "seed": 123,
            "arms": [
                {"policy": "baseline", "status": "NOT_STARTED"},
                {"policy": "optimized", "status": "SUCCESS"},
                {"policy": "rh_milp_grid", "status": "SUCCESS"},
                {"policy": "rh_cpsat_linear", "status": "NOT_STARTED"},
            ],
        }
        expected = [p for p in policy_order(case, 123) if p in {"baseline", "rh_cpsat_linear"}]
        self.assertEqual(missing_policies(job, case), expected)
        for bad_status in ("FAILED", "TIMEOUT", "RESULT_UNFINALIZED", "CANCELLED"):
            bad = copy.deepcopy(job)
            bad["arms"][0]["status"] = bad_status
            with self.subTest(bad_status=bad_status), self.assertRaises(ValueError):
                missing_policies(bad, case)

    def test_archive_recovery_preserves_raw_pending_and_logical_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            case = examples()[0]
            case.duration = 6
            run = run_case(case, "baseline", 123, output=root, static_layout=True)
            source = root / (run["case_id"] + ".json")
            original = source.read_bytes()
            event = root / (run["case_id"] + ".events.jsonl")
            event_bytes = event.read_bytes()
            pending = source.with_name(source.name + ".xz.pending")
            pending.write_bytes(b"incomplete compression evidence")
            archived, metadata = archive_copy(source, root / "recovery", "xz")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(event.read_bytes(), event_bytes)
            self.assertEqual(pending.read_bytes(), b"incomplete compression evidence")
            self.assertEqual(read_saved(archived), original)
            row = result_row(run, archived, metadata, root, "session1")
            self.assertTrue(row["result"].startswith("recovery/"))
            self.assertTrue(all(k.startswith("recovery/") for k in row["archive_files"]))
            with self.assertRaises(FileExistsError):
                archive_copy(source, root / "recovery", "xz")

    def test_policy_order_is_the_original_suite_order(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            case = examples()[0]
            case.duration = 1
            # Unstarted rows expose the original permutation without computing any policy.
            suite = run_suite(
                (case,),
                (123,),
                Path(parent) / "suite",
                policies=("baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear"),
                suite_wall_limit=0,
            )
            self.assertEqual([r["policy"] for r in suite["runs"]], policy_order(case, 123))
            self.assertTrue(all(r["status"] == "NOT_RUN_BUDGET" for r in suite["runs"]))

    def test_previously_compressed_cas_is_retained_and_reindexed(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent)
            (root / "planning_inputs").mkdir()
            cas = root / "planning_inputs/snapshot.json.gz"
            cas.write_bytes(gzip.compress(b'{"snapshot": "identical"}', mtime=0))
            before = cas.read_bytes()
            source = root / "run.json"
            source.write_text(
                json.dumps({"control": {"planning_inputs": {"ref": {"path": "planning_inputs/snapshot.json"}}}})
            )
            (root / "run.events.jsonl").write_text("{}\n")
            _, archive = archive_copy(source, root / "recovered", "xz")
            self.assertIn("planning_inputs/snapshot.json", archive["archive_files"])
            self.assertEqual(cas.read_bytes(), before)
            self.assertEqual(read_saved(root / "recovered/planning_inputs/snapshot.json"), b'{"snapshot": "identical"}')

    def test_checkpoint_refuses_unverified_events(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            job_root = root / "job"
            case = examples()[0]
            case.duration = 6
            run = run_case(case, "baseline", 123, output=job_root, static_layout=True)
            original = job_root / (run["case_id"] + ".json")
            (job_root / (run["case_id"] + ".events.jsonl")).write_text("{}\n")
            archived, archive = archive_copy(original, job_root / "recovery", "xz")
            row = result_row(run, archived, archive, job_root, "session")
            suite = {
                "schema": "clover-headless-suite-v1",
                "runs": [row],
                "seeds": [123],
                "policies": ["baseline"],
                "planned_runs": 1,
                "source": source_manifest(),
                "scenario_hashes": {case.id: fingerprint(case.to_dict())},
            }
            job = {
                "directory": "job",
                "seed": 123,
                "scenario_hash": run["scenario_hash"],
                "mode": "D0",
                "arms": [{"policy": "baseline", "status": "NOT_STARTED"}],
            }
            with self.assertRaises((ValueError, KeyError)):
                checkpoint(root, {"jobs": [job]}, job, suite)
            self.assertFalse((job_root / "manifest.json").exists())
            self.assertEqual(job["arms"][0]["status"], "NOT_STARTED")

    def test_child_failure_writes_stopped_state_and_exit_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as parent:
            root = Path(parent).resolve()
            bundle, output = root / "bundle", root / "output"
            bundle.mkdir()
            output.mkdir()
            (bundle / "design.json").write_text(json.dumps({"run_limits": {"run_wall_seconds": 900}}))
            campaign = {"identity": {}, "jobs": []}
            (output / "campaign.json").write_text(json.dumps(campaign))
            (output / "driver.json").write_text(json.dumps({"pid": 999, "process_created_at": 0, "status": "STOPPED"}))
            process = Mock()
            process.create_time.return_value = 12
            child = Mock(pid=321)
            child.wait.return_value = 42
            with (
                patch("resume_static_scenario_study.open_campaign", return_value=campaign),
                patch("resume_static_scenario_study.frozen_code", return_value={"commit": "fixture"}),
                patch("resume_static_scenario_study.psutil.Process", return_value=process),
                patch("resume_static_scenario_study.subprocess.Popen", return_value=child),
                patch("resume_static_scenario_study.os.killpg"),
                self.assertRaisesRegex(RuntimeError, "exited 42"),
            ):
                supervise(bundle, output, output / "resume_sessions/test")
            driver = json.loads((output / "driver.json").read_text())
            self.assertEqual(driver["status"], "STOPPED_REQUIRES_REVIEW")
            self.assertEqual(driver["last_child_exit_code"], 42)
            self.assertTrue((output / "resume_sessions/test/exit.json").exists())

    def test_missing_or_reused_pid_is_never_running(self) -> None:
        driver = {"pid": 123, "process_created_at": 10, "status": "RUNNING"}
        with patch("resume_static_scenario_study.psutil.Process", side_effect=psutil.NoSuchProcess(123)):
            self.assertEqual(health(driver)["derived_status"], "STOPPED_PROCESS_MISSING")
        process = Mock()
        process.create_time.return_value = 11
        with patch("resume_static_scenario_study.psutil.Process", return_value=process):
            self.assertEqual(health(driver)["derived_status"], "STOPPED_PROCESS_MISSING")

    def test_access_denied_and_legacy_identity_remain_unknown(self) -> None:
        with patch("resume_static_scenario_study.psutil.Process", side_effect=psutil.AccessDenied(123)):
            self.assertEqual(health({"pid": 123, "status": "RUNNING"})["derived_status"], "UNKNOWN_PROCESS_ACCESS")
        with patch("resume_static_scenario_study.psutil.Process", return_value=Mock()):
            self.assertEqual(health({"pid": 123, "status": "RUNNING"})["derived_status"], "UNKNOWN_PROCESS_IDENTITY")

    def test_orphaned_live_child_is_not_reported_as_stopped(self) -> None:
        child = Mock()
        child.create_time.return_value = 20
        child.is_running.return_value = True
        with patch("resume_static_scenario_study.psutil.Process", side_effect=[psutil.NoSuchProcess(123), child]):
            result = health(
                {
                    "pid": 123,
                    "process_created_at": 10,
                    "status": "RUNNING",
                    "child_pid": 456,
                    "child_process_created_at": 20,
                }
            )
        self.assertEqual(result["derived_status"], "ORPHANED_CHILD_ACTIVE")
        self.assertFalse(result["pid_alive"])
        self.assertTrue(result["child_pid_alive"])

    def test_live_process_without_fresh_heartbeat_is_flagged(self) -> None:
        process = Mock()
        process.create_time.return_value = 10
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        with patch("resume_static_scenario_study.psutil.Process", return_value=process):
            result = health(
                {
                    "pid": 123,
                    "process_created_at": 10,
                    "status": "RUNNING",
                    "heartbeat_at_utc": "2020-01-01T00:00:00+00:00",
                }
            )
        self.assertTrue(result["pid_alive"])
        self.assertEqual(result["derived_status"], "RUNNING_HEARTBEAT_STALE")

    def test_native_non_use_and_partial_phase_block_progression(self) -> None:
        campaign = {
            "jobs": [
                {
                    "phase": "M2a",
                    "status": "SUCCESS",
                    "arms": [
                        {
                            "policy": "rh_milp_grid",
                            "status": "SUCCESS",
                            "metrics": {"rh_execution": {"reports_with_native_search": 1, "primary_prefix_commits": 1}},
                        }
                    ],
                }
            ]
        }
        self.assertTrue(phase_valid(campaign, "M2a"))
        campaign["jobs"][0]["arms"][0]["metrics"]["rh_execution"]["primary_prefix_commits"] = 0
        self.assertFalse(phase_valid(campaign, "M2a"))
        campaign["jobs"][0]["status"] = "INTERRUPTED"
        self.assertFalse(phase_valid(campaign, "M2a"))


if __name__ == "__main__":
    unittest.main()
