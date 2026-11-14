"""Paired inference, sampling boundaries, raw integrity and real CLI acceptance."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.stats import ttest_rel

from simulation.evaluation import comparison
from simulation.evaluation.__main__ import main
from simulation.evaluation.comparison import compare_plan, write_comparison
from simulation.evaluation.comparison_inputs import draft_plan
from simulation.evaluation.fixtures import examples
from simulation.evaluation.paired_statistics import holm_adjust, mean_inference
from simulation.evaluation.runner import run_suite
from simulation.evaluation.scenario import Demand


def save(path: Path, value: dict[str, Any]) -> Path:
    """Write an explicit test input, separate from the recorded raw evidence."""
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    return path


def test_paired_t_matches_reference_and_resamples_whole_differences() -> None:
    control = np.arange(12) * 1000
    target = control + np.array([-3, 4, 2, 7, -2, 8, 1, 6, 5, 2, -1, 4])
    result = mean_inference((target - control).tolist(), resamples=1000, seed=44)
    reference = ttest_rel(target, control)
    assert result["mean_difference"] == pytest.approx(float((target - control).mean()))
    assert result["p_value"] == pytest.approx(reference.pvalue)
    assert result["t_interval"] == pytest.approx(reference.confidence_interval())
    assert result == mean_inference((target - control).tolist(), resamples=1000, seed=44)
    assert result["bootstrap_percentile_interval"][0] < result["mean_difference"]
    assert result["bootstrap_percentile_interval"][1] > result["mean_difference"]


@pytest.mark.parametrize("values,reason", [([], "INSUFFICIENT_UNITS"), ([2], "INSUFFICIENT_UNITS"),
                                          ([0, 0, 0], "ZERO_OR_NUMERICALLY_ZERO_VARIANCE"),
                                          ([3, 3, 3], "ZERO_OR_NUMERICALLY_ZERO_VARIANCE")])
def test_no_false_precision_from_empty_single_or_constant_samples(values: list[float], reason: str) -> None:
    result = mean_inference(values, resamples=1000)
    assert result["withheld"] == reason and result["t_interval"] is result["p_value"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True])
def test_nonfinite_or_boolean_observations_are_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        mean_inference([1, value], resamples=1000)


def test_holm_keeps_missing_hypotheses_in_the_declared_family() -> None:
    assert holm_adjust([.01, .04, .03, None]) == pytest.approx([.04, .09, .09, None])
    assert holm_adjust([.02, None]) == [.04, None]
    assert holm_adjust([None, None]) == [None, None]
    assert holm_adjust([]) == []


def simulated_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, layouts: int = 1,
                      count: int = 10) -> tuple[dict[str, Any], dict[tuple[str, int, str], dict[str, Any]]]:
    """Supply independent arithmetic observations; raw loading is tested separately."""
    plan = {"schema": "clover-paired-analysis-plan-v1", "purpose": "training", "control": "optimized",
            "candidates": ["rh_milp_grid"], "metrics": ["throughput_per_min"], "confidence": .95,
            "bootstrap_resamples": 1000, "bootstrap_seed": 42, "minimum_seed_pairs": 10, "minimum_layouts": 3,
            "layout_sampling_independent": True, "hardware_id": "fixture-host", "blocks": []}
    data = {}
    for layout in range(layouts):
        for repeat in range(count + layout * 2):
            seed = layout * 100 + repeat
            suite = tmp_path / f"layout{layout}-repeat{repeat}.json"
            suite.write_text("suite", encoding="utf-8")
            plan["blocks"].append({"condition": "same-condition", "layout": f"layout{layout}",
                                   "replicate": str(repeat), "seed": seed, "stochastic": True,
                                   "demand_kind": "stream",
                                   "scenario": "case", "suite": str(suite)})
            for policy in ["optimized", "rh_milp_grid"]:
                Path(str(suite) + policy).write_text("result", encoding="utf-8")
                value = 20 + repeat + (layout * 2 + repeat % 3 + 1 if policy == "rh_milp_grid" else 0)
                run = {"offered": 40, "unfinished": 2, "rejected": 0, "cancelled": 0, "due_jobs": 0,
                       "censored_start_count": 0, "faults": 0, "arrival_to_start": {"p95": value, "count": 40},
                       "start_cohort_jobs": 40,
                       "due_cohort_hash": "same-obligation-cohort",
                       "measurement_energy": {"consumed": 10, "charged": 0, "rescue": 0},
                       "throughput_per_min": value, "late_due_rate": None, "energy_per_completed": None}
                data[(str(suite), seed, policy)] = {
                    "policy": policy, "status": "SUCCESS", "failure": None, "result": run,
                    "full_observation": True, "suite": str(suite),
                    "suite_sha256": hashlib.sha256(b"suite").hexdigest(),
                    "result_path": str(suite) + policy, "result_sha256": hashlib.sha256(b"result").hexdigest(),
                    "context": {"source": "frozen", "timing": "D0", "window": [0, 60]},
                    "settings": {"policy": policy}, "input_key": f"pair-{layout}-{repeat}",
                    "sampling_input_key": f"input-{layout}-{repeat}", "layout_hash": f"geometry-{layout}"}
    monkeypatch.setattr(comparison, "load_arm", lambda suite, scenario, seed, policy: copy.deepcopy(
        data[(str(suite), seed, policy)]))
    return plan, data


def analyze_fixture(tmp_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Run the public plan analysis over a saved, reviewable plan."""
    return compare_plan(save(tmp_path / "analysis-plan.json", plan))


