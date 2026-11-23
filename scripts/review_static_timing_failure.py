"""Audit a closed timing failure and continue untouched inputs without retrying it."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audit_korean_case_pilot import execution_lock, inside
from audit_static_scenario_study import audit_bundle
from materialize_static_scenario_study import POLICIES, Scenario, file_sha, fingerprint
from run_static_scenario_study import check_gate, frozen_code, open_campaign, read_run, record_metrics, sync_job
from simulation.evaluation.comparison_inputs import load_arm
from simulation.evaluation.runner import run_suite
from simulation.evaluation.study import save

STATUS = "TIMING_COVERAGE_VIOLATION"
FAILURE = "publication cost exceeded prepaid allowance"


def check_timing_failure(run: dict[str, Any], identity: dict[str, Any], design: dict[str, Any]) -> None:
    """Require complete safe observations while retaining their invalid timing label.

    This check does not certify timing or establish the cause of a wall-clock
    spike. It only permits other, never-started inputs to be measured.
    """
    control = run.get("control") or {}
    case = run["scenario"]
    if (
        run["status"] != STATUS or run["failure"] != FAILURE
        or run["source"]["source_hash"] != identity["source_hash"]
        or run["source_hash_at_end"] != identity["source_hash"]
        or run["environment"] != identity["environment"]
        or run["observed_seconds"] != case["duration"]
        or not run["final_audit"]["ok"] or not run["static_layout_audit"]["ok"]
        or run["faults"] or run["energy"]["rescue"] or run["max_energy_error"] >= 1e-7
        or not run["offered"] == run["scheduled_arrivals"] == len(run["trace"])
        or run["offered"] != sum(run["state_counts"].values())
        or not control or control.get("coverage_ok") is not False
        or not control.get("disposed") or not all(control["disposed"].values())
        or control["settings"] != design["d1_settings"]
    ):
        raise ValueError("only a fully observed, safe, same-source timing failure may be reviewed")
    violations = []
    for row in control["decisions"]:
        overrun = False
        for phase in row["phases"]:
            if phase["name"] == "publication":
                if phase["injected_seconds"] != design["d1_settings"]["publication_allowance_seconds"]:
                    raise ValueError("publication allowance changed")
                overrun |= phase["wall_seconds"] > phase["injected_seconds"]
            elif phase["wall_seconds"] != phase["injected_seconds"]:
                raise ValueError("a non-publication phase has unaccounted time")
        if row["coverage_ok"] == overrun:
            raise ValueError("coverage flag does not match publication timing")
        if overrun:
            violations.append(row["token"])
    if not violations:
        raise ValueError("missing observed publication violation")


def create_review(bundle: Path, output: Path, destination: Path, proof: Path, job_index: int, policy: str) -> None:
    """Bind a raw audit and recorded replay to one exact failed result, without native retries."""
    with execution_lock():
        campaign = open_campaign(bundle, output)
        job = next(j for j in campaign["jobs"] if j["index"] == job_index)
        if job["status"] != "PARTIAL" or any(a["status"] == "NOT_STARTED" for a in job["arms"]):
            raise ValueError("the reviewed input block must already be closed")
        arm = next(a for a in job["arms"] if a["policy"] == policy)
        run, digest = read_run(output, job, arm)
        if digest != arm.get("result_sha256"):
            raise ValueError("failed result changed after checkpoint")
        design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
        check_timing_failure(run, campaign["identity"], design)
        checked = load_arm(output / job["directory"] / "manifest.json", run["scenario"]["id"], job["seed"], policy)
        if checked["full_observation"] or checked["status"] != STATUS or checked["result_sha256"] != digest:
            raise ValueError("raw audit must retain the timing-invalid result")
        replay_proof = json.loads(proof.read_text(encoding="utf-8"))
        if (
            replay_proof.get("schema") != "clover-static-timing-diagnostic-v1"
            or replay_proof.get("native_solver_calls") != 0 or replay_proof.get("matches") is not True
            or replay_proof.get("expected_digest") != run["event_digest"]
            or replay_proof.get("actual_digest") != run["event_digest"] or replay_proof.get("status") != STATUS
        ):
            raise ValueError("exact failed recorded replay proof required")
        row = {
            "job": job_index, "policy": policy, "status": STATUS,
            "result": arm["result"], "result_sha256": digest,
            "events": arm["events"], "events_sha256": checked["events_sha256"],
        }
        receipt = {
            "schema": "clover-static-reviewed-timing-v1",
            "registered_at_utc": datetime.now(UTC).isoformat(),
            "identity": campaign["identity"], "reviewed_failures": [row],
            "replay_proof": str(proof.resolve()), "replay_proof_sha256": file_sha(proof),
            "scope": "continue never-started inputs; failed arm stays invalid and is never retried",
            "analysis": "report failure in full denominator; exclude its timing-invalid performance pairs",
            "cause": "wall-clock overrun observed; root cause not established by recorded replay",
            "settings_changed": False, "native_retries_allowed": 0,
        }
        receipt["receipt_hash"] = fingerprint(receipt)
        if destination.exists():
            raise FileExistsError("never overwrite a registered failure review")
        destination.parent.mkdir(parents=True, exist_ok=True)
        save(destination, receipt)
        print(json.dumps({"review": str(destination), "failures": [row]}, ensure_ascii=False))


def load_review(path: Path, campaign: dict[str, Any], output: Path) -> dict[tuple[int, str], dict[str, Any]]:
    """Reject changed evidence, generic failure waivers and future unreviewed failures."""
    receipt = json.loads(path.read_text(encoding="utf-8"))
    content = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if (
        receipt.get("schema") != "clover-static-reviewed-timing-v1"
        or receipt.get("receipt_hash") != fingerprint(content) or receipt["identity"] != campaign["identity"]
        or receipt.get("settings_changed") is not False or receipt.get("native_retries_allowed") != 0
        or file_sha(Path(receipt["replay_proof"])) != receipt["replay_proof_sha256"]
        or not receipt.get("reviewed_failures")
    ):
        raise ValueError("invalid immutable same-campaign timing review")
    result = {}
    for row in receipt["reviewed_failures"]:
        job = next(j for j in campaign["jobs"] if j["index"] == row["job"])
        arm = next(a for a in job["arms"] if a["policy"] == row["policy"])
        key = (job["index"], arm["policy"])
        if (
            key in result or row["status"] != STATUS or arm["status"] != STATUS
            or arm["result"] != row["result"] or arm.get("result_sha256") != row["result_sha256"]
            or file_sha(inside(output / job["directory"], row["result"])) != row["result_sha256"]
            or arm["events"] != row["events"]
            or file_sha(inside(output / job["directory"], row["events"])) != row["events_sha256"]
        ):
            raise ValueError("reviewed failure identity or original bytes changed")
        result[key] = row
    if any(
        arm["status"] not in {"SUCCESS", "NOT_STARTED"} and (job["index"], arm["policy"]) not in result
        for job in campaign["jobs"] for arm in job["arms"]
    ):
        raise ValueError("unreviewed failures or interrupted arms remain; do not start more inputs")
    return result


def run_reviewed_validation(bundle: Path, output: Path, review: Path) -> None:
    """Measure M3 only after all development arms close, retaining reviewed failures.

    The original launcher requires every development arm to succeed. This
    explicit protocol amendment permits exact audited failures in that gate;
    it never changes original source, inputs, policies, result labels or limits.
    """
    from resume_static_scenario_study import phase_valid

    with execution_lock():
        campaign = open_campaign(bundle, output)
        reviewed = load_review(review, campaign, output)
        check_gate(campaign, output, "M2b")  # The unchanged twenty-canary technical gate.
        if not all(phase_valid(campaign, phase, reviewed) for phase in ("M2a", "M2b")):
            raise ValueError("all development arms must close; new failures still require review")
        if any(j["status"] in {"RUNNING", "INTERRUPTED", "RESUMING"} for j in campaign["jobs"]):
            raise ValueError("recover interrupted evidence before validation")
        if any(j["phase"] == "M3" and j["status"] not in {"SUCCESS", "NOT_STARTED"} for j in campaign["jobs"]):
            raise ValueError("an existing validation failure requires a new review")
        design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
        limits = design["run_limits"]
        selected = [j for j in campaign["jobs"] if j["phase"] == "M3" and j["status"] == "NOT_STARTED"]
        random.Random("static-study-order-v1:M3").shuffle(selected)
        campaign["status"] = "RUNNING"
        save(output / "campaign.json", campaign)
        for job in selected:
            audit_bundle(bundle)
            frozen_code()
            if shutil.disk_usage(output).free < limits["disk_reserve_bytes"]:
                raise OSError("disk reserve reached; no input started")
            job["status"] = "RUNNING"
            save(output / "campaign.json", campaign)
            started = time.perf_counter()
            try:
                case = Scenario.from_dict(json.loads(inside(bundle, job["scenario"]).read_text(encoding="utf-8")))
                run_suite(
                    (case,), (job["seed"],), output / job["directory"], policies=tuple(POLICIES),
                    run_wall_limit=limits["run_wall_seconds"], suite_wall_limit=limits["job_wall_seconds"],
                    planning_settings=design["rh_settings"], decision_settings=design["d1_settings"],
                    compress_artifacts=True, archive_codec=limits["archive_codec"], static_layout=True,
                )
                sync_job(job, output)
                record_metrics(output, job)
                audit_bundle(bundle)
                frozen_code()
            except Exception as exc:
                sync_job(job, output)
                job.update(status="INFRASTRUCTURE_FAILURE", failure=f"{type(exc).__name__}: {exc}")
                campaign["status"] = "STOPPED_INFRASTRUCTURE_FAILURE"
                save(output / "campaign.json", campaign)
                raise
            job["job_wall_seconds"] = time.perf_counter() - started
            job["free_disk_bytes_after"] = shutil.disk_usage(output).free
            campaign["counts"] = dict(Counter(a["status"] for j in campaign["jobs"] for a in j["arms"]))
            save(output / "campaign.json", campaign)
            print(json.dumps({"phase": "M3", "index": job["index"], "status": job["status"]}), flush=True)
            if not phase_valid({"jobs": [job]}, "M3"):
                raise RuntimeError("new validation failure or missing native coverage; retained without retries")


def main() -> None:
    """Create an explicit review or execute validation under its recorded amendment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--mode", choices=("create", "validation"), required=True)
    parser.add_argument("--replay-proof", type=Path)
    parser.add_argument("--job", type=int)
    parser.add_argument("--policy", choices=POLICIES)
    args = parser.parse_args()
    if args.mode == "create":
        if args.replay_proof is None or args.job is None or args.policy is None:
            parser.error("create requires --replay-proof, --job and --policy")
        create_review(args.bundle.resolve(), args.output.resolve(), args.review.resolve(),
                      args.replay_proof.resolve(), args.job, args.policy)
    else:
        run_reviewed_validation(args.bundle.resolve(), args.output.resolve(), args.review.resolve())


if __name__ == "__main__":
    main()
