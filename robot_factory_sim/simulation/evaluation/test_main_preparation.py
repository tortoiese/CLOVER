"""Synthetic main-study replication, physical treatments, reference and shard invariants."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simulation.evaluation.comparison import compare_plan
from simulation.evaluation.main_inputs import (
    CONDITIONS,
    demand_case,
    free_flow_cycle,
    layout,
    seed_for,
)
from simulation.evaluation.main_protocol import build_main_spec
from simulation.evaluation.reference_capacity import probe_case, summarize_probe
from simulation.evaluation.scenario import fingerprint
from simulation.evaluation.study import freeze_study, run_study, save, study_plan, validate_spec
from simulation.evaluation.study_shards import collect_study
from simulation.evaluation.test_study import spec as small_spec


@pytest.fixture(scope="module")
def main_spec(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Build the full matrix with an explicitly fake rate reference, without executing it."""
    reference = {"families": {f"S{i:02d}": {"training_rate": .08, "training_cycle_seconds": 20,
                 "validation_ratios": [1, 1]} for i in range(12) if i != 1}}
    path = tmp_path_factory.mktemp("fake-capacity") / "reference.json"
    save(path, reference)
    return build_main_spec(reference, path)


def test_full_main_matrix_and_nested_seeds(main_spec: dict) -> None:
    cases = validate_spec(main_spec)
    heldout = [r for r in cases if r["phase"] == "heldout" and r["inferential"]]
    assert len(heldout) == 14 * 6 * 10
    assert sum(len(r["policies"]) for r in heldout) == 3660
    for condition, family, _ in CONDITIONS[:14]:
        rows = [r for r in heldout if r["condition"] == condition]
        assert len({r["layout"] for r in rows}) == 6
        assert len({r["seed"] for r in rows}) == 60
    assert not ({r["seed"] for r in cases if r["phase"] in {"training", "validation"}}
                & {r["seed"] for r in cases if r["phase"] == "heldout"})
    batches = [r for r in cases if r["family"] == "S01"]
    assert all(r["load"]["reference_rho"] is None and r["load"]["lambda_tasks_per_second"] is None for r in batches)
    assert all(len(r["scenario"]["workload"]["jobs"]) == 8 for r in batches)
    assert len({r["family"] for r in main_spec["cases"] if r["phase"] == "heldout"} ) == 12
    first_twenty = [r for r in main_spec["cases"] if r["phase"] == "heldout"][:20]
    assert len({r["family"] for r in first_twenty}) >= 3


def test_single_axis_shared_input(main_spec: dict) -> None:
    rows = [r for r in main_spec["cases"] if r["phase"] == "heldout" and r["seed"] == seed_for("S02", "heldout", 0, 0)]
    assert len(rows) == 3
    assert len({fingerprint(r["scenario"]["workload"]) for r in rows}) == 1
    assert len({r["load"]["lambda_tasks_per_second"] for r in rows}) == 1
    assert len({fingerprint(r["scenario"]["initial_robots"]) for r in rows}) == 1
    assert {r["scenario"]["factory"]["edges"][0]["width"] for r in rows} == {1.3, 2.2, 3.1}


def test_random_layouts_and_no_bridge_bypass() -> None:
    first, metadata = layout("S02", "heldout", 0)
    assert first.to_dict() == layout("S02", "heldout", 0)[0].to_dict()
    assert first.to_dict() != layout("S02", "heldout", 1)[0].to_dict()
    assert len(set(metadata["transforms"]["x"]["multipliers"])) > 1
    neighbors: dict[int, set[int]] = {}
    for edge in first.factory.edges:
        if edge.id != 1:
            neighbors.setdefault(edge.from_node, set()).add(edge.to_node)
            neighbors.setdefault(edge.to_node, set()).add(edge.from_node)
    reached, pending = set(), [2]
    while pending:
        current = pending.pop()
        if current not in reached:
            reached.add(current)
            pending.extend(neighbors.get(current, ()))
    assert 3 not in reached
    assert free_flow_cycle(first) > 0


