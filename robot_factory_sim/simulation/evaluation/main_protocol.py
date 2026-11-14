"""Prepare an executable confirmatory synthetic study without opening its held-out outcomes."""

from __future__ import annotations

import copy
import platform
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.artifacts import archive_run, events_for_result, stored_path
from simulation.decisions.contracts import DEADLINE_PROTOCOL, DecisionSettings
from simulation.evaluation.calibration import calibrate
from simulation.evaluation.comparison_inputs import file_sha256, read_json
from simulation.evaluation.main_inputs import (
    CONDITIONS,
    CORE,
    CORE_POLICIES,
    NATIVE_FAMILIES,
    demand_case,
    layout,
    seed_for,
    window,
)
from simulation.evaluation.reference_capacity import predict_reference, verify_reference
from simulation.evaluation.runner import replay, run_case, source_manifest
from simulation.evaluation.scenario import Scenario, fingerprint
from simulation.evaluation.study import code_identity, freeze_study, load_frozen, save, validate_spec
from simulation.planning.contracts import Settings

CORE_LAYOUTS, CORE_REPEATS = 6, 10
AUX_LAYOUTS, AUX_REPEATS = 3, 3
DEV_REPEATS = 3
ANCHORS = {family: next(c for c in CONDITIONS if c[1] == family) for family in (f"S{i:02d}" for i in range(12))}


def hardware_id() -> str:
    """Lock one host without publishing its hostname."""
    return "host-" + fingerprint([platform.node(), platform.machine(), platform.system()])[:20]


def _settings() -> dict[str, Any]:
    return {"schema": "clover-study-spec-v1", "stage": "confirmatory",
            "provenance": {"kind": "synthetic_research", "reviewed_by": "user authorized synthetic preparation",
                "description": "independent monotone grid-gap draws within twelve prespecified synthetic topologies"},
            "decision_settings": DecisionSettings(timing_protocol=DEADLINE_PROTOCOL,
                primary_deadline_policy="reject_late_primary", admission_reserve_seconds=.1,
                record_planning_inputs=True, end_to_end_soft_deadline_seconds=2,
                publication_allowance_seconds=.2).to_dict(),
            "planning_settings": asdict(Settings(horizon=40, step=2, max_tasks=8, sigma=0, timeout=2,
                                                  watchdog=3, native_actions=4)),
            "selection": {"method": "a_priori", "heldout_used": False,
                "rationale": "Prespecified H40/step2/candidates8/nominal solver2 reference; no optimality claim. "
                             "Reference-load calibration uses D0 optimized development outcomes, not solver tuning.",
                "calibration_reports": []},
            "analysis": {"control": "optimized", "candidates": ["baseline", "rh_milp_grid", "rh_cpsat_linear",
                                                                   "rh_cpsat_native"],
                "metrics": ["throughput_per_min", "late_due_rate", "urgent_late_due_rate",
                            "energy_per_completed_relative_change",
                            "energy_per_completed", "batch_makespan", "mean_wip", "unfinished",
                            "charge_queue_robot_seconds", "arrival_to_start_p95", "recorded_run_wall_seconds",
                            "dispatch_timely_acceptance_rate", "dispatch_deadline_exceeded_rate",
                            "dispatch_fallback_rate"],
                "primary_metrics": ["throughput_per_min", "urgent_late_due_rate"],
                "practical_difference": {"throughput_per_min": .1, "urgent_late_due_rate": .05},
                "effect_nulls": {"throughput_per_min": .1, "late_due_rate": .02,
                                 "energy_per_completed_relative_change": .10},
                "inferential_metrics": ["throughput_per_min", "late_due_rate", "energy_per_completed_relative_change"],
                "multiplicity_scope": "across_layouts_only",
                "condition_effect_nulls": {c: {"throughput_per_min": -.1, "urgent_late_due_rate": -.05}
                                           for c in ("S06_smooth", "S06_burst_2", "S06_burst_4")},
                "condition_inferential_metrics": {c: ["throughput_per_min", "urgent_late_due_rate", "late_due_rate",
                                                       "energy_per_completed_relative_change"]
                                                  for c in ("S06_smooth", "S06_burst_2", "S06_burst_4")},
                "condition_criteria": {"default": "throughput t/bootstrap lower >+0.1; SLA/energy noninferiority",
                    "S06": "urgent due-rate t/bootstrap upper <-0.05; throughput lower >-0.1; "
                           "overall SLA/energy noninferiority; use the registered margin-null Holm tests"},
                "secondary_metrics": ["batch_makespan", "measurement_energy_consumed", "dispatch_latency_p95",
                                      "dispatch_latency_p99"],
                "guardrails": {"invariants_required": True, "nominal_faults": 0, "nominal_rescue_energy": 0,
                    "coverage_required": True, "late_primary_accepted": 0,
                    "late_due_rate_noninferiority_margin": .02, "energy_per_completed_relative_margin": .10,
                    "missing_or_censored_guardrail": "withhold suitability recommendation"},
                "selection_rule": "Recommend a candidate only when the across-layout throughput CI lower bound "
                    "and bootstrap lower bounds exceed +0.1 tasks/min, margin-null Holm p<0.05, "
                    "SLA/relative-energy upper bounds below +0.02/+0.10 with margin-null Holm p<0.05, "
                    "and all registered safety guardrails hold. "
                    "For S06 use its registered urgent-SLA primary and throughput noninferiority instead. "
                    "Otherwise retain optimized or withhold; no winner from exploratory auxiliary/stress cells. "
                    "Pointwise intervals; Holm family includes every core across-layout primary/guardrail test. "
                    "No outcome-dependent extra seeds, exclusions or best-of-retries.",
                "confidence": .95, "bootstrap_resamples": 5000, "bootstrap_seed": 20261114,
                "minimum_seed_pairs": 10, "minimum_layouts": 3, "layout_sampling_independent": True,
                "hardware_id": hardware_id()},
            "execution": {"hardware_id": hardware_id(), "run_wall_limit_seconds": 900,
                          "native_concurrency_per_host": 1, "minimum_free_disk_bytes": 2_000_000_000,
                          "archive_artifacts": True},
            "cases": []}