def test_conditional_inference_uses_run_pairs_not_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch)
    report = analyze_fixture(tmp_path, plan)
    group = report["comparisons"][0]
    estimate = group["per_layout"][0]["estimate"]
    expected = [i % 3 + 1 for i in range(10)]
    assert report["planned_arms"] == 20 and estimate["n"] == 10
    assert estimate["mean_difference"] == pytest.approx(np.mean(expected))
    assert estimate["t_interval"] and not estimate["withheld"]
    assert "INSUFFICIENT_LAYOUTS" in group["across_layouts"]["estimate"]["withheld"]
    assert report["holm_family_size"] == 2
    assert estimate["holm_p_value"] == pytest.approx(min(1, estimate["p_value"] * 2))
    assert report["recommendation"] is None and report["heldout_validated"] is False


def test_layout_estimate_is_equal_weighted_clusters_not_pooled_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch, layouts=3)
    report = analyze_fixture(tmp_path, plan)
    group = report["comparisons"][0]
    means = [np.mean([layout * 2 + i % 3 + 1 for i in range(10 + layout * 2)]) for layout in range(3)]
    estimate = group["across_layouts"]["estimate"]
    assert estimate["n"] == 3 and not estimate["withheld"]
    assert estimate["mean_difference"] == pytest.approx(np.mean(means))
    assert sum(row["observed_pairs"] for row in group["per_layout"]) == 36
    assert report["holm_family_size"] == 4


@pytest.mark.parametrize("change,reason", [
    ("e0", "E0_DESCRIPTIVE_ONLY"), ("hardware", "HARDWARE_ID_NOT_REVIEWED"),
    ("deterministic", "DETERMINISTIC_OR_UNREVIEWED_INPUTS"), ("duplicate", "REPEATED_PHYSICAL_INPUT"),
    ("failure", "INCOMPLETE_PLANNED_PAIRS"), ("partial", "INCOMPLETE_PLANNED_PAIRS"),
    ("unstarted", "INCOMPLETE_PLANNED_PAIRS"), ("censored", "CENSORED_STARTS_OBSERVED_P95_ONLY"),
    ("terminal_without_start", "INCOMPLETE_START_COHORT_OBSERVED_P95_ONLY"),
    ("null_ratio", "INCOMPLETE_PLANNED_PAIRS"),
    ("due_cohort", "INCOMPLETE_PLANNED_PAIRS"),
])
def test_inference_gates_preserve_planned_denominators(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     change: str, reason: str) -> None:
    plan, data = simulated_records(tmp_path, monkeypatch)
    if change == "e0":
        plan["purpose"] = "e0"
    elif change == "hardware":
        plan["hardware_id"] = None
    elif change == "deterministic":
        for block in plan["blocks"]:
            block["stochastic"] = False
    elif change == "duplicate":
        for arm in data.values():
            arm["sampling_input_key"] = "same-physical-input-different-seed-name"
    elif change in {"failure", "partial"}:
        arm = next(a for a in data.values() if a["policy"] == "rh_milp_grid")
        arm["full_observation"] = False
        if change == "failure":
            arm.update(status="FAILED", failure="wall limit")
    elif change == "unstarted":
        plan["blocks"][0].update(suite=None, unstarted_status="NOT_RUN_BUDGET")
    elif change == "censored":
        plan["metrics"] = ["arrival_to_start_p95"]
        for arm in data.values():
            arm["result"]["censored_start_count"] = 2
    elif change == "terminal_without_start":
        plan["metrics"] = ["arrival_to_start_p95"]
        for arm in data.values():
            arm["result"]["arrival_to_start"]["count"] = 39
            arm["result"]["cancelled"] = 1
    elif change == "null_ratio":
        plan["metrics"] = ["energy_per_completed", "late_due_rate"]
    elif change == "due_cohort":
        plan["metrics"] = ["late_due_rate"]
        for arm in data.values():
            arm["result"].update(due_jobs=10, late_due_rate=.1)
        next(a for a in data.values() if a["policy"] == "rh_milp_grid")["result"]["due_cohort_hash"] = "other"
    report = analyze_fixture(tmp_path, plan)
    assert report["planned_blocks"] == 10 and report["planned_arms"] == 20
    estimate = report["comparisons"][0]["per_layout"][0]["estimate"]
    assert estimate["t_interval"] is None and reason in estimate["withheld"]
    if change == "failure":
        assert report["arm_statuses"] == {"SUCCESS": 19, "FAILED": 1}
    if change in {"failure", "partial", "unstarted"}:
        assert report["comparisons"][0]["per_layout"][0]["observed_pairs"] == 9
    if change == "null_ratio":
        assert estimate["mean_difference"] is None


