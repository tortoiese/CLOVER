"""Descriptive static-study metrics; native attempts, returns and commits remain distinct."""

from __future__ import annotations

from collections import Counter
from typing import Any

from simulation.decisions.metrics import control_summary
from simulation.evaluation.statistics import distribution


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    """Retain censored waits and failed runs without inserting zero performance outcomes."""
    start, end = run["measurement_window"]
    trace = {j["uid"]: j for j in run["trace"]}
    tasks = {t["uid"]: t for t in run["tasks"]}
    cohorts = {}
    for label, urgent in (("urgent", True), ("ordinary", False)):
        inputs = [j for j in trace.values() if start <= j["deadline"] < end and (j["priority"] == 0) == urgent]
        late = sum(
            tasks.get(j["uid"], {}).get("completed") is None or tasks[j["uid"]]["completed"] > j["deadline"]
            for j in inputs
        )
        offered = [
            t for t in tasks.values() if start <= t["created"] < end and (trace[t["uid"]]["priority"] == 0) == urgent
        ]
        opened = [end - t["created"] for t in offered if t["started"] is None and t["terminal"] is None]
        waits = [t["started"] - t["created"] for t in offered if t["started"] is not None]
        cohorts[label] = {
            "due": len(inputs),
            "late": late,
            "late_rate": late / len(inputs) if inputs else None,
            "started_wait": distribution(waits),
            "censored_start_count": len(opened),
            "open_start_age": distribution(opened),
            "open_start_age_over_120s": sum(x > 120 for x in opened),
            "started_wait_over_120s": sum(x > 120 for x in waits),
        }
    times = [start + 60 * i for i in range(int((end - start) // 60) + 1)]
    if not times or times[-1] != end:
        times.append(end)
    series = [
        {
            "time": t,
            "backlog": sum(
                j["at"] <= t and (tasks.get(uid, {}).get("terminal") is None or tasks[uid]["terminal"] > t)
                for uid, j in trace.items()
            ),
        }
        for t in times
    ]
    mean_t = sum(r["time"] for r in series) / len(series)
    mean_b = sum(r["backlog"] for r in series) / len(series)
    denom = sum((r["time"] - mean_t) ** 2 for r in series)
    slope = sum((r["time"] - mean_t) * (r["backlog"] - mean_b) for r in series) / denom if denom else None
    control = run.get("control")
    rows = [r for r in (control or {}).get("decisions", []) if start <= r["triggered_at"] < end]
    dispatch = [r for r in rows if r["request_kind"] == "dispatch"]
    native = [r for r in dispatch if r.get("primary_kind") == "rh_plan" and r.get("compute") is not None]
    returned = [r for r in native if ((r["compute"].get("value") or {}).get("plan")) is not None]
    reports = [r["compute"]["value"]["plan"]["report"] for r in returned]
    receipt = [r for r in native if (r.get("receipt") or {}).get("primary_accepted")]
    return {
        "schema": "clover-static-arm-metrics-v1",
        "status": run["status"],
        "failure": run["failure"],
        "descriptive_only": True,
        "static_layout_audit": run.get("static_layout_audit"),
        "throughput_per_min": run["throughput_per_min"],
        "completed_in_window": run["completed_in_window"],
        "unfinished": run["unfinished"],
        "late_due_rate": run["late_due_rate"],
        "cohorts": cohorts,
        "backlog_60s": series,
        "backlog_slope_tasks_per_s": slope,
        "energy": run["energy"],
        "measurement_energy": run["measurement_energy"],
        "energy_per_completed": run["energy_per_completed"],
        "resource_waits": run["resource_waits"],
        "decision_summary": control_summary(control, start, end),
        "rh_execution": {
            "dispatch_opportunities": len(dispatch),
            "snapshots_retained": sum(bool(r.get("planning_input_ref")) for r in dispatch),
            "worker_compute_attempts": len(native),
            "returned_plans": len(returned),
            "feasible_reports": sum(r["status"] in {"OPTIMAL", "FEASIBLE"} for r in reports),
            "reports_with_native_search": sum(r.get("search_seconds", 0) > 0 for r in reports),
            "primary_prefix_commits": len(receipt),
            "worker_peak_rss_mib": max((r.get("worker_peak_rss_mib", 0) for r in reports), default=None),
            "native_report_timings": {
                name: distribution([r.get(name, 0) for r in reports])
                for name in ("build_seconds", "solve_seconds", "prepare_seconds", "solution_check_seconds")
            },
            "exclusive_control_phases": {
                name: distribution([p["wall_seconds"] for r in rows for p in r["phases"] if p["name"] == name])
                for name in sorted({p["name"] for r in rows for p in r["phases"]})
            },
            "report_statuses": dict(Counter(r["status"] for r in reports)),
            "all_dispatch_reasons": dict(Counter(r["reason"] for r in dispatch)),
            "fallback_reasons": dict(Counter(r["reason"] for r in dispatch if r.get("fallback") is not None)),
            "scope": ("worker compute attempts are not proof of a native solver entry; "
                      "feasible reports are not validated commits"),
        },
        "wall_seconds": run["wall_seconds"],
        "faults": run["faults"],
        "final_audit": run["final_audit"],
        "max_energy_error": run["max_energy_error"],
    }