def test_bursts_preserve_realized_count_attributes() -> None:
    anchor, _ = layout("S06", "training")
    a = demand_case(anchor, "S06", "S06_smooth", 201, .08)
    b = demand_case(anchor, "S06", "S06_burst_4", 201, .08)
    first = {r["uid"]: r for r in a.workload["jobs"]}
    second = {r["uid"]: r for r in b.workload["jobs"]}
    assert len(first) == len(second) > 0
    for uid in first:
        assert {k: v for k, v in first[uid].items() if k not in {"at", "deadline"}} == {
            k: v for k, v in second[uid].items() if k not in {"at", "deadline"}}
        assert first[uid]["deadline"] - first[uid]["at"] == pytest.approx(
            second[uid]["deadline"] - second[uid]["at"])


def test_capacity_saturation_has_no_heldout_seed_and_needs_backlog() -> None:
    case, seed, _ = probe_case("S05", "validation", 0)
    assert seed == seed_for("S05", "validation", 90, 0)
    assert seed != probe_case("S05", "training", 0)[1]
    report = {"segments": [{"completions": 10, "start": i * 100, "end": (i + 1) * 100,
                           "wip_before_start": 20} for i in range(6)],
              "scenario": case.to_dict(), "status": "PASS", "faults": 0, "rescue_energy": 0}
    assert summarize_probe(report)["rate_tasks_per_second"] == .1
    assert summarize_probe(report)["backlogged_final_bins"]
    report["segments"][-1]["wip_before_start"] = 0
    assert not summarize_probe(report)["backlogged_final_bins"]


def test_shards_collect_complete_and_reject_duplicate_or_changed(tmp_path: Path) -> None:
    body = small_spec()
    extra = copy.deepcopy(body["cases"][0])
    extra.update(id="second_training", seed=1101)
    body["cases"].append(extra)
    save(tmp_path / "spec.json", body)
    freeze_study(tmp_path / "spec.json", tmp_path / "frozen.json")
    paths = []
    for index in range(2):
        run_study(tmp_path / "frozen.json", tmp_path / f"shard{index}", phase="training",
                  shard_index=index, shard_count=2)
        paths.append(tmp_path / f"shard{index}" / "manifest.json")
    with pytest.raises(ValueError, match="missing or duplicate"):
        collect_study((paths[0], paths[0]), tmp_path / "duplicate.json")
    with pytest.raises(ValueError, match="missing or duplicate"):
        collect_study((paths[0],), tmp_path / "missing.json")
    parent = collect_study(tuple(paths), tmp_path / "collected.json")
    assert parent["status"] == "SUCCESS" and parent["planned_runs"] == 4
    plan = study_plan(tmp_path / "collected.json")
    save(tmp_path / "plan.json", plan)
    assert compare_plan(tmp_path / "plan.json")["full_observation_arms"] == 4
    path = paths[0]
    changed = json.loads(path.read_text())
    changed["run_wall_limit"] += 1
    save(path, changed)
    with pytest.raises(ValueError, match="changed"):
        study_plan(tmp_path / "collected.json")


def test_reference_summary_rejects_incomplete_and_seed_relabel() -> None:
    from simulation.evaluation.reference_capacity import family_statistics

    probes = [{"phase": phase, "repeat": repeat, "seed": i,
               "rate_tasks_per_second": .1, "free_flow_cycle_seconds": 20,
               "audit_pass": True, "backlogged_final_bins": True}
              for i, (phase, repeat) in enumerate((("training", 0), ("training", 1), ("training", 2),
                                                  ("validation", 0), ("validation", 1)))]
    assert family_statistics(probes)["status"] == "PASS"
    with pytest.raises(ValueError, match="incomplete"):
        family_statistics(probes[:-1])
    probes[-1]["seed"] = probes[0]["seed"]
    with pytest.raises(ValueError, match="overlap"):
        family_statistics(probes)


def test_json_main_inputs_are_portable(main_spec: dict) -> None:
    restored = json.loads(json.dumps(main_spec))
    assert restored["cases"] == main_spec["cases"]