@pytest.mark.parametrize("change,reason", [
    ("geometry", "REPEATED_FACTORY_UNDER_LAYOUT_LABELS"),
    ("seed", "SHARED_SEEDS_REQUIRE_CROSSED_CLUSTER_METHOD"),
    ("independence", "LAYOUT_SAMPLING_NOT_REVIEWED"),
])
def test_layout_generalization_requires_distinct_reviewed_clusters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, reason: str,
) -> None:
    plan, data = simulated_records(tmp_path, monkeypatch, layouts=3)
    if change == "geometry":
        for arm in data.values():
            arm["layout_hash"] = "same-geometry-with-three-labels"
    elif change == "seed":
        for block in plan["blocks"]:
            old_seed = block["seed"]
            block["seed"] %= 100
            for policy in ["optimized", "rh_milp_grid"]:
                data[(block["suite"], block["seed"], policy)] = data[(block["suite"], old_seed, policy)]
    else:
        plan["layout_sampling_independent"] = False
    estimate = analyze_fixture(tmp_path, plan)["comparisons"][0]["across_layouts"]["estimate"]
    assert reason in estimate["withheld"] and estimate["t_interval"] is None


@pytest.mark.parametrize("field", ["context", "settings", "layout_hash"])
def test_incompatible_replicates_cannot_be_pooled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    plan, data = simulated_records(tmp_path, monkeypatch)
    last_suite = plan["blocks"][-1]["suite"]
    for (suite, _, _), arm in data.items():
        if suite == last_suite:
            arm[field] = "different" if field == "layout_hash" else {"different": True}
    with pytest.raises(ValueError, match="mixed|different factory"):
        analyze_fixture(tmp_path, plan)