def build_main_spec(capacity: dict[str, Any], capacity_path: Path, *,
                    core_layouts: int = CORE_LAYOUTS) -> dict[str, Any]:
    """Generate all inputs before any main outcome and explicitly register the nested sample matrix."""
    if core_layouts not in {3, 6}:
        raise ValueError("prespecified core layout count must be 3 or 6")
    spec = _settings()
    spec["selection"]["capacity_receipt"] = str(capacity_path.resolve())
    spec["selection"]["capacity_sha256"] = file_sha256(capacity_path)
    spec["main_study_design"] = {"version": "synthetic-main-v1", "core_layouts": core_layouts,
        "core_demand_repeats_per_layout": CORE_REPEATS, "aux_layouts": AUX_LAYOUTS,
        "aux_demand_repeats_per_layout": AUX_REPEATS, "development_repeats": DEV_REPEATS,
        "conditions": [c[0] for c in CONDITIONS],
        "layout_distribution": "independent_monotone_grid_gap_uniform_0.8_1.25_v1",
        "scope": "conditional synthetic generator only; sample size is fixed, not a guaranteed power claim"}
    for phase in ("training", "validation", "heldout", "stress"):
        configurations = list(ANCHORS.values()) if phase in {"training", "validation"} else list(CONDITIONS)
        if phase == "stress":
            configurations = [("S09_recovery", "S09", 1.3), ("S11_grid256", "S11", 1.3),
                              ("S04_long", "S04", .9)]
        for condition, family, multiplier in configurations:
            n_layout = 1 if phase in {"training", "validation"} else (
                core_layouts if phase == "heldout" and family in CORE else AUX_LAYOUTS)
            n_repeat = DEV_REPEATS if phase in {"training", "validation", "stress"} else (
                CORE_REPEATS if family in CORE else AUX_REPEATS)
            for draw in range(n_layout):
                anchor, provenance = layout(family, phase, draw, large=condition == "S11_grid256")
                predicted = predict_reference(anchor, family, capacity) if family != "S01" else None
                rate = multiplier * predicted["rate_tasks_per_second"] if predicted else .1
                for repeat in range(n_repeat):
                    seed = seed_for(family, phase, draw, repeat)
                    case = demand_case(anchor, family, condition, seed, rate)
                    if condition == "S04_long":
                        case.duration, case.warmup = 7800, 600
                        case.workload = {"kind": "poisson", "rate": rate, "fixed_station": True, "deadline_slack": 120}
                        case = demand_case(case, family, condition, seed, rate)
                    case.id = f"{phase}_{condition}_L{draw}_r{repeat}"
                    policies = list(CORE_POLICIES) + (["rh_cpsat_native"] if family in NATIVE_FAMILIES else [])
                    batch = family == "S01"
                    load = {"kind": "finite_batch" if batch else "stream",
                        "lambda_tasks_per_second": None if batch else rate,
                        "reference_capacity_tasks_per_second": None if batch else predicted["rate_tasks_per_second"],
                        "job_count": len(case.workload["jobs"]),
                        "calibration_status": "NOT_APPLICABLE" if batch else "CALIBRATED",
                        "capacity_evidence": {"receipt_sha256": spec["selection"]["capacity_sha256"],
                                              "family": family, "prediction": predicted},
                        "method": "batch has no lambda/rho" if batch else
                            "D0 optimized saturated finite-window reference, input-only free-flow transfer; "
                            "not physical maximum or a policy-specific rescaling"}
                    spec["cases"].append({"id": case.id, "phase": phase, "condition": condition,
                        "family": family, "layout": f"{phase}_{condition}_L{draw}", "layout_draw": provenance,
                        "seed": seed, "stochastic": True, "demand_generation": {"version": "main-demand-v1",
                            "seed": seed, "rate_multiplier": multiplier, "distribution": "Poisson and UID-keyed noise",
                            "burst": "equal realized count compression" if "burst" in condition else None},
                        "scenario": case.to_dict(), "policies": policies, "load": load,
                        "window": window(case, family, stress=phase == "stress"),
                        "inferential": phase == "heldout" and family in CORE})
    phase_order = {p: i for i, p in enumerate(("training", "validation", "heldout", "stress"))}
    spec["cases"].sort(key=lambda row: (phase_order[row["phase"]],
        fingerprint(["main-block-order-v1", 20261114, row["id"]])))
    return spec


