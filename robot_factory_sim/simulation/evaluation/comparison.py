"""Failure-visible paired analyses with explicit sampling and inference gates."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from simulation.evaluation.comparison_inputs import PLAN_SCHEMA, file_sha256, load_arm, read_json
from simulation.evaluation.paired_statistics import holm_adjust, mean_inference
from simulation.evaluation.runner import environment_manifest, source_manifest
from simulation.evaluation.scenario import POLICIES, fingerprint, integer, number

METRICS = {
    "throughput_per_min": ("tasks/min", "higher"),
    "late_due_rate": ("fraction of due jobs", "lower"),
    "urgent_late_due_rate": ("late fraction of input priority0 jobs due inside the window", "lower"),
    "energy_per_completed": ("battery-unit/completion in measurement window", "lower"),
    "energy_per_completed_relative_change": ("ratio to matched control energy/completion minus one", "lower"),
    "measurement_energy_consumed": ("battery-unit in measurement window", "lower"),
    "mean_wip": ("tasks", "lower"),
    "unfinished": ("tasks at observation end", "lower"),
    "charge_queue_robot_seconds": ("robot-second including ongoing queue tails", "lower"),
    "arrival_to_start_p95": ("sim-second; observed starts only", "lower"),
    "recorded_run_wall_seconds": ("runner wall-second; excludes preflight, final disposal and storage", "lower"),
    "batch_makespan": ("sim-second from first release to last completion; whole declared batch", "lower"),
    "dispatch_timely_acceptance_rate": ("timely primary accepts / deadline-observable dispatch requests", "higher"),
    "dispatch_deadline_exceeded_rate": ("late or pending cycles / deadline-observable dispatch requests", "lower"),
    "dispatch_fallback_rate": ("fallback / fully observed eligible dispatch requests", "lower"),
    "dispatch_latency_p95": ("sim-second; finalized deadline-observable dispatch requests", "lower"),
    "dispatch_latency_p99": ("sim-second; finalized deadline-observable dispatch requests", "lower"),
}


def validate_plan(plan: dict[str, Any]) -> None:
    """Validate exploratory scope and declared sampling units before reading runs."""
    if plan.get("schema") != PLAN_SCHEMA or plan.get("purpose") not in {
        "e0", "training", "validation", "heldout", "stress"
    }:
        raise ValueError("expected E0/training or registered validation/held-out/stress paired analysis plan")
    if plan.get("registered_study") or plan["purpose"] in {"validation", "heldout", "stress"}:
        from simulation.evaluation.study import verify_registered_plan

        verify_registered_plan(plan)
    control, candidates, metrics = plan["control"], plan["candidates"], plan["metrics"]
    if (control not in POLICIES or not candidates or len(set(candidates)) != len(candidates)
            or control in candidates or set(candidates) - set(POLICIES)):
        raise ValueError("invalid comparison policies")
    if not metrics or len(set(metrics)) != len(metrics) or set(metrics) - set(METRICS):
        raise ValueError("unknown or duplicate comparison metrics")
    if set(plan.get("effect_nulls", {})) - set(metrics):
        raise ValueError("effect nulls must refer to declared metrics")
    for value in plan.get("effect_nulls", {}).values():
        number(value, "effect null", -float("inf"))
    if plan.get("multiplicity_scope", "all_estimates") not in {"all_estimates", "across_layouts_only"}:
        raise ValueError("unknown multiplicity scope")
    if set(plan.get("inferential_metrics", metrics)) - set(metrics):
        raise ValueError("inferential metrics must be declared measurements")
    for overrides in plan.get("condition_effect_nulls", {}).values():
        if set(overrides) - set(metrics):
            raise ValueError("condition effect null refers to an undeclared metric")
        for value in overrides.values():
            number(value, "condition null", -float("inf"))
    if any(set(values) - set(metrics) for values in plan.get("condition_inferential_metrics", {}).values()):
        raise ValueError("condition inferential metric is undeclared")
    number(plan["confidence"], "confidence", .5, .999)
    integer(plan["bootstrap_resamples"], "bootstrap resamples", 1000, 50000)
    integer(plan["bootstrap_seed"], "bootstrap seed", 0, 2**32 - 1)
    integer(plan["minimum_seed_pairs"], "minimum seed pairs", 10, 100000)
    integer(plan["minimum_layouts"], "minimum layouts", 3, 10000)
    if type(plan["layout_sampling_independent"]) is not bool:
        raise ValueError("layout independence must be explicitly declared")
    if plan["hardware_id"] is not None and (not isinstance(plan["hardware_id"], str) or not plan["hardware_id"]):
        raise ValueError("hardware_id must be a reviewed identifier or null")
    if not plan["blocks"]:
        raise ValueError("comparison plan has no declared blocks")
    seen = set()
    for block in plan["blocks"]:
        if type(block.get("inferential", True)) is not bool:
            raise ValueError("inference eligibility must be explicit boolean")
        selected = block.get("candidates", candidates)
        if not selected or len(set(selected)) != len(selected) or set(selected) - set(candidates):
            raise ValueError("invalid block-specific candidates")
        for name in ("condition", "layout", "replicate", "scenario"):
            if not isinstance(block[name], str) or not block[name]:
                raise ValueError("missing analysis block label: " + name)
        key = (block["condition"], block["layout"], block["replicate"])
        if key in seen:
            raise ValueError("duplicate declared comparison block")
        seen.add(key)
        integer(block["seed"], "block seed", 0, 2**32 - 1)
        if type(block["stochastic"]) is not bool:
            raise ValueError("sampling kind must be explicitly declared")
        if block["demand_kind"] not in {"stream", "finite_batch", "unreviewed"}:
            raise ValueError("review demand kind before analyzing batch completion time")
        if block["suite"] is not None and (not isinstance(block["suite"], str) or not block["suite"]):
            raise ValueError("invalid suite reference")
        if block["suite"] is None and not block.get("unstarted_status"):
            raise ValueError("unstarted block requires its retained status")


def metric_value(arm: dict[str, Any], metric: str, demand_kind: str) -> float | None:
    """Extract a per-run measurement; undefined ratios never become zero."""
    run = arm["result"]
    if run is None or not arm.get("full_observation"):
        return None
    if metric == "batch_makespan" and demand_kind != "finite_batch":
        return None
    if metric == "energy_per_completed_relative_change":
        return run["energy_per_completed"]
    if metric.startswith("dispatch_"):
        summary = run.get("control_summary", {})
        if not summary.get("available"):
            return None
        cohort = summary["by_kind"]["dispatch"]
        if metric in {"dispatch_latency_p95", "dispatch_latency_p99"}:
            return cohort["latency"][metric.removeprefix("dispatch_latency_")] if cohort["pending"] == 0 else None
        return cohort[metric.removeprefix("dispatch_")]
    if metric == "arrival_to_start_p95":
        value = run["arrival_to_start"]["p95"]
    elif metric == "charge_queue_robot_seconds":
        value = run["resource_waits"]["by_channel"].get("charge", {}).get("queue_robot_seconds", 0)
    elif metric == "recorded_run_wall_seconds":
        value = run["wall_seconds"]
    elif metric == "measurement_energy_consumed":
        value = run["measurement_energy"]["consumed"] if run["measurement_energy"] else None
    else:
        value = run[metric]
    if value is None:
        return None
    return number(value, metric)


def paired_metric_values(control: dict[str, Any], target: dict[str, Any], metric: str,
                         demand_kind: str) -> tuple[float | None, float | None]:
    """Retain paired ratio denominators and leave zero/undefined control energy unknown."""
    c, t = (metric_value(a, metric, demand_kind) for a in (control, target))
    if metric == "energy_per_completed_relative_change":
        return (1.0, t / c) if c is not None and c > 0 and t is not None else (None, None)
    return c, t


def _inference(values: list[float], reasons: list[str], plan: dict[str, Any], label: str,
               null_difference: float = 0) -> dict[str, Any]:
    seed = int(fingerprint([plan["bootstrap_seed"], label])[:8], 16)
    if reasons:
        return {"n": len(values), "mean_difference": float(np.mean(values)) if values else None,
                "t_interval": None, "p_value": None, "bootstrap_percentile_interval": None,
                "withheld": sorted(set(reasons)), "bootstrap_seed": seed, "null_difference": null_difference,
                "holm_p_value": None}
    result = mean_inference(values, confidence=plan["confidence"],
                            resamples=plan["bootstrap_resamples"], seed=seed, null_difference=null_difference)
    result["withheld"] = [result["withheld"]] if result["withheld"] else []
    result["holm_p_value"] = None
    return result


def compare_plan(path: Path) -> dict[str, Any]:
    """Analyze saved runs without solving, replaying or changing their evidence.

    Failures/missing arms retain planned denominators. Complete-case effects
    are labeled descriptive; any missing pair withholds inference. Intervals
    are exploratory and pointwise, with Holm adjustment of the declared family.
    """
    path = path.resolve()
    plan, plan_sha = read_json(path)
    validate_plan(plan)
    analysis_source = source_manifest()
    cache, files, contexts_registry, groups, arm_rows = {}, {}, {}, defaultdict(list), []
    parent_unknown = 0
    if plan.get("draft_from"):
        origin = plan["draft_from"]
        origin_path = (path.parent / origin["manifest"]).resolve()
        if file_sha256(origin_path) != origin["sha256"]:
            raise ValueError("draft source manifest changed; draft a new plan")
        files[str(origin_path)] = origin["sha256"]
        parent_unknown = origin["unidentified_parent_arm_slots"]
        integer(parent_unknown, "unidentified parent arms")
    seen_actual = set()
    for block in plan["blocks"]:
        suite = ((path.parent / block["suite"]).resolve() if block["suite"] is not None else None)
        arms = {}
        for policy in [plan["control"], *block.get("candidates", plan["candidates"])]:
            if suite is None:
                arm = {"policy": policy, "status": block["unstarted_status"], "result": None,
                       "failure": "parent did not start this declared block"}
            else:
                key = (str(suite), block["scenario"], block["seed"], policy)
                if key not in cache:
                    cache[key] = load_arm(suite, block["scenario"], block["seed"], policy)
                arm = cache[key]
                for file_key, digest_key in (("suite", "suite_sha256"), ("result_path", "result_sha256"),
                                             ("events_path", "events_sha256")):
                    if file_key in arm:
                        file, digest = arm[file_key], arm[digest_key]
                        if file in files and files[file] != digest:
                            raise ValueError("evidence changed during analysis: " + file)
                        files[file] = digest
                if key in seen_actual:
                    raise ValueError("same actual suite arm declared twice under different labels")
                seen_actual.add(key)
            arms[policy] = arm
            run = arm["result"]
            arm_rows.append({"condition": block["condition"], "layout": block["layout"],
                             "replicate": block["replicate"], "seed": block["seed"], "policy": policy,
                             "status": arm["status"], "failure": arm["failure"],
                             "full_observation": arm.get("full_observation", False),
                             "recorded_context": arm.get("context"), "recorded_settings": arm.get("settings"),
                             "result_sha256": arm.get("result_sha256"), "result": arm.get("result_path"),
                             "offered": run["offered"] if run else None,
                             "unfinished": run["unfinished"] if run else None,
                             "rejected": run["rejected"] if run else None,
                             "cancelled": run["cancelled"] if run else None,
                             "due_jobs": run["due_jobs"] if run else None,
                             "due_cohort_hash": run["due_cohort_hash"] if run else None,
                             "urgent_due_jobs": run.get("urgent_due_jobs") if run else None,
                             "urgent_due_cohort_hash": run.get("urgent_due_cohort_hash") if run else None,
                             "start_cohort_jobs": run["start_cohort_jobs"] if run else None,
                             "observed_starts": run["arrival_to_start"]["count"] if run else None,
                             "censored_start_count": run["censored_start_count"] if run else None,
                             "faults": run["faults"] if run else None,
                             "measurement_energy": run["measurement_energy"] if run else None})
        control = arms[plan["control"]]
        for candidate in block.get("candidates", plan["candidates"]):
            target = arms[candidate]
            pair_issues = []
            if not control.get("full_observation") or not target.get("full_observation"):
                pair_issues.append("FAILED_MISSING_OR_PARTIAL_ARM")
            if control["result"] is not None and target["result"] is not None:
                if control["input_key"] != target["input_key"]:
                    pair_issues.append("PAIRED_INPUT_MISMATCH")
                if control["context"] != target["context"]:
                    pair_issues.append("PAIRED_CONTEXT_MISMATCH")
            context = None
            if control["result"] is not None and target["result"] is not None:
                identity = {"common": control["context"], "control_settings": control["settings"],
                            "candidate_settings": target["settings"], "demand_kind": block["demand_kind"]}
                context = fingerprint(identity)
                contexts_registry[context] = identity
            for metric in plan["metrics"]:
                c, t = paired_metric_values(control, target, metric, block["demand_kind"])
                reasons = list(pair_issues)
                if c is None or t is None:
                    reasons.append("UNDEFINED_METRIC")
                if (metric == "late_due_rate" and control["result"] and target["result"]
                        and control["result"]["due_cohort_hash"] != target["result"]["due_cohort_hash"]):
                    reasons.append("DUE_COHORT_MISMATCH")
                if (metric == "urgent_late_due_rate" and control["result"] and target["result"]
                        and control["result"]["urgent_due_cohort_hash"] != target["result"]["urgent_due_cohort_hash"]):
                    reasons.append("URGENT_DUE_COHORT_MISMATCH")
                censoring = (metric == "arrival_to_start_p95" and any(
                    a["result"] and a["result"]["censored_start_count"] for a in (control, target)))
                incomplete_starts = (metric == "arrival_to_start_p95" and any(
                    a["result"] and a["result"]["arrival_to_start"]["count"] < a["result"]["start_cohort_jobs"]
                    for a in (control, target)))
                groups[(block["condition"], candidate, metric)].append({
                    "inferential": block.get("inferential", True),
                    "layout": block["layout"], "replicate": block["replicate"], "seed": block["seed"],
                    "stochastic": block["stochastic"], "control": c, "candidate": t,
                    "difference": t - c if not reasons else None, "issues": reasons,
                    "censored_starts": bool(censoring), "incomplete_starts": bool(incomplete_starts),
                    "context": context,
                    "layout_hash": control.get("layout_hash"), "input_key": control.get("sampling_input_key")})
    output, hypotheses = [], []
    for (condition, candidate, metric), pairs in sorted(groups.items()):
        label = fingerprint([condition, candidate, metric])
        contexts = {p["context"] for p in pairs if p["context"] is not None}
        # Different settings/source/machines/windows must be separate plans or
        # declared conditions, never an accidental aggregate with a single CI.
        if len(contexts) > 1:
            raise ValueError("mixed source/environment/settings/window for condition: " + condition)
        by_layout = defaultdict(list)
        for p in pairs:
            by_layout[p["layout"]].append(p)
        layout_summaries, layout_values, layout_reasons = [], [], []
        observed_layout_hashes, seed_sets = [], []
        for layout, records in sorted(by_layout.items()):
            records = sorted(records, key=lambda p: (p["replicate"], p["seed"]))
            differences = [p["difference"] for p in records if p["difference"] is not None]
            reasons = []
            if not all(p.get("inferential", True) for p in records):
                reasons.append("PREREGISTERED_EXPLORATORY_CONDITION")
            if plan["purpose"] == "e0":
                reasons.append("E0_DESCRIPTIVE_ONLY")
            if parent_unknown:
                reasons.append("INCOMPLETE_PARENT_MATRIX")
            if plan["hardware_id"] is None:
                reasons.append("HARDWARE_ID_NOT_REVIEWED")
            if len(differences) != len(records):
                reasons.append("INCOMPLETE_PLANNED_PAIRS")
            if len(differences) < plan["minimum_seed_pairs"]:
                reasons.append("INSUFFICIENT_SEED_PAIRS")
            if not all(p["stochastic"] for p in records):
                reasons.append("DETERMINISTIC_OR_UNREVIEWED_INPUTS")
            hashes = [p["input_key"] for p in records if p["input_key"] is not None]
            if len(set(hashes)) < len(hashes):
                reasons.append("REPEATED_PHYSICAL_INPUT")
            if len({p["seed"] for p in records}) != len(records):
                reasons.append("REPEATED_DEMAND_SEED")
            if any(p["censored_starts"] for p in records):
                reasons.append("CENSORED_STARTS_OBSERVED_P95_ONLY")
            if any(p["incomplete_starts"] for p in records):
                reasons.append("INCOMPLETE_START_COHORT_OBSERVED_P95_ONLY")
            physical = {p["layout_hash"] for p in records if p["layout_hash"] is not None}
            if len(physical) > 1:
                raise ValueError("one layout label refers to different factory configurations")
            observed_layout_hashes.extend(physical)
            seed_sets.append({p["seed"] for p in records})
            null = plan.get("condition_effect_nulls", {}).get(condition, {}).get(
                metric, plan.get("effect_nulls", {}).get(metric, 0))
            estimate = _inference(differences, reasons, plan, label + layout, null)
            row = {"layout": layout, "planned_pairs": len(records), "observed_pairs": len(differences),
                   "distinct_inputs": len(set(hashes)), "control_mean": float(np.mean(
                       [p["control"] for p in records if p["difference"] is not None])) if differences else None,
                   "candidate_mean": float(np.mean(
                       [p["candidate"] for p in records if p["difference"] is not None])) if differences else None,
                   "estimate": estimate}
            if plan.get("multiplicity_scope", "all_estimates") == "all_estimates":
                hypotheses.append(estimate)
            else:
                estimate["p_value"] = None
                estimate["test_scope"] = "descriptive within-layout interval only"
            layout_summaries.append(row)
            if differences:
                layout_values.append(float(np.mean(differences)))
            layout_reasons.extend(reasons)
        if not plan["layout_sampling_independent"]:
            layout_reasons.append("LAYOUT_SAMPLING_NOT_REVIEWED")
        if len(layout_values) < plan["minimum_layouts"]:
            layout_reasons.append("INSUFFICIENT_LAYOUTS")
        if len(observed_layout_hashes) != len(set(observed_layout_hashes)):
            layout_reasons.append("REPEATED_FACTORY_UNDER_LAYOUT_LABELS")
        if any(a & b for i, a in enumerate(seed_sets) for b in seed_sets[i + 1:]):
            layout_reasons.append("SHARED_SEEDS_REQUIRE_CROSSED_CLUSTER_METHOD")
        inferential = (metric in plan.get("condition_inferential_metrics", {}).get(
            condition, plan.get("inferential_metrics", plan["metrics"]))
                       and all(p.get("inferential", True) for p in pairs))
        if not inferential:
            layout_reasons.append("PREREGISTERED_DESCRIPTIVE_METRIC_OR_CONDITION")
        aggregate = _inference(layout_values, layout_reasons, plan, label + ":layouts",
                               plan.get("condition_effect_nulls", {}).get(condition, {}).get(
                                   metric, plan.get("effect_nulls", {}).get(metric, 0)))
        if plan.get("multiplicity_scope", "all_estimates") == "all_estimates" or inferential:
            hypotheses.append(aggregate)
        output.append({"condition": condition, "candidate": candidate, "control": plan["control"],
                       "metric": metric, "unit": METRICS[metric][0], "preferred_direction": METRICS[metric][1],
                       "difference_definition": "candidate minus control", "pairs": pairs,
                       "per_layout": layout_summaries, "across_layouts": {"weighting": "equal_layout_means",
                                                                             "estimate": aggregate}})
    adjusted = holm_adjust([h["p_value"] for h in hypotheses])
    for hypothesis, p_adjusted in zip(hypotheses, adjusted):
        hypothesis["holm_p_value"] = p_adjusted
    for file, expected in {str(path): plan_sha, **files}.items():
        if file_sha256(Path(file)) != expected:
            raise ValueError("evidence changed during analysis: " + file)
    if source_manifest()["source_hash"] != analysis_source["source_hash"]:
        raise ValueError("analyzer source changed during analysis")
    policy_statuses = {}
    for policy in [plan["control"], *plan["candidates"]]:
        selected = [arm for arm in arm_rows if arm["policy"] == policy]
        statuses = dict(Counter(arm["status"] for arm in selected))
        policy_statuses[policy] = {"planned_arms": len(selected), "statuses": statuses,
                                   "full_observation_arms": sum(a["full_observation"] for a in selected),
                                   "non_success_or_unstarted_fraction":
                                   (len(selected) - statuses.get("SUCCESS", 0)) / len(selected) if selected else None}
    return {"schema": "clover-paired-analysis-v1", "purpose": plan["purpose"], "analysis_plan_sha256": plan_sha,
            "analysis_source": analysis_source, "analysis_environment": environment_manifest(),
            "analysis_plan": plan, "input_file_sha256": files, "planned_blocks": len(plan["blocks"]),
            "unidentified_parent_arm_slots_all_policies": parent_unknown,
            "planned_arms": len(arm_rows), "arm_statuses": dict(Counter(a["status"] for a in arm_rows)),
            "full_observation_arms": sum(a["full_observation"] for a in arm_rows),
            "policy_statuses": policy_statuses,
            "arms": arm_rows, "contexts": contexts_registry,
            "comparisons": output, "holm_family_size": len(hypotheses),
            "interval_scope": "pointwise intervals; Holm adjusts only the registered test family, not intervals",
            "layout_scope": "equal configured-layout weights; sampling declarations do not prove representativeness",
            "recommendation": None, "heldout_validated": False,
            "registered_phase_verified": bool(plan.get("registered_study")),
            "scope": "R5 exploratory infrastructure; no algorithm selection, equivalence or superiority verdict"}


def write_comparison(report: dict[str, Any], output: Path) -> None:
    """Write JSON, tidy effects CSV and interpretation notes to a new directory."""
    rows = []
    for group in report["comparisons"]:
        for layout in [*group["per_layout"], {"layout": "__across_layouts__", **group["across_layouts"]}]:
            estimate = layout["estimate"]
            ci, boot = estimate["t_interval"], estimate["bootstrap_percentile_interval"]
            rows.append({"condition": group["condition"], "layout": layout["layout"],
                         "candidate": group["candidate"], "control": group["control"], "metric": group["metric"],
                         "unit": group["unit"], "preferred_direction": group["preferred_direction"],
                         "planned_pairs": layout.get("planned_pairs"), "observed_pairs": layout.get("observed_pairs"),
                         "units_in_descriptive_mean": estimate["n"], "control_mean": layout.get("control_mean"),
                         "candidate_mean": layout.get("candidate_mean"),
                         "mean_difference": estimate["mean_difference"],
                         "test_null_difference": estimate.get("null_difference", 0),
                         "test_scope": estimate.get("test_scope", "registered aggregate"),
                         "t_ci_lower": ci[0] if ci else None, "t_ci_upper": ci[1] if ci else None,
                         "bootstrap_lower": boot[0] if boot else None, "bootstrap_upper": boot[1] if boot else None,
                         "p_value": estimate["p_value"], "holm_p_value": estimate["holm_p_value"],
                         "withheld": ";".join(estimate["withheld"])})
    output.mkdir(parents=True, exist_ok=False)
    with (output / "analysis.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    with (output / "effects.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    notes = (
        "# Paired exploratory analysis\n\n"
        f"Declared blocks: {report['planned_blocks']}; arms: {report['planned_arms']}; "
        f"full observation arms: {report['full_observation_arms']}.\n\n"
        f"Parent arm slots not yet identifiable (all policies): "
        f"{report['unidentified_parent_arm_slots_all_policies']}.\n\n"
        "Differences are candidate minus control. CSV blank values mean undefined/withheld, not zero. "
        "Means use only complete comparable pairs; all failed, missing, partial and undefined arms remain "
        "in analysis.json and planned denominators. Any incomplete planned pair withholds inference.\n\n"
        "Per-layout estimates use independent demand-seed pairs. Across-layout estimates weight each "
        "configured layout mean equally. Repeated factories, reused seeds across layouts, deterministic "
        "inputs, insufficient replication and unreviewed sampling withhold intervals. Layout/hardware "
        "labels are declarations, not proof of independence or representativeness.\n\n"
        "The t interval is a pointwise exploratory interval. Paired percentile bootstrap is a fixed-method "
        "sensitivity analysis, not an alternative chosen after looking at results. Holm adjusts p-values "
        "for the whole declared family, including unavailable slots; it does not adjust these intervals. "
        "The t approximation assumes independent unit differences and a suitable distribution of their mean. "
        "The 10-pair/3-layout gates do not establish normality, adequate power or independence. "
        "Observed-start P95 does not describe censored tasks; inference is withheld when starts are censored. "
        "An absent due-date denominator or zero completions keeps the respective ratio undefined.\n\n"
        "Batch makespan is available only for declared finite batches with every offered job completed "
        "inside the measurement window. It measures elapsed time from first release to last completion; "
        "partial batches and streams do not receive a completed-only makespan.\n\n"
        "Recorded run wall time uses the runner's existing execution timer; it excludes preflight, final "
        "disposal and storage. It is neither complete CLI cost nor operational decision latency. "
        "Energy/completion is window consumption divided by window completions, not a matched job-cohort "
        "energy estimate. Rescue, charging, faults and unfinished counts must be reviewed separately.\n\n"
        "E0 always withholds inference. No algorithm recommendation, equivalence or held-out validation "
        "is produced by this analysis. Analysis can read older source-frozen runs without rerunning solvers; "
        "original input/result/event digests and source/settings contexts remain recorded.\n\n"
        "Methods: [NIST paired intervals](https://itl.nist.gov/div898/handbook/prc/section3/prc312.htm), "
        "[SciPy paired tests](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.ttest_rel.html), "
        "[paired resampling](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html), "
        "[Holm adjustment](https://www.statsmodels.org/stable/generated/"
        "statsmodels.stats.multitest.multipletests.html).\n"
    )
    (output / "README.md").write_text(notes, encoding="utf-8", newline="\n")