def test_wrong_input_pair_is_visible_and_never_given_a_difference(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    plan, data = simulated_records(tmp_path, monkeypatch)
    next(a for a in data.values() if a["policy"] == "rh_milp_grid")["input_key"] = "other-demand"
    report = analyze_fixture(tmp_path, plan)
    pair = report["comparisons"][0]["pairs"][0]
    assert pair["difference"] is None and "PAIRED_INPUT_MISMATCH" in pair["issues"]


def test_declaring_the_same_run_twice_does_not_create_a_new_replicate(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch)
    renamed = {**plan["blocks"][0], "replicate": "renamed"}
    plan["blocks"].append(renamed)
    with pytest.raises(ValueError, match="same actual suite arm"):
        analyze_fixture(tmp_path, plan)


@pytest.fixture
def real_suite(tmp_path: Path) -> tuple[Path, Path]:
    """Run the real plant with common Poisson input for each policy/seed pair."""
    case = examples()[0]
    case.duration = 40
    case.workload = {"kind": "poisson", "rate": .3, "fixed_station": True}
    folder = tmp_path / "suite"
    suite = run_suite((case,), (11, 12), folder, policies=("baseline", "optimized"))
    assert suite["status"] == "SUCCESS"
    plan = draft_plan(folder / "manifest.json", control="baseline", candidates=("optimized",))
    return folder / "manifest.json", save(tmp_path / "plan.json", plan)


def test_real_suite_cli_writes_readable_effects_without_touching_evidence(real_suite: tuple[Path, Path],
                                                                      tmp_path: Path) -> None:
    manifest, plan = real_suite
    before = {p.name: p.read_bytes() for p in manifest.parent.iterdir()}
    output = tmp_path / "effects"
    assert main(["compare", "--plan", str(plan), "--output", str(output)]) == 0
    report = json.loads((output / "analysis.json").read_text())
    assert report["planned_arms"] == report["full_observation_arms"] == 4
    assert report["arm_statuses"] == {"SUCCESS": 4} and len(report["comparisons"]) == 8
    assert len(list(csv.DictReader((output / "effects.csv").open()))) == 16
    assert all(g["per_layout"][0]["estimate"]["t_interval"] is None for g in report["comparisons"])
    assert "pointwise" in (output / "README.md").read_text()
    assert before == {p.name: p.read_bytes() for p in manifest.parent.iterdir()}
    assert main(["compare", "--plan", str(plan), "--output", str(output)]) == 2


@pytest.mark.parametrize("mutation", ["trace", "derived", "raw", "duplicate", "escape"])
def test_corrupted_or_escaping_evidence_fails_before_output(real_suite: tuple[Path, Path], tmp_path: Path,
                                                         mutation: str) -> None:
    manifest_path, plan = real_suite
    suite = json.loads(manifest_path.read_text())
    row = suite["runs"][0]
    path = manifest_path.parent / row["result"]
    run = json.loads(path.read_text())
    if mutation == "trace":
        run["trace"][0]["noise"] += 1
    elif mutation == "derived":
        row["throughput_per_min"] = run["throughput_per_min"] = 999
    elif mutation == "raw":
        raw = manifest_path.parent / row["events"]
        raw.write_text("\n".join(raw.read_text().splitlines()[:-1]) + "\n")
    elif mutation == "duplicate":
        suite["runs"].append(copy.deepcopy(row))
    elif mutation == "escape":
        row["result"] = "../outside.json"
    save(path, run)
    save(manifest_path, suite)
    save(plan, draft_plan(manifest_path, control="baseline", candidates=("optimized",)))
    output = tmp_path / "must-not-be-created"
    assert main(["compare", "--plan", str(plan), "--output", str(output)]) == 2
    assert not output.exists()


def test_missing_arm_retains_the_planned_denominator(real_suite: tuple[Path, Path]) -> None:
    manifest_path, plan = real_suite
    suite = json.loads(manifest_path.read_text())
    suite["runs"].pop()
    save(manifest_path, suite)
    save(plan, draft_plan(manifest_path, control="baseline", candidates=("optimized",)))
    report = compare_plan(plan)
    assert report["planned_arms"] == 4 and report["arm_statuses"]["NOT_RECORDED"] == 1
    assert report["comparisons"][0]["per_layout"][0]["observed_pairs"] == 1


def test_not_run_budget_row_does_not_become_a_zero_observation(real_suite: tuple[Path, Path]) -> None:
    manifest_path, plan = real_suite
    suite = json.loads(manifest_path.read_text())
    row = suite["runs"][0]
    suite["runs"][0] = {k: row[k] for k in ("case_id", "scenario", "policy", "seed")}
    suite["runs"][0].update(status="NOT_RUN_BUDGET", failure="not started")
    save(manifest_path, suite)
    save(plan, draft_plan(manifest_path, control="baseline", candidates=("optimized",)))
    report = compare_plan(plan)
    assert report["arm_statuses"]["NOT_RUN_BUDGET"] == 1
    assert report["comparisons"][0]["per_layout"][0]["observed_pairs"] == 1


def test_plan_cli_and_inference_scope_are_explicit(real_suite: tuple[Path, Path], tmp_path: Path) -> None:
    manifest, _ = real_suite
    target = tmp_path / "draft.json"
    assert main(["comparison-plan", "--manifest", str(manifest), "--control", "baseline",
                 "--candidates", "optimized", "--output", str(target)]) == 0
    plan = json.loads(target.read_text())
    assert plan["purpose"] == "e0" and all(not b["stochastic"] for b in plan["blocks"])
    plan["purpose"] = "e2"
    with pytest.raises(ValueError, match="held-out"):
        compare_plan(save(target, plan))


def test_summary_writer_preserves_existing_results(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch)
    report = analyze_fixture(tmp_path, plan)
    target = tmp_path / "saved"
    write_comparison(report, target)
    original = (target / "analysis.json").read_bytes()
    with pytest.raises(FileExistsError):
        write_comparison(report, target)
    assert (target / "analysis.json").read_bytes() == original


def test_parent_checkpoint_preserves_unidentified_planned_slots(real_suite: tuple[Path, Path], tmp_path: Path) -> None:
    suite, _ = real_suite
    parent_path = save(tmp_path / "parent.json", {
        "schema": "clover-prepared-runs-v1", "status": "RUNNING", "layout_id": "layout-A", "planned_runs": 4,
        "entries": [{"cell": "S00_anchor", "family": "S00", "scenario": "open", "run_seed": 11,
                     "demand_seed": 11, "policies": ["baseline", "optimized"], "suite": "suite/manifest.json",
                     "status": "SUCCESS"}]})
    plan = draft_plan(parent_path, control="baseline", candidates=("optimized",))
    plan["purpose"], plan["hardware_id"] = "training", "reviewed-host"
    report = compare_plan(save(tmp_path / "parent-plan.json", plan))
    assert report["planned_arms"] == 2
    assert report["unidentified_parent_arm_slots_all_policies"] == 2
    estimate = report["comparisons"][0]["per_layout"][0]["estimate"]
    assert "INCOMPLETE_PARENT_MATRIX" in estimate["withheld"]
    parent = json.loads(parent_path.read_text())
    parent["status"] = "SUCCESS"
    save(parent_path, parent)
    with pytest.raises(ValueError, match="draft source manifest changed"):
        compare_plan(tmp_path / "parent-plan.json")


def test_analyzer_source_drift_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch)
    hashes = iter([{"source_hash": "before"}, {"source_hash": "after"}])
    monkeypatch.setattr(comparison, "source_manifest", lambda: next(hashes))
    with pytest.raises(ValueError, match="analyzer source changed"):
        analyze_fixture(tmp_path, plan)


