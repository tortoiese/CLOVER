"""Descriptive staged-control costs with explicit publication and startup scopes."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def summarize(group: dict[str, Any], key: str) -> dict[str, Any]:
    """Summarize recorded exclusive phases, not overlapping native report totals."""
    from simulation.evaluation.profiling import describe, seconds

    rows = group["decisions"]
    n = len(rows)
    stages: dict[str, list[float]] = defaultdict(list)
    measured, latency, applications, allowances, queue_wait = [], [], [], [], []
    statuses = Counter()
    complete = accepted = cold = violations = deadline = 0
    for row in rows:
        statuses[row["reason"]] += 1
        complete += row["time"] is not None
        accepted += bool(row["accepted"])
        cold += "startup" in row and row["startup"].get("status") != "DISABLED_FOR_RUN"
        violations += not row["coverage_ok"]
        deadline += bool(row.get("soft_deadline_exceeded", False))
        per_phase: dict[str, float] = defaultdict(float)
        for phase in row["phases"]:
            value = seconds(phase["wall_seconds"])
            seconds(phase["injected_seconds"])
            per_phase[phase["name"]] += value
        for name, value in per_phase.items():
            stages[name].append(value)
        measured.append(sum(per_phase.values()))
        if row["time"] is not None:
            latency.append(seconds(row["time"] - row["triggered_at"]))
        if "publication" in per_phase:
            applications.append(per_phase["publication"])
            allowances.append(sum(p["injected_seconds"] for p in row["phases"] if p["name"] == "publication"))
        if "queue_sim_wait_seconds" in row:
            queue_wait.append(seconds(row["queue_sim_wait_seconds"]))
    return {
        "group_hash": key,
        **group["identity"],
        "runs": len(group["runs"]),
        "cases": group["runs"],
        "run_statuses": dict(group["statuses"]),
        "decisions": n,
        "accepted": accepted,
        "pending_at_end": n - complete,
        "solver_statuses": {},
        "decision_statuses": dict(statuses),
        "fallback_reasons": dict(group["fallbacks"]),
        "timing_available": bool(n),
        "timing_finalized_decisions": complete,
        "cold_worker_decisions": cold,
        "coverage_violations": violations,
        "soft_budget_observed": complete,
        "soft_budget_exceeded": deadline,
        "parent_seconds": describe([], n),
        "request_sim_delay_seconds": describe(latency, n),
        "application_wall_seconds": describe(applications, n),
        "control_measured_seconds": describe(measured, n),
        "publication_prepaid_seconds": describe(allowances, n),
        "queue_sim_wait_seconds": describe(queue_wait, n),
        "stages": {name: describe(values, n) for name, values in sorted(stages.items())},
        "scope": "exclusive control phases; parent_seconds unavailable; startup/RPC reports overlap phases",
    }