def verify_main_design(spec: dict[str, Any], cases: list[dict[str, Any]], root: Path) -> None:
    """Require numeric/evidence consistency and the entire preregistered replication matrix."""
    capacity_path = Path(spec["selection"]["capacity_receipt"])
    if file_sha256(capacity_path) != spec["selection"]["capacity_sha256"]:
        raise ValueError("main-study capacity receipt changed")
    capacity = verify_reference(capacity_path)
    from simulation.evaluation.study import _geometry

    heldout_geometry = {c["geometry_hash"] for c in cases if c["phase"] == "heldout"}
    heldout_seeds = {c["seed"] for c in cases if c["phase"] == "heldout"}
    for family in capacity["families"].values():
        for probe in family["probes"]:
            report, _ = read_json(Path(probe["report"]))
            if probe["seed"] in heldout_seeds or _geometry(Scenario.from_dict(report["scenario"])) in heldout_geometry:
                raise ValueError("held-out input leaked into capacity calibration")
    expected = build_main_spec(capacity, capacity_path, core_layouts=spec["main_study_design"]["core_layouts"])
    expected["execution"].update(readiness_path=str((root / "readiness.json").resolve()), require_readiness=True)
    for key in ("planning_settings", "decision_settings", "analysis", "execution", "main_study_design"):
        # JSON restores tuples (restart backoffs) as lists; compare their saved values.
        if fingerprint(spec[key]) != fingerprint(expected[key]):
            raise ValueError("main-study registration differs: " + key)
    # Rebuild all geometry, demand and load values rather than trusting CALIBRATED labels.
    if spec["cases"] != expected["cases"]:
        raise ValueError("main-study matrix/inputs/load differs from registered generator")
    if len(cases) != len(expected["cases"]):
        raise ValueError("main-study case count mismatch")


