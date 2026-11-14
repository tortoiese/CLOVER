"""Deadline-aware decision cohorts, distinct from task and auxiliary counts."""

from __future__ import annotations

from typing import Any

import numpy as np

from simulation.decisions.contracts import DEADLINE_EPSILON
from simulation.evaluation.statistics import distribution


def control_summary(control: dict[str, Any] | None, start: float, end: float) -> dict[str, Any]:
    """Summarize triggers whose complete deadline lies inside the window.

    Pending decisions past their deadline are failures of cycle timeliness.
    Triggers with deadlines beyond observation end are right censored instead.
    """
    if not control or any("request_kind" not in r for r in control["decisions"]):
        return {"available": False, "scope": "unrecorded decision request kinds"}
    deadline = control["settings"]["end_to_end_soft_deadline_seconds"]
    result: dict[str, Any] = {"available": True, "window": [start, end], "by_kind": {}}
    for kind in ("dispatch", "auxiliary"):
        triggered = [r for r in control["decisions"] if r["request_kind"] == kind
                     and start <= r["triggered_at"] < end]
        rows = [r for r in triggered if r["triggered_at"] + deadline < end]
        finalized = [r for r in rows if r["time"] is not None]
        latency = [r["time"] - r["triggered_at"] for r in finalized]
        timely = sum(bool(r["accepted"] and r["coverage_ok"]
                          and r["time"] - r["triggered_at"] <= deadline + DEADLINE_EPSILON) for r in finalized)
        late = sum(r["time"] is None or r["time"] - r["triggered_at"] > deadline + DEADLINE_EPSILON for r in rows)
        latency_summary = distribution(latency)
        latency_summary["p99"] = float(np.quantile(latency, .99)) if latency else None
        result["by_kind"][kind] = {
            "triggered": len(triggered), "eligible": len(rows),
            "right_censored_deadline": len(triggered) - len(rows),
            "finalized": len(finalized), "pending": len(rows) - len(finalized),
            "accepted": sum(bool(r["accepted"]) for r in rows), "timely_accepted": timely,
            "late_accepted": sum(bool(r["accepted"]) and r["time"] is not None
                                 and r["time"] - r["triggered_at"] > deadline + DEADLINE_EPSILON for r in rows),
            "deadline_exceeded": late, "fallback": sum(r.get("fallback") is not None for r in rows),
            "timely_acceptance_rate": timely / len(rows) if rows else None,
            "deadline_exceeded_rate": late / len(rows) if rows else None,
            "fallback_rate": sum(r.get("fallback") is not None for r in rows) / len(rows)
            if rows and len(finalized) == len(rows) else None,
            "latency": latency_summary,
        }
    result["scope"] = ("decision opportunities, not jobs; late-window triggers censored; "
                       "latency observed completions only")
    return result
