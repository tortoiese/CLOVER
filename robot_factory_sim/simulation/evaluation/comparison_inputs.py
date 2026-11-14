"""Read immutable saved suite arms and draft explicit exploratory comparison plans."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.evaluation.analysis import analyze
from simulation.evaluation.scenario import POLICIES, Scenario, fingerprint

PLAN_SCHEMA = "clover-paired-analysis-plan-v1"


def read_json(path: Path) -> tuple[dict[str, Any], str]:
    """Read an object and its exact file digest without modifying evidence."""
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object: " + str(path))
    return value, hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    """Hash large saved event/model files without retaining them in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside(root: Path, relative: str) -> Path:
    """Keep a suite's saved result/event references inside that suite."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("missing saved file reference")
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError("saved file reference escapes suite directory")
    return path


def load_arm(suite_path: Path, scenario_id: str, seed: int, policy: str) -> dict[str, Any]:
    """Verify one arm, preserving failed/unstarted rows instead of zero filling."""
    suite, suite_sha = read_json(suite_path)
    if suite.get("schema") != "clover-headless-suite-v1":
        raise ValueError("unsupported comparison suite manifest")
    scenarios, seeds, policies = suite["scenario_hashes"], suite["seeds"], suite["policies"]
    if (not scenarios or not seeds or not policies or len(set(seeds)) != len(seeds)
            or len(set(policies)) != len(policies) or set(policies) - set(POLICIES)
            or suite["planned_runs"] != len(scenarios) * len(seeds) * len(policies)):
        raise ValueError("invalid suite planned matrix")
    seen = set()
    for row in suite["runs"]:
        key = (row["scenario"], row["seed"], row["policy"])
        if (key in seen or key[0] not in scenarios or key[1] not in seeds or key[2] not in policies
                or row["case_id"] != f"{key[0]}__{key[2]}__{key[1]}"):
            raise ValueError("duplicate or undeclared suite arm")
        seen.add(key)
    key = (scenario_id, seed, policy)
    selected = next((r for r in suite["runs"] if (r["scenario"], r["seed"], r["policy"]) == key), None)
    metadata = {"policy": policy, "scenario": scenario_id, "seed": seed,
                "suite": str(suite_path), "suite_sha256": suite_sha, "result": None,
                "status": "NOT_PLANNED" if scenario_id not in scenarios or seed not in seeds or policy not in policies
                else "NOT_RECORDED", "failure": None}
    if selected is None:
        return metadata
    metadata.update(status=selected["status"], failure=selected.get("failure"))
    if selected["status"] == "NOT_RUN_BUDGET":
        if selected.get("result"):
            raise ValueError("not-run arm unexpectedly has a result")
        return metadata
    path = inside(suite_path.parent, selected["result"])
    run, run_sha = read_json(path)
    if (run.get("schema") not in {"clover-headless-run-v4", "clover-headless-run-v5"}
            or any(run[k] != selected[k] for k in ("case_id", "policy", "seed", "status"))):
        raise ValueError("result/manifest identity mismatch")
    for name, value in selected.items():
        if name not in {"scenario", "result", "events", "same_source"} and name in run and run[name] != value:
            raise ValueError("result/manifest field mismatch: " + name)
    case = Scenario.from_dict(run["scenario"])
    if (case.id != scenario_id or not fingerprint(case.to_dict()) == run["scenario_hash"] == scenarios[scenario_id]
            or fingerprint(run["trace"]) != run["trace_hash"]
            or fingerprint({"robots": {k: asdict(v) for k, v in case.initial_robots.items()},
                            "cabins": case.initial_cabins}) != run["initial_hash"]):
        raise ValueError("saved scenario/trace/initial hash mismatch")
    # Use the saved raw stream without requiring the analyzer's current source
    # to equal the old simulation source. No solver or plant is re-executed.
    rebuilt = analyze(path, inside(suite_path.parent, selected["events"]))
    if not rebuilt["stored_metrics_match"] or rebuilt["input_result_sha256"] != run_sha:
        raise ValueError("saved resource metrics do not match raw events")
    complete = (run["status"] == "SUCCESS" and run["final_audit"]["ok"]
                and run["source"]["source_hash"] == run.get("source_hash_at_end")
                == suite["source"]["source_hash"]
                and run["observed_seconds"] == case.duration
                and run["offered"] == run["scheduled_arrivals"] == len(run["trace"])
                and run["offered"] == sum(run["state_counts"].values())
                and run["max_energy_error"] < 1e-7 and selected.get("same_source", True))
    control, planning = run.get("control"), run.get("planning")
    if control and (fingerprint(control["decisions"]) != control["decisions_hash"]
                    or fingerprint(control["lifecycle"]) != control["lifecycle_hash"]):
        raise ValueError("control decision/lifecycle hash mismatch")
    if control and "planning_inputs" in control:
        from simulation.decisions.contracts import digest
        from simulation.decisions.input_store import load_planning_input

        if digest(control["planning_inputs"]) != control["planning_inputs_hash"]:
            raise ValueError("planning input index hash mismatch")
        for decision in control["decisions"]:
            if decision.get("planning_input_ref"):
                snap = load_planning_input(control, decision["planning_input_ref"], path.parent)
                if snap.token != decision["token"] or snap.epoch != run["execution_epoch"]:
                    raise ValueError("planning input observation identity mismatch")
    if planning and fingerprint(planning["decisions"]) != planning["decisions_hash"]:
        raise ValueError("planning decision hash mismatch")
    if control:
        complete = complete and control["coverage_ok"] and all(control["disposed"].values())
    if complete:
        expected = {
            "throughput_per_min": 60 * run["completed_in_window"] / (case.duration - case.warmup)
            if case.duration > case.warmup else None,
            "late_due_rate": run["late_due_jobs"] / run["due_jobs"] if run["due_jobs"] else None,
            "energy_per_completed": run["measurement_energy"]["consumed"] / run["completed_in_window"]
            if run["completed_in_window"] else None,
        }
        for field, value in expected.items():
            if (run[field] is None) != (value is None) or (value is not None and not math.isclose(
                    run[field], value, rel_tol=1e-10, abs_tol=1e-10)):
                raise ValueError("inconsistent derived metric: " + field)
    # Retain analysis fields, not potentially hundreds of MB of native models.
    compact = {k: run[k] for k in ("offered", "completed_in_window", "unfinished", "rejected", "cancelled",
                                  "due_jobs", "censored_start_count", "arrival_to_start", "throughput_per_min",
                                  "late_due_rate", "energy_per_completed", "measurement_energy", "mean_wip",
                                  "resource_waits", "wall_seconds", "faults")}
    compact["start_cohort_jobs"] = sum(case.warmup <= job["at"] < run["observed_seconds"] for job in run["trace"])
    from simulation.decisions.metrics import control_summary

    compact["control_summary"] = control_summary(control, case.warmup, run["observed_seconds"])
    if control and "summary" in control and control["summary"] != compact["control_summary"]:
        raise ValueError("saved decision cohort metrics differ")
    if run["arrival_to_start"]["count"] > compact["start_cohort_jobs"]:
        raise ValueError("observed starts exceed arrival cohort")
    tasks = run["tasks"]
    if (len(tasks) != run["offered"] or len({t["uid"] for t in tasks}) != len(tasks)
            or not {t["uid"] for t in tasks}.issubset({job["uid"] for job in run["trace"]})
            or Counter(t["status"] for t in tasks) != Counter(run["state_counts"])):
        raise ValueError("task identity/count summary mismatch")
    deadlines = {job["uid"]: job["deadline"] for job in run["trace"]}
    due_ids = sorted(t["uid"] for t in tasks if deadlines[t["uid"]] is not None
                     and case.warmup <= deadlines[t["uid"]] < run["observed_seconds"]
                     and not (t["status"] == "cancelled" and t["terminal"] is not None
                              and t["terminal"] <= deadlines[t["uid"]]))
    if len(due_ids) != run["due_jobs"]:
        raise ValueError("due-job population does not match saved task records")
    compact["due_cohort_hash"] = fingerprint(due_ids)
    compact["batch_makespan"] = (
        max(t["completed"] for t in tasks) - min(t["created"] for t in tasks)
        if complete and tasks and run["completed_total"] == run["offered"]
        and all(t["completed"] is not None and t["created"] >= case.warmup for t in tasks) else None)
    metadata.update(result=compact, result_path=str(path), result_sha256=run_sha,
                    events_path=str(inside(suite_path.parent, selected["events"])),
                    events_sha256=rebuilt["input_events_sha256"], full_observation=complete,
                    input_key=fingerprint([run["scenario_hash"], run["trace_hash"], run["initial_hash"]]),
                    sampling_input_key=fingerprint([case.factory.to_dict(), asdict(case.plant),
                                                     run["trace_hash"], run["initial_hash"],
                                                     case.duration, case.warmup]),
                    layout_hash=fingerprint(case.factory.to_dict()),
                    context={"source_hash": run["source"]["source_hash"], "environment": run["environment"],
                             "timing_mode": run["timing_mode"], "window": [case.warmup, case.duration],
                             "plant": asdict(case.plant)},
                    settings={"control": control["settings"], "math": control["math_settings"]} if control else
                    planning["settings"] if planning else None)
    return metadata


def draft_plan(manifest_path: Path, *, control: str = "optimized",
               candidates: tuple[str, ...] = ("rh_milp_grid", "rh_cpsat_linear")) -> dict[str, Any]:
    """Draft a descriptive plan; users explicitly review sampling/condition labels."""
    manifest_path = manifest_path.resolve()
    manifest, digest = read_json(manifest_path)
    blocks = []
    unidentified = 0
    if manifest.get("schema") == "clover-prepared-runs-v1":
        declared = sum(len(row["policies"]) for row in manifest["entries"])
        if type(manifest["planned_runs"]) is not int or manifest["planned_runs"] < declared:
            raise ValueError("invalid prepared parent planned count")
        unidentified = manifest["planned_runs"] - declared
        for row in manifest["entries"]:
            blocks.append({"condition": row["cell"], "layout": manifest["layout_id"] + ":" + row.get("family", ""),
                           "replicate": str(row["run_seed"]), "seed": row["run_seed"],
                           "stochastic": row.get("demand_seed") is not None,
                           "demand_kind": "finite_batch" if row.get("demand_seed") is None else "stream",
                           "scenario": row["scenario"],
                           "suite": str(inside(manifest_path.parent, row["suite"])) if row.get("suite") else None,
                           "unstarted_status": row["status"] if not row.get("suite") else None})
    elif manifest.get("schema") == "clover-headless-suite-v1":
        for scenario_id in manifest["scenario_hashes"]:
            for seed in manifest["seeds"]:
                blocks.append({"condition": scenario_id, "layout": "REVIEW_LAYOUT:" + scenario_id,
                               "replicate": str(seed), "seed": seed, "stochastic": False,
                               "demand_kind": "unreviewed",
                               "scenario": scenario_id, "suite": str(manifest_path)})
    else:
        raise ValueError("expected a prepared-run parent or headless suite")
    return {"schema": PLAN_SCHEMA, "purpose": "e0", "control": control, "candidates": list(candidates),
            "metrics": ["throughput_per_min", "late_due_rate", "energy_per_completed", "mean_wip",
                        "charge_queue_robot_seconds", "arrival_to_start_p95", "recorded_run_wall_seconds",
                        "batch_makespan"],
            "confidence": .95, "bootstrap_resamples": 5000, "bootstrap_seed": 1001,
            "minimum_seed_pairs": 10, "minimum_layouts": 3, "layout_sampling_independent": False,
            "hardware_id": None,
            "blocks": blocks, "draft_from": {"manifest": str(manifest_path), "sha256": digest,
                                                "unidentified_parent_arm_slots": unidentified},
            "scope": "exploratory draft; review layout/condition/sampling labels; E0 has no inference"}