def prepare_main(capacity_path: Path, output: Path, *, core_layouts: int = CORE_LAYOUTS,
                 development_from: Path | None = None) -> dict[str, Any]:
    """Audit development windows, seal the main matrix, and write immutable shard commands."""
    if output.exists():
        raise ValueError("main preparation output already exists")
    if not code_identity()["python_matches_head"]:
        raise ValueError("commit Python before main preparation/calibration")
    capacity = verify_reference(capacity_path)
    spec = build_main_spec(capacity, capacity_path, core_layouts=core_layouts)
    spec["execution"].update(readiness_path=str((output / "readiness.json").resolve()), require_readiness=True)
    cases = validate_spec(spec)
    output.mkdir(parents=True)
    state = {"schema": "clover-main-preparation-v1", "status": "RUNNING", "cases": len(cases),
             "capacity": str(capacity_path.resolve()), "source": source_manifest(), "development": []}
    save(output / "preparation.json", state)
    previous = {}
    if development_from is not None:
        old, _ = read_json(development_from / "preparation.json")
        if old["source"] != state["source"] or old["status"] != "PREPARED":
            raise ValueError("development reuse requires verified same-source preparation")
        previous = {r["case"]: r for r in old["development"]}
    for row in cases:
        if row["phase"] not in {"training", "validation"}:
            continue
        case = Scenario.from_dict(row["scenario"])
        if row["id"] in previous:
            prior = previous[row["id"]]
            report, sha = read_json(Path(prior["report"]))
            if (sha != prior["sha256"] or report["scenario_hash"] != row["scenario_hash"]
                    or report["trace_hash"] != row["trace_hash"] or report["window"] != row["window"]):
                raise ValueError("reused development input/window differs")
            spec["selection"]["calibration_reports"].append(prior["report"])
            state["development"].append(prior)
            save(output / "preparation.json", state)
            continue
        folder = output / "development" / row["id"]
        result = run_case(case, "optimized", row["seed"], output=folder, wall_limit=180)
        run_path = folder / f"{result['case_id']}.json"
        run_path, _ = archive_run(run_path)
        report = calibrate(run_path, row["window"])
        report_path = folder / "calibration.json"
        save(report_path, report)
        spec["selection"]["calibration_reports"].append(str(report_path.resolve()))
        state["development"].append({"case": row["id"], "status": report["status"],
                                     "report": str(report_path.resolve()),
                                     "sha256": file_sha256(report_path), "reasons": report["reasons"]})
        save(output / "preparation.json", state)
    if any(r["status"] != "PASS" for r in state["development"]):
        state["status"] = "DEVELOPMENT_WINDOW_FAILED"
        save(output / "preparation.json", state)
        return state
    save(output / "spec.json", spec)
    frozen = freeze_study(output / "spec.json", output / "frozen.json")
    counts = {phase: {"blocks": sum(c["phase"] == phase for c in cases),
                      "arms": sum(len(c["policies"]) for c in cases if c["phase"] == phase)}
              for phase in ("training", "validation", "heldout", "stress")}
    jobs = []
    for phase in ("heldout", "stress"):
        n = counts[phase]["blocks"]
        for index in range(n):
            jobs.append({"phase": phase, "shard_index": index, "shard_count": n,
                         "output": str((output / "runs" / phase / f"shard_{index:04d}").resolve())})
    save(output / "jobs.json", {"frozen": str((output / "frozen.json").resolve()),
                               "study_hash": frozen["study_hash"], "jobs": jobs})
    canary = copy.deepcopy(spec)
    canary.update(stage="pilot", main_study_design=None, execution={"archive_artifacts": True})
    canary["selection"]["calibration_reports"] = []
    canary["cases"] = []
    for family in ("S01", "S02", "S04", "S08", "S11"):
        row = copy.deepcopy(next(c for c in cases if c["phase"] == "training" and c["family"] == family))
        row["id"] = "canary_" + family
        case = Scenario.from_dict(row["scenario"])
        if family != "S04":
            case.duration, case.warmup = 300, 0
            case.workload["jobs"] = [j for j in case.workload["jobs"] if j["at"] < 300]
        case.id = row["id"]
        row["scenario"], row["window"] = case.to_dict(), window(case, family)
        row["load"]["job_count"] = len(case.workload["jobs"])
        row["demand_generation"]["scope"] = "development canary, not a main outcome"
        row["window"].update(minimum_completed_charge_sessions_per_robot=0, nominal=False)
        row["inferential"] = False
        canary["cases"].append(row)
    anchor, draw = layout("S11", "training", 90, large=True)
    prediction = predict_reference(anchor, "S11", capacity)
    seed = seed_for("S11", "training", 90, 9)
    case = demand_case(anchor, "S11", "S11_grid256", seed, .9 * prediction["rate_tasks_per_second"])
    case.duration, case.warmup, case.id = 300, 0, "canary_S11_large"
    case.workload["jobs"] = [j for j in case.workload["jobs"] if j["at"] < 300]
    row = copy.deepcopy(next(c for c in canary["cases"] if c["family"] == "S11"))
    row.update(id=case.id, scenario=case.to_dict(), seed=seed, layout="canary_large_draw",
               layout_draw=draw, policies=["optimized", "rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_native"])
    row["demand_generation"]["seed"] = seed
    row["load"]["capacity_evidence"]["prediction"] = prediction
    row["load"].update(lambda_tasks_per_second=.9 * prediction["rate_tasks_per_second"],
                        reference_capacity_tasks_per_second=prediction["rate_tasks_per_second"],
                        job_count=len(case.workload["jobs"]))
    row["window"] = window(case, "S11", stress=True)
    canary["cases"].append(row)
    save(output / "canary_spec.json", canary)
    freeze_study(output / "canary_spec.json", output / "canary_frozen.json")
    state.update(status="PREPARED", matrix=counts, main_jobs=len(jobs), study_hash=frozen["study_hash"],
                 frozen_sha256=file_sha256(output / "frozen.json"), hardware_id=hardware_id())
    save(output / "preparation.json", state)
    return state