def test_docs_only_revision_and_exploratory_descriptives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from simulation.evaluation import study

    body = small_spec()
    body["cases"][0]["inferential"] = False
    save(tmp_path / "spec.json", body)
    frozen = freeze_study(tmp_path / "spec.json", tmp_path / "frozen.json")
    # Exercise the committed-content branch separately from local dirty test sources.
    frozen["code"].update(python_matches_head=True, changed_python=[])
    frozen["study_hash"] = study._body_hash(frozen)
    save(tmp_path / "frozen.json", frozen)
    monkeypatch.setattr(study, "code_identity", lambda: {"commit": "f" * 40,
                         "python_matches_head": True, "changed_python": []})
    run_study(tmp_path / "frozen.json", tmp_path / "runs", phase="training")
    plan = study_plan(tmp_path / "runs/manifest.json")
    save(tmp_path / "plan.json", plan)
    report = compare_plan(tmp_path / "plan.json")
    group = next(g for g in report["comparisons"] if g["metric"] == "throughput_per_min")
    assert group["pairs"][0]["difference"] is not None
    assert "PREREGISTERED_EXPLORATORY_CONDITION" in group["across_layouts"]["estimate"]["withheld"]


def test_practical_margin_is_the_tested_null() -> None:
    from simulation.evaluation.paired_statistics import mean_inference

    result = mean_inference([.05, .1, .15], resamples=1000, null_difference=.1)
    assert result["null_difference"] == .1
    assert result["p_value"] == pytest.approx(1)
    assert result["mean_difference"] == pytest.approx(.1)
    assert result["t_interval"][0] < .1 < result["t_interval"][1]


def test_relative_energy_preserves_zero_denominators() -> None:
    from simulation.evaluation.comparison import paired_metric_values

    def arm(energy: float | None) -> dict:
        return {"result": {"energy_per_completed": energy}, "full_observation": True}

    assert paired_metric_values(arm(2), arm(1), "energy_per_completed_relative_change", "stream") == (1, .5)
    assert paired_metric_values(arm(0), arm(1), "energy_per_completed_relative_change", "stream") == (None, None)
    assert paired_metric_values(arm(None), arm(1), "energy_per_completed_relative_change", "stream") == (None, None)


def test_main_gate_blocks_before_creating_output(tmp_path: Path) -> None:
    body = small_spec()
    body["execution"] = {"require_readiness": True, "readiness_path": str(tmp_path / "absent.json")}
    save(tmp_path / "spec.json", body)
    freeze_study(tmp_path / "spec.json", tmp_path / "frozen.json")
    with pytest.raises(ValueError, match="readiness"):
        run_study(tmp_path / "frozen.json", tmp_path / "never_started", phase="heldout")
    assert not (tmp_path / "never_started").exists()


def test_primary_family_keeps_failed_slots_and_excludes_layout_diagnostics(tmp_path: Path) -> None:
    from simulation.evaluation.comparison_inputs import PLAN_SCHEMA

    body = {"schema": PLAN_SCHEMA, "purpose": "e0", **small_spec()["analysis"]}
    body.update(multiplicity_scope="across_layouts_only",
                inferential_metrics=["throughput_per_min", "dispatch_timely_acceptance_rate"])
    body["blocks"] = [{"condition": "one", "layout": f"L{draw}", "replicate": str(repeat),
                       "seed": 1000 + draw * 100 + repeat, "stochastic": True, "scenario": "x",
                       "demand_kind": "stream", "suite": None, "unstarted_status": "NOT_STARTED"}
                      for draw in range(6) for repeat in range(10)]
    save(tmp_path / "plan.json", body)
    report = compare_plan(tmp_path / "plan.json")
    assert report["planned_arms"] == 120
    assert report["holm_family_size"] == 2
    assert all(g["across_layouts"]["estimate"]["p_value"] is None for g in report["comparisons"])
    from simulation.evaluation.comparison import write_comparison

    write_comparison(report, tmp_path / "results")
    assert "test_null_difference" in (tmp_path / "results/effects.csv").read_text()


def test_main_launch_lock_is_serial_and_released(tmp_path: Path) -> None:
    from simulation.evaluation.main_jobs import _host_lock

    with _host_lock(tmp_path):
        with pytest.raises(ValueError, match="active"):
            with _host_lock(tmp_path):
                pass
        assert (tmp_path / "active_launch.lock/owner.json").exists()
    assert not (tmp_path / "active_launch.lock").exists()