def test_evidence_changes_during_analysis_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, data = simulated_records(tmp_path, monkeypatch)
    last_suite = plan["blocks"][-1]["suite"]

    def load(suite: Path, scenario: str, seed: int, policy: str) -> dict[str, Any]:
        if str(suite) == last_suite:
            Path(plan["blocks"][0]["suite"]).write_text("changed during analysis")
        return copy.deepcopy(data[(str(suite), seed, policy)])

    monkeypatch.setattr(comparison, "load_arm", load)
    with pytest.raises(ValueError, match="evidence changed during analysis"):
        analyze_fixture(tmp_path, plan)


@pytest.mark.parametrize("duration,complete", [(60, True), (1, False)])
def test_makespan_requires_whole_declared_batch(tmp_path: Path, duration: float, complete: bool) -> None:
    case = examples()[0]
    case.duration = duration
    case.factory.stations[0].process_time_std = 0
    case.workload = {"kind": "trace", "jobs": [
        asdict(Demand(f"j:{i}", 0, "S0", ("S0",), "inspection")) for i in range(3)]}
    folder = tmp_path / "batch"
    assert run_suite((case,), (11,), folder, policies=("baseline", "optimized"))["status"] == "SUCCESS"
    plan = draft_plan(folder / "manifest.json", control="baseline", candidates=("optimized",))
    plan["metrics"] = ["batch_makespan"]
    plan["blocks"][0]["demand_kind"] = "finite_batch"
    result = compare_plan(save(tmp_path / "batch-plan.json", plan))
    pair = result["comparisons"][0]["pairs"][0]
    if complete:
        assert pair["control"] > 0 and pair["candidate"] > 0
    else:
        assert pair["control"] is pair["candidate"] is pair["difference"] is None
    plan["blocks"][0]["demand_kind"] = "stream"
    result = compare_plan(save(tmp_path / "stream-plan.json", plan))
    assert result["comparisons"][0]["pairs"][0]["difference"] is None


@pytest.mark.parametrize("field,value", [("minimum_seed_pairs", 3), ("minimum_layouts", 1),
                                         ("layout_sampling_independent", "yes"), ("confidence", 1),
                                         ("bootstrap_resamples", 10), ("hardware_id", "")])
def test_safety_gates_cannot_be_lowered_by_editing_the_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         field: str, value: Any) -> None:
    plan, _ = simulated_records(tmp_path, monkeypatch)
    plan[field] = value
    with pytest.raises(ValueError):
        analyze_fixture(tmp_path, plan)