def readiness(bundle: Path, canary_manifest: Path, output: Path) -> dict[str, Any]:
    """Certify preparation from actual frozen inputs, audited reference data and D1 canary results."""
    from simulation.evaluation.comparison import compare_plan
    from simulation.evaluation.study import study_plan

    if output.exists():
        raise ValueError("readiness output already exists")
    if output.resolve() != (bundle / "readiness.json").resolve():
        raise ValueError("readiness must use the registered bundle/readiness.json path")
    prepared, _ = read_json(bundle / "preparation.json")
    frozen, _ = load_frozen(bundle / "frozen.json")
    if (prepared["status"] != "PREPARED" or frozen["source"] != source_manifest()
            or frozen["stage"] != "confirmatory" or prepared["study_hash"] != frozen["study_hash"]
            or prepared["frozen_sha256"] != file_sha256(bundle / "frozen.json")):
        raise ValueError("preparation/source not ready")
    spec, spec_sha = read_json(bundle / "spec.json")
    if spec_sha != frozen["spec_sha256"]:
        raise ValueError("main spec changed after freeze")
    verify_main_design(spec, validate_spec(spec), bundle)
    for row in prepared["development"]:
        calibration, sha = read_json(Path(row["report"]))
        if (sha != row["sha256"] or calibration["status"] != "PASS"
                or calibrate(Path(calibration["run"]), calibration["window"]) != calibration):
            raise ValueError("development calibration changed or incomplete")
    canary_parent, _ = read_json(canary_manifest)
    if canary_parent["frozen_sha256"] != file_sha256(bundle / "canary_frozen.json"):
        raise ValueError("canary belongs to a different preparation")
    canary_frozen, _ = load_frozen(bundle / "canary_frozen.json")
    if (canary_frozen["source"] != frozen["source"]
            or canary_frozen["planning_settings"] != frozen["planning_settings"]
            or canary_frozen["decision_settings"] != frozen["decision_settings"]):
        raise ValueError("canary code/settings differ from main registration")
    expected_arms = sum(len(c["policies"]) for c in canary_frozen["cases"])
    plan = study_plan(canary_manifest)
    save(bundle / "canary_analysis_plan.json", plan)
    report = compare_plan(bundle / "canary_analysis_plan.json")
    save(bundle / "canary_analysis.json", report)
    reasons, arms, wall, storage = [], [], [], []
    canary_rows = {c["scenario"]["id"]: c for c in canary_frozen["cases"]}
    archive_cost = {}
    for entry in canary_parent["entries"]:
        suite, _ = read_json(canary_manifest.parent / entry["suite"])
        archive_cost.update({r["case_id"]: r.get("archive_wall_seconds", 0) for r in suite["runs"]})
    if report["planned_arms"] != expected_arms or report["full_observation_arms"] != expected_arms:
        reasons.append("CANARY_MATRIX_INCOMPLETE")
    accepted_backends = set()
    for arm in report["arms"]:
        if not arm["full_observation"]:
            reasons.append("CANARY_PARTIAL_OR_FAILED")
        if arm["result"] is None:
            continue
        run, _ = read_json(Path(arm["result"]))
        replay_matches = replay(run, input_root=Path(arm["result"]).parent)["matches"]
        if not replay_matches:
            reasons.append("CANARY_REPLAY_MISMATCH")
        if any(r.get("primary_kind") == "rh_plan" and r["accepted"] for r in run["control"]["decisions"]):
            accepted_backends.add(run["policy"])
        if any(r["accepted"] and r["time"] is not None and r["time"] - r["triggered_at"] > 2 + 1e-9
               for r in run["control"]["decisions"]):
            reasons.append("LATE_PRIMARY_ACCEPTED")
        folder = Path(arm["result"]).parent
        size = Path(arm["result"]).stat().st_size + events_for_result(Path(arm["result"])).stat().st_size
        refs = run["control"]["planning_inputs"].values()
        size += sum(stored_path(folder / e["path"]).stat().st_size for e in refs if "path" in e)
        wall.append(run["wall_seconds"] * 2100 / run["scenario"]["duration"])
        storage.append(size * 2100 / run["scenario"]["duration"])
        arms.append({"case_id": run["case_id"], "policy": run["policy"], "status": run["status"],
                     "replay_matches": replay_matches,
                     "wall_seconds": run["wall_seconds"] + archive_cost.get(run["case_id"], 0),
                     "duration": run["scenario"]["duration"], "bytes": size, "offered": run["offered"],
                     "family": canary_rows[run["scenario"]["id"]]["family"],
                     "nodes": len(run["scenario"]["factory"]["nodes"]),
                     "robots": len(run["scenario"]["factory"]["robots"]),
                     "faults": run["faults"], "rescue_energy": run["energy"]["rescue"],
                     "result_sha256": file_sha256(Path(arm["result"]))})
    if accepted_backends != {"rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_native"}:
        reasons.append("RH_BACKEND_HAS_NO_ACCEPTED_CANARY_PLAN")
    jobs, _ = read_json(bundle / "jobs.json")
    started_jobs = sum((Path(j["output"]) / "manifest.json").exists() for j in jobs["jobs"])
    if started_jobs:
        reasons.append("MAIN_OUTCOMES_ALREADY_STARTED")
    free = shutil.disk_usage(bundle).free
    # Match graph/fleet/policy rather than charging every small factory the largest graph's cost.
    estimated_bytes = estimated_wall = 0.0
    for case in [c for c in frozen["cases"] if c["phase"] in {"heldout", "stress"}]:
        nodes = len(case["scenario"]["factory"]["nodes"])
        for policy in case["policies"]:
            samples = [a for a in arms if a["policy"] == policy and (a["nodes"] > 128) == (nodes > 128)]
            exact = [a for a in samples if a["family"] == case["family"]]
            samples = exact or sorted(samples, key=lambda a: abs(a["nodes"] - nodes))[:2]
            if not samples:
                reasons.append("UNPROFILED_POLICY_OR_GRAPH_CLASS")
                continue
            ratio = max(len(case["scenario"]["workload"]["jobs"]) / max(1, min(a["offered"] for a in samples)),
                        .25 * case["scenario"]["duration"] / min(a["duration"] for a in samples))
            ratio *= len(case["scenario"]["factory"]["robots"]) / min(a["robots"] for a in samples)
            estimated_bytes += max(a["bytes"] for a in samples) * ratio
            estimated_wall += max(a["wall_seconds"] for a in samples) * ratio
    estimate_bytes = int(estimated_bytes * 3)
    if free < 2_000_000_000 + estimate_bytes:
        reasons.append("INSUFFICIENT_DISK_FOR_PLANNING_ENVELOPE")
    result = {"schema": "clover-main-readiness-v1", "status": "READY" if not reasons else "NOT_READY",
              "reasons": sorted(set(reasons)), "source": frozen["source"], "frozen_code": frozen["code"],
              "study_hash": frozen["study_hash"], "frozen_sha256": file_sha256(bundle / "frozen.json"),
              "canary_manifest": str(canary_manifest.resolve()),
              "canary_manifest_sha256": file_sha256(canary_manifest),
              "jobs_sha256": file_sha256(bundle / "jobs.json"),
              "hardware_id": hardware_id(), "matrix": prepared["matrix"], "jobs": prepared["main_jobs"],
              "development_window_reports": len(prepared["development"]), "canary_arms": arms,
              "budget": {"native_concurrency": 1, "per_arm_wall_limit_seconds": 900,
                         "planning_wall_hours": estimated_wall * 2 / 3600,
                         "planning_storage_bytes": estimate_bytes, "free_disk_bytes": free,
                         "scope": "policy/family/graph/fleet and offered-job scaled canary with factors2/3; "
                                  "not a guaranteed time/space bound; reserve2GB and stop between shards"},
              "main_shards_started": started_jobs,
              "scope": "ready for the frozen synthetic study; no performance superiority or real-factory claim"}
    save(output, result)
    return result
