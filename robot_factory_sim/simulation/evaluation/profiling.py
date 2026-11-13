"""Budget-aware descriptive timing summaries; no policy recommendation or CI."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from simulation.evaluation.scenario import fingerprint
from simulation.evaluation.statistics import distribution


def seconds(value: Any) -> float | None:
    """Validate recorded durations while preserving absent data as unknown."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid recorded timing duration")
    return float(value)


def describe(values: list[float], total: int) -> dict[str, Any]:
    """Report observed quantiles with the count of unrecorded observations."""
    return {
        **distribution(values),
        "p99": float(np.quantile(values, 0.99)) if values else None,
        "unrecorded": total - len(values),
    }


def profile_manifest(path: Path) -> dict[str, Any]:
    """Group saved decisions by exact policy/settings/source/time-mode identity.

    Missing or unstarted phases are never substituted by zero. Distinct
    resolutions, action scopes, budgets and D0/D1 modes are separate groups.
    """
    path = path.resolve()
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema") != "clover-headless-suite-v1":
        raise ValueError("unsupported timing suite manifest")
    groups, skipped, seen, digests = {}, [], set(), {}
    for row in manifest["runs"]:
        case = row["case_id"]
        if case in seen:
            raise ValueError("duplicate timing case")
        seen.add(case)
        if row["status"] == "NOT_RUN_BUDGET":
            if row.get("result"):
                raise ValueError("not-run entry unexpectedly has a result")
            skipped.append(case)
            continue
        relative = Path(row["result"])
        target = (path.parent / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(path.parent):
            raise ValueError("timing result path escapes suite directory")
        result_raw = target.read_bytes()
        run = json.loads(result_raw)
        if (
            run.get("schema") not in {"clover-headless-run-v4","clover-headless-run-v5"}
            or run["case_id"] != case
            or run["policy"] != row["policy"]
            or run["seed"] != row["seed"]
            or run["status"] != row["status"]
        ):
            raise ValueError("timing result/manifest identity mismatch")
        source = run["source"]["source_hash"]
        if source != manifest["source"]["source_hash"] or run.get("source_hash_at_end") != source:
            raise ValueError("source mismatch; timing groups require frozen source")
        if not isinstance(run.get("scenario_hash"), str) or not run["scenario_hash"]:
            raise ValueError("missing scenario identity in timing result")
        if row.get("scenario_hash", run["scenario_hash"]) != run["scenario_hash"]:
            raise ValueError("scenario mismatch in timing manifest")
        planning = run.get("planning")
        control = run.get("control")
        if control and (fingerprint(control["decisions"])!=control["decisions_hash"]
                        or fingerprint(control["lifecycle"])!=control["lifecycle_hash"]):
            raise ValueError("timing control/lifecycle hash mismatch")
        if planning and fingerprint(planning["decisions"]) != planning["decisions_hash"]:
            raise ValueError("timing decision hash mismatch")
        identity = {
            "policy": run["policy"],
            "scenario_hash": run["scenario_hash"],
            "source_hash": source,
            "settings": {"decision":control["settings"],"math":control["math_settings"]} if control else (
                planning["settings"] if planning else None),
            "timing_mode": control["settings"]["timing_protocol"] if control else (
                planning["settings"].get("timing_mode", "D0") if planning else "D0"),
        }
        key = fingerprint(identity)
        if key not in groups:
            groups[key] = {
                "identity": identity,
                "runs": [],
                "statuses": Counter(),
                "decisions": [],
                "fallbacks": Counter(),
            }
        group = groups[key]
        group["runs"].append(case)
        group["statuses"][run["status"]] += 1
        if planning:
            group["decisions"].extend(planning["decisions"])
            group["fallbacks"].update(planning["fallbacks"])
        if control:
            group["control"]=True
            group["decisions"].extend(control["decisions"])
            group["fallbacks"].update(control["fallbacks"])
        digests[relative.as_posix()] = fingerprint(json.loads(result_raw))
    summary = []
    for key, group in sorted(groups.items()):
        if group.get("control"):
            from simulation.decisions.profiling import summarize
            summary.append(summarize(group,key))
            continue
        records = group["decisions"]
        n = len(records)
        parent, request, application = [], [], []
        stage_values: dict[str, list[float]] = defaultdict(list)
        last, statuses = Counter(), Counter()
        complete = accepted = cold = known_budget = exceeded = 0
        for decision in records:
            report = decision["result"]["report"]
            statuses[report["status"]] += 1
            accepted += bool(decision["accepted"])
            complete += bool(report.get("timing_complete", False))
            cold += report.get("worker_reused") is False
            last[report.get("timing_stage", "unrecorded")] += 1
            elapsed = seconds(report.get("parent_elapsed_seconds"))
            if elapsed is not None:
                parent.append(elapsed)
                budget = group["identity"]["settings"].get("timeout")
                if budget is not None:
                    known_budget += 1
                    exceeded += elapsed > float(budget)
            for field, values in (("latency_seconds", request), ("apply_wall_seconds", application)):
                value = seconds(decision.get(field))
                if value is not None:
                    values.append(value)
            for name, value in report.get("stage_seconds", {}).items():
                elapsed = seconds(value)
                if elapsed is not None:
                    stage_values[name].append(elapsed)
        summary.append(
            {
                "group_hash": key,
                **group["identity"],
                "runs": len(group["runs"]),
                "cases": group["runs"],
                "run_statuses": dict(group["statuses"]),
                "decisions": n,
                "accepted": accepted,
                "pending_at_end": sum(d["reason"] == "PENDING_AT_END" for d in records),
                "solver_statuses": dict(statuses),
                "fallback_reasons": dict(group["fallbacks"]),
                "timing_available": bool(n),
                "timing_finalized_decisions": complete,
                "cold_worker_decisions": cold,
                "last_stages": dict(last),
                "soft_budget_observed": known_budget,
                "soft_budget_exceeded": exceeded,
                "parent_seconds": describe(parent, n),
                "request_sim_delay_seconds": describe(request, n),
                "application_wall_seconds": describe(application, n),
                "stages": {name: describe(values, n) for name, values in sorted(stage_values.items())},
            }
        )
    planned = manifest["planned_runs"]
    if type(planned) is not int or planned < len(seen):
        raise ValueError("invalid planned timing run count")
    return {
        "schema": "clover-timing-analysis-v1",
        "suite_status": manifest["status"],
        "source_hash": manifest["source"]["source_hash"],
        "planned_runs": planned,
        "recorded_rows": len(seen),
        "loaded_runs": sum(g["runs"] for g in summary),
        "not_started_budget": skipped,
        "not_yet_recorded": planned - len(seen),
        "groups": summary,
        "input_manifest_hash": fingerprint(json.loads(raw)),
        "input_result_hashes": digests,
        "scope": (
            "descriptive decision timings only; unrecorded includes unstarted/unknown, not zero; "
            "no CI or recommendation"
        ),
    }
