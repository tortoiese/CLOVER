"""Audit fixed observation windows and meaningful completed charging sessions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from simulation.evaluation.analysis import analyze
from simulation.evaluation.comparison_inputs import read_json
from simulation.evaluation.scenario import Scenario, integer, number


def validate_window(case: Scenario, window: dict[str, Any]) -> None:
    """Require an explicit window matching the runner's half-open observation."""
    if window.get("mode") not in {"finite_batch", "finite_stream", "steady_stream", "transient_stress"}:
        raise ValueError("unknown observation window mode")
    cutoff = number(window["arrival_cutoff"], "arrival cutoff", .001, case.duration)
    drain = number(window["drain_seconds"], "drain seconds", 0, case.duration)
    if abs(cutoff + drain - case.duration) > 1e-9:
        raise ValueError("arrival cutoff + common drain must equal duration")
    if window.get("drain_in_measurement") is not True:
        raise ValueError("runner measures warmup..duration, including any drain")
    if window["mode"] == "steady_stream" and drain:
        raise ValueError("steady-stream window cannot contain a drain")
    if window["mode"] == "finite_batch" and case.workload["kind"] != "trace":
        raise ValueError("finite batch requires an explicit trace")
    if case.workload["kind"] == "trace":
        if any(row["at"] >= cutoff for row in case.workload["jobs"]):
            raise ValueError("arrival at or after declared cutoff")
    elif cutoff != case.duration:
        raise ValueError("a stream with an arrival cutoff must be materialized as a trace")
    number(window["minimum_observation_seconds"], "minimum observation", .001, case.duration - case.warmup)
    integer(window["minimum_completed_charge_sessions_per_robot"], "minimum charge sessions", 0, 10000)
    number(window["minimum_charge_fraction"], "minimum charge fraction", .001, 1)
    if type(window["nominal"]) is not bool:
        raise ValueError("nominal/stress status must be explicit")
    if window["mode"] == "steady_stream" and window["minimum_completed_charge_sessions_per_robot"] < 2:
        raise ValueError("steady-stream exposure requires at least two completed sessions per robot")


def calibrate(run_path: Path, window: dict[str, Any], *, bins: int = 6) -> dict[str, Any]:
    """Verify raw observations; report exposure, without asserting stationarity.

    Completed sessions must start and finish inside the measurement window and
    deliver the predeclared fraction of capacity. Starts and interrupted sessions
    are not full cycles. A pass proves the declared exposure, not steady state.
    """
    run_path = run_path.resolve()
    run, run_sha = read_json(run_path)
    case = Scenario.from_dict(run["scenario"])
    validate_window(case, window)
    integer(bins, "calibration bins", 2, 100)
    audit = analyze(run_path)
    reasons = []
    if not audit["stored_metrics_match"] or run["status"] != "SUCCESS" or not run["final_audit"]["ok"]:
        reasons.append("INCOMPLETE_OR_FAILED_AUDIT")
    if (run["observed_seconds"] != case.duration
            or run["source"]["source_hash"] != run["source_hash_at_end"]):
        reasons.append("INCOMPLETE_WINDOW_OR_CHANGED_SOURCE")
    if run["max_energy_error"] >= 1e-7:
        reasons.append("ENERGY_BALANCE_ERROR")
    if window["nominal"] and (run["faults"] or run["energy"]["rescue"] > 1e-9):
        reasons.append("NOMINAL_FAULT_OR_RESCUE")
    robots = {r.id: {"completed": 0, "interrupted": 0, "carry_in": 0, "small": 0, "open": 0,
                      "delivered_energy": 0.0} for r in case.factory.robots if r.is_active}
    active, starts, ends = {}, 0, 0
    with run_path.with_suffix(".events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["event"] not in {"charge_session_start", "charge_session_end"}:
                continue
            key = (row["robot"], row["robot_instance"], row["session"])
            if row["event"] == "charge_session_start":
                if key in active:
                    raise ValueError("duplicate charging start")
                active[key] = row
                starts += 1
                continue
            ends += 1
            start = active.pop(key, None)
            if start is None or start["charger"] != row["charger"] or start["start"] != row["start"]:
                raise ValueError("unmatched charging end")
            if row["time"] < case.warmup:
                continue
            counts = robots.setdefault(row["robot"], dict.fromkeys(robots[next(iter(robots))], 0))
            delivered = row["charged_energy_units"] - start["charged_energy_units"]
            if delivered < -1e-9:
                raise ValueError("negative charging energy")
            counts["delivered_energy"] += max(0, delivered)
            if start["time"] < case.warmup:
                counts["carry_in"] += 1
            elif not row["completed"]:
                counts["interrupted"] += 1
            elif delivered + 1e-9 < start["capacity_units"] * window["minimum_charge_fraction"]:
                counts["small"] += 1
            else:
                counts["completed"] += 1
    for key, start in active.items():
        if start["time"] < run["observed_seconds"]:
            robots[key[0]]["open"] += 1
    required = window["minimum_completed_charge_sessions_per_robot"]
    if any(r["completed"] < required for r in robots.values()):
        reasons.append("INSUFFICIENT_COMPLETED_CHARGE_EXPOSURE")
    tasks, segments = run["tasks"], []
    for i in range(bins):
        left = case.warmup + (case.duration - case.warmup) * i / bins
        right = case.warmup + (case.duration - case.warmup) * (i + 1) / bins
        completed = sum(t["completed"] is not None and left <= t["completed"] < right for t in tasks)

        def wip(at: float) -> int:
            return sum(t["created"] < at and (t["terminal"] is None or t["terminal"] >= at) for t in tasks)

        segments.append({"start": left, "end": right, "completions": completed,
                         "throughput_per_min": completed * 60 / (right - left),
                         "wip_before_start": wip(left), "wip_before_end": wip(right)})
    return {"schema": "clover-window-calibration-v1", "status": "PASS" if not reasons else "FAIL",
            "reasons": reasons, "run": str(run_path), "result_sha256": run_sha,
            "events_sha256": audit["input_events_sha256"], "source": run["source"],
            "scenario_hash": run["scenario_hash"], "scenario": case.to_dict(), "seed": run["seed"],
            "policy": run["policy"], "window": window, "measurement_window": [case.warmup, case.duration],
            "charge_starts": starts, "charge_ends": ends, "robots": robots, "segments": segments,
            "faults": run["faults"], "rescue_energy": run["energy"]["rescue"],
            "scope": "synthetic/input exposure audit; PASS does not prove stationarity or real-factory validity"}
