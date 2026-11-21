"""Resume frozen static studies without repeating completed policy runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import psutil
from audit_korean_case_pilot import execution_lock, inside
from materialize_static_scenario_study import (
    POLICIES,
    REPO,
    Scenario,
    file_sha,
    fingerprint,
)
from run_static_scenario_study import (
    check_gate,
    frozen_code,
    open_campaign,
    record_metrics,
    sync_job,
)
from simulation.artifacts import archive_run, events_for_result, open_saved, stored_path
from simulation.evaluation.comparison_inputs import load_arm
from simulation.evaluation.runner import make_trace, run_case
from simulation.evaluation.study import save

PHASES = ("M2a", "M2b", "M3")
ROW_KEYS = (
    "case_id",
    "policy",
    "seed",
    "status",
    "failure",
    "scenario_hash",
    "initial_hash",
    "trace_hash",
    "offered",
    "completed_total",
    "unfinished",
    "rejected",
    "cancelled",
    "throughput_per_min",
    "late_due_rate",
    "mean_wip",
    "faults",
    "event_digest",
    "max_energy_error",
    "wall_seconds",
)


def now() -> str:
    """Return an unambiguous heartbeat timestamp."""
    return datetime.now(UTC).isoformat()


def health(driver: dict[str, Any]) -> dict[str, Any]:
    """Distinguish a saved RUNNING label from an actual matching live process."""
    result = {"recorded_status": driver.get("status"), "derived_status": driver.get("status"), "pid_alive": False}
    try:
        process = psutil.Process(driver["pid"])
        if "process_created_at" not in driver:
            result.update(pid_alive=True, derived_status="UNKNOWN_PROCESS_IDENTITY")
            return result
        matched = process.create_time() == driver.get("process_created_at")
        result["pid_alive"] = matched and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, KeyError):
        pass
    except psutil.AccessDenied:
        result["derived_status"] = "UNKNOWN_PROCESS_ACCESS"
        return result
    if driver.get("status") == "RUNNING" and not result["pid_alive"]:
        result["derived_status"] = "STOPPED_PROCESS_MISSING"
    if driver.get("child_pid"):
        try:
            child = psutil.Process(driver["child_pid"])
            child_alive = child.create_time() == driver.get("child_process_created_at") and child.is_running()
            result["child_pid_alive"] = child_alive
            if child_alive and not result["pid_alive"]:
                result["derived_status"] = "ORPHANED_CHILD_ACTIVE"
        except psutil.NoSuchProcess:
            result["child_pid_alive"] = False
        except psutil.AccessDenied:
            result["child_pid_alive"] = None
            if not result["pid_alive"]:
                result["derived_status"] = "UNKNOWN_CHILD_ACCESS"
    if driver.get("heartbeat_at_utc"):
        age = (datetime.now(UTC) - datetime.fromisoformat(driver["heartbeat_at_utc"])).total_seconds()
        result["heartbeat_age_seconds"] = age
        if result["pid_alive"] and age > 60:
            result["derived_status"] = "RUNNING_HEARTBEAT_STALE"
    return result


def policy_order(case: Scenario, seed: int) -> list[str]:
    """Use exactly the original run_suite within-input randomized policy order."""
    initial = int(fingerprint(case.to_dict())[:8], 16)
    return np.random.default_rng(np.random.SeedSequence([seed, initial, 303])).permutation(POLICIES).tolist()


def missing_policies(job: dict[str, Any], case: Scenario) -> list[str]:
    """Permit only never-started policies; failed or unfinalized arms require review."""
    states = {a["policy"]: a["status"] for a in job["arms"]}
    if set(states) != set(POLICIES) or any(v not in {"SUCCESS", "NOT_STARTED"} for v in states.values()):
        raise ValueError("recover artifacts first; failed and interrupted runs are never automatically retried")
    return [p for p in policy_order(case, job["seed"]) if states[p] == "NOT_STARTED"]


def archive_copy(source: Path, destination: Path, codec: str) -> tuple[Path, dict[str, Any]]:
    """Archive verified copies while retaining the original raw and pending evidence.

    Args:
        source: Complete original result JSON, possibly beside interrupted compression.
        destination: New exclusive directory for the recovered result/events/CAS.
        codec: The frozen lossless codec.

    Returns:
        The recovered archive path and its ordinary archive_run metadata.
    """
    source = source.resolve()
    original = file_sha(source)
    run = json.loads(source.read_text(encoding="utf-8"))
    destination.mkdir(parents=True, exist_ok=False)
    files = [(Path(source.name), source), (Path(source.stem + ".events.jsonl"), events_for_result(source))]
    for entry in (run.get("control") or {}).get("planning_inputs", {}).values():
        if entry.get("path"):
            files.append((Path(entry["path"]), stored_path(inside(source.parent, entry["path"]))))
    copied = {}
    for relative, path in dict.fromkeys(files):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with open_saved(path) as original_stream, target.open("xb") as copy_stream:
            for chunk in iter(lambda: original_stream.read(1024 * 1024), b""):
                copy_stream.write(chunk)
                digest.update(chunk)
        copied[str(relative)] = digest.hexdigest()
    archived, metadata = archive_run(destination / source.name, codec)
    if set(copied) != set(metadata["archive_files"]) or any(
        metadata["archive_files"][name]["logical_sha256"] != expected for name, expected in copied.items()
    ):
        raise ValueError("recovered result/events/CAS bytes differ from source copies")
    if file_sha(source) != original or metadata["archive_files"][source.name]["logical_sha256"] != original:
        raise ValueError("recovery did not preserve original result bytes")
    return archived, metadata


def result_row(
    run: dict[str, Any], archived: Path, metadata: dict[str, Any], job_root: Path, session: str
) -> dict[str, Any]:
    """Match the original suite schema, adding explicit continuation provenance."""
    job_root, archived = job_root.resolve(), archived.resolve()
    row = {key: run[key] for key in ROW_KEYS}
    prefix = archived.parent.relative_to(job_root)
    row.update(
        scenario=run["scenario"]["id"],
        result=str(archived.relative_to(job_root)),
        events=str(events_for_result(archived).relative_to(job_root)),
        **metadata,
    )
    row["archive_files"] = {str(prefix / name): value for name, value in metadata["archive_files"].items()}
    row.update(same_source=True, resume_session=session)
    return row


def checkpoint(output: Path, campaign: dict[str, Any], job: dict[str, Any], suite: dict[str, Any]) -> None:
    """Publish both ledgers after each finalized arm, keeping the complete denominator."""
    candidate = output / job["directory"] / "resume_candidate_manifest.json"
    save(candidate, suite)
    latest = suite["runs"][-1]
    if latest["status"] == "SUCCESS":
        checked = load_arm(candidate, latest["scenario"], job["seed"], latest["policy"])
        if not checked["full_observation"]:
            raise ValueError("new resumed arm failed full raw verification")
    for row in suite["runs"]:
        next(a for a in job["arms"] if a["policy"] == row["policy"]).update(row)
    record_metrics(output, job)
    campaign["counts"] = dict(Counter(a["status"] for j in campaign["jobs"] for a in j["arms"]))
    save(output / job["directory"] / "manifest.json", suite)
    save(output / "campaign.json", campaign)


def recover_interrupted(bundle: Path, output: Path, session: Path) -> None:
    """Finalize complete raw results, then execute only missing policies in interrupted inputs."""
    with execution_lock():
        campaign = open_campaign(bundle, output)
        design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
        limits = design["run_limits"]
        for job in campaign["jobs"]:
            if job["status"] != "INTERRUPTED":
                continue
            check_gate(campaign, output, job["phase"])
            job_root = output / job["directory"]
            suite = json.loads((job_root / "manifest.json").read_text(encoding="utf-8"))
            save(session / f"{job['directory']}_before.json", suite)
            started = time.perf_counter()
            job.update(
                status="RESUMING",
                continuation_session=session.name,
                original_job_elapsed_unknown=True,
                resumed_job_budget_scope="3600s active continuation segment; original interrupted total is uncertified",
            )
            save(output / "campaign.json", campaign)
            for arm in job["arms"]:
                if arm["status"] != "RESULT_UNFINALIZED":
                    continue
                original = inside(job_root, arm["result"])
                if file_sha(original) != arm["result_sha256"]:
                    raise ValueError("unfinalized raw result changed since independent audit")
                run = json.loads(original.read_text(encoding="utf-8"))
                if (
                    run["status"] != "SUCCESS"
                    or run["scenario_hash"] != job["scenario_hash"]
                    or run["source"]["source_hash"] != campaign["identity"]["source_hash"]
                ):
                    raise ValueError("only complete same-source raw results may be finalized without rerunning")
                before_sha = file_sha(original)
                destination = job_root / "recovered" / session.name / arm["policy"]
                archived, metadata = archive_copy(original, destination, limits["archive_codec"])
                row = result_row(run, archived, metadata, job_root, session.name)
                suite["runs"].append(row)
                receipt = {
                    "schema": "clover-static-raw-recovery-v1",
                    "job": job["index"],
                    "policy": arm["policy"],
                    "original_result": str(original.relative_to(output)),
                    "original_sha256": before_sha,
                    "recovered_result": str(archived.relative_to(output)),
                    "recovered_stored_sha256": file_sha(archived),
                    "logical_sha256": metadata["archive_files"][original.name]["logical_sha256"],
                    "additional_solver_runs": 0,
                    "original_raw_retained": True,
                }
                save(session / f"{job['directory']}_recovery.json", receipt)
                checkpoint(output, campaign, job, suite)
                print(json.dumps({"recovered": job["index"], "policy": arm["policy"]}), flush=True)
                del run
            case = Scenario.from_dict(json.loads(inside(bundle, job["scenario"]).read_text(encoding="utf-8")))
            for policy in missing_policies(job, case):
                frozen_code()
                if time.perf_counter() - started >= limits["job_wall_seconds"]:
                    raise TimeoutError("continuation job budget exhausted; unstarted policies retained")
                if shutil.disk_usage(output).free < limits["disk_reserve_bytes"]:
                    raise OSError("disk reserve reached before next resumed policy")
                case_id = f"{case.id}__{policy}__{job['seed']}"
                if list(job_root.glob(case_id + ".*")):
                    raise ValueError("uncheckpointed artifacts exist; never automatically repeat that policy")
                run = run_case(
                    case,
                    policy,
                    job["seed"],
                    trace=make_trace(case, job["seed"]),
                    output=job_root,
                    wall_limit=limits["run_wall_seconds"],
                    planning_settings=design["rh_settings"],
                    decision_settings=design["d1_settings"] if job["mode"] == "D1" else None,
                    static_layout=True,
                )
                if run["source"]["source_hash"] != campaign["identity"]["source_hash"]:
                    raise ValueError("application source changed")
                archived, metadata = archive_run(job_root / f"{case_id}.json", limits["archive_codec"])
                suite["runs"].append(result_row(run, archived, metadata, job_root, session.name))
                checkpoint(output, campaign, job, suite)
                print(json.dumps({"resumed_job": job["index"], "policy": policy, "status": run["status"]}), flush=True)
                if run["status"] != "SUCCESS":
                    raise RuntimeError("resumed policy did not succeed; retained without retries")
                del run
            job.update(status="SUCCESS", failure=None, continuation_wall_seconds=time.perf_counter() - started)
            suite.update(
                status="SUCCESS",
                continuation_session=session.name,
                continuation_wall_seconds=job["continuation_wall_seconds"],
                cross_session=True,
            )
            checkpoint(output, campaign, job, suite)


def phase_valid(campaign: dict[str, Any], phase: str) -> bool:
    """Require complete operating evidence and real RH use before the next stage."""
    return all(
        j["status"] == "SUCCESS"
        and all(
            a["status"] == "SUCCESS"
            and (
                not a["policy"].startswith("rh_")
                or (
                    a["metrics"]["rh_execution"]["reports_with_native_search"] > 0
                    and a["metrics"]["rh_execution"]["primary_prefix_commits"] > 0
                )
            )
            for a in j["arms"]
        )
        for j in campaign["jobs"]
        if j["phase"] == phase
    )


def supervise(bundle: Path, output: Path, session: Path) -> None:
    """Monitor original frozen launchers in separate process groups with durable heartbeat/exit receipts."""
    campaign = open_campaign(bundle, output)
    previous_driver = json.loads((output / "driver.json").read_text(encoding="utf-8"))
    if health(previous_driver)["pid_alive"]:
        raise ValueError("another matching supervisor is alive")
    if previous_driver.get("child_pid"):
        try:
            child_process = psutil.Process(previous_driver["child_pid"])
            if child_process.create_time() == previous_driver.get("child_process_created_at"):
                raise ValueError("an orphaned matching child is still active; do not duplicate execution")
        except psutil.NoSuchProcess:
            pass
    from simulation.evaluation.main_jobs import _host_lock

    with _host_lock(output):
        session.mkdir(parents=True, exist_ok=False)
        save(session / "campaign_before.json", campaign)
        save(session / "driver_before.json", previous_driver)
        design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
        remaining = [
            (j["index"], a["policy"]) for j in campaign["jobs"] for a in j["arms"] if a["status"] == "NOT_STARTED"
        ]
        orders = {}
        for phase in PHASES:
            ids = [j["index"] for j in campaign["jobs"] if j["phase"] == phase and j["status"] == "NOT_STARTED"]
            random.Random("static-study-order-v1:" + phase).shuffle(ids)
            orders[phase] = ids
        protocol = {
            "schema": "clover-static-resume-protocol-v1",
            "session": session.name,
            "registered_at_utc": now(),
            "identity": campaign["identity"],
            "orchestration_code": frozen_code(),
            "supervisor_sha256": file_sha(Path(__file__)),
            "original_runtime_commit": "7445390",
            "remaining_native_runs": len(remaining),
            "remaining_arms": remaining,
            "phase_job_orders": orders,
            "recovery_native_reruns_allowed": 0,
            "math_and_decision_settings_changed": False,
            "limits": design["run_limits"],
            "heartbeat_seconds": 15,
            "observer_scope": "supervisor process, outside timed original phase launcher",
            "interrupted_job_budget": "3600s active continuation segment; previous unfinished archive elapsed unknown",
            "analysis": "retain original sessions and cross_session flag; report interrupted-block sensitivity",
        }
        save(session / "protocol.json", protocol)
        state = {
            "schema": "clover-static-driver-v2",
            "status": "RUNNING",
            "pid": os.getpid(),
            "process_created_at": psutil.Process().create_time(),
            "started_at_utc": now(),
            "heartbeat_at_utc": now(),
            "runtime_checkout": str(REPO),
            "session": session.name,
            "identity": campaign["identity"],
            "completed_phases": [],
            "protocol": str(session.relative_to(output) / "protocol.json"),
        }
        stop = False
        original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

        def request_stop(signum: int, _frame: Any) -> None:
            nonlocal stop
            stop = True
            state["stop_signal"] = signum

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)

        def child(arguments: list[str], label: str) -> None:
            state.update(current_phase=label, heartbeat_at_utc=now())
            with (session / (label + ".log")).open("xb") as log:
                process = subprocess.Popen(
                    [sys.executable, "-B", *arguments],
                    cwd=REPO,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                state.update(child_pid=process.pid, child_process_created_at=psutil.Process(process.pid).create_time())
                save(output / "driver.json", state)
                while True:
                    try:
                        code = process.wait(timeout=15)
                        break
                    except subprocess.TimeoutExpired:
                        state["heartbeat_at_utc"] = now()
                        save(output / "driver.json", state)
                        if stop:
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                state.update(last_child_exit_code=code, heartbeat_at_utc=now())
                state.pop("child_pid", None)
                save(output / "driver.json", state)
                if code or stop:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    raise RuntimeError(f"{label} exited {code}; partial data retained, no retries")

        try:
            child(
                [
                    str(Path(__file__)),
                    "--bundle",
                    str(bundle),
                    "--output",
                    str(output),
                    "--session",
                    session.name,
                    "--mode",
                    "recover",
                ],
                "RECOVERY",
            )
            for phase in PHASES:
                child(
                    [
                        str(REPO / "scripts/run_static_scenario_study.py"),
                        "--bundle",
                        str(bundle),
                        "--output",
                        str(output),
                        "--phase",
                        phase,
                    ],
                    phase,
                )
                current = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
                if not phase_valid(current, phase):
                    raise RuntimeError("phase incomplete or RH native coverage missing; review required")
                state["completed_phases"].append(phase)
                child(
                    [
                        str(REPO / "scripts/run_static_scenario_study.py"),
                        "--bundle",
                        str(bundle),
                        "--output",
                        str(output),
                        "--phase",
                        "report",
                    ],
                    phase + "_REPORT",
                )
            state["status"] = "COMPLETE"
        except BaseException as exc:
            state.update(status="STOPPED_REQUIRES_REVIEW", failure=f"{type(exc).__name__}: {exc}")
            current = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
            for job in current["jobs"]:
                if job["status"] in {"RUNNING", "RESUMING"}:
                    sync_job(job, output)
                    record_metrics(output, job)
                    job.update(status="INTERRUPTED", failure=state["failure"])
            current["status"] = "STOPPED_INTERRUPTED"
            current["counts"] = dict(Counter(a["status"] for j in current["jobs"] for a in j["arms"]))
            save(output / "campaign.json", current)
            raise
        finally:
            state["heartbeat_at_utc"] = now()
            save(output / "driver.json", state)
            save(session / "exit.json", state)
            for sig, handler in original_handlers.items():
                signal.signal(sig, handler)


def main() -> None:
    """Provide explicit supervision, recovery and truthful read-only status commands."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--session", default="resume_20261121")
    parser.add_argument("--mode", required=True, choices=["supervise", "recover", "status"])
    args = parser.parse_args()
    bundle, output = args.bundle.resolve(), args.output.resolve()
    if not args.session.replace("_", "").isalnum():
        raise ValueError("safe session id required")
    session = output / "resume_sessions" / args.session
    if args.mode == "status":
        driver = json.loads((output / "driver.json").read_text(encoding="utf-8"))
        print(json.dumps({**health(driver), "driver": driver}, ensure_ascii=False, indent=2))
    elif args.mode == "recover":
        protocol = json.loads((session / "protocol.json").read_text(encoding="utf-8"))
        if protocol["supervisor_sha256"] != file_sha(Path(__file__)):
            raise ValueError("resume tool changed after registration")
        recover_interrupted(bundle, output, session)
    else:
        supervise(bundle, output, session)


if __name__ == "__main__":
    main()
