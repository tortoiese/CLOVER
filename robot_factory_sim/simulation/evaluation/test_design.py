"""Experimental pairing, executable inputs and tamper/phase boundaries."""

from __future__ import annotations

import copy
import json
from collections import Counter
from dataclasses import asdict
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from simulation.evaluation import design as preparation
from simulation.evaluation import prepared as execution
from simulation.evaluation.__main__ import main
from simulation.evaluation.design import materialize, prepare_design, write_json
from simulation.evaluation.design_references import synthetic_references
from simulation.evaluation.runner import replay, run_case
from simulation.path_graph import PathGraph


@pytest.fixture(scope="module")
def design() -> dict[str, Any]:
    """Use the reviewed design rather than a second private copy of its cells."""
    path = Path(__file__).resolve().parents[3] / "docs/scenarios/experiment_design_v3.json"
    return json.loads(path.read_text(encoding="utf-8"))


def cases(design: dict[str, Any], ids: tuple[str, ...], phase: str = "e0") -> dict[str, Any]:
    """Materialize a paired block under a single shared demand seed."""
    return {m["cell"]: s for s, m in materialize(design, synthetic_references(), phase, (1001,), ids)}


def test_full_design_is_executable_and_fixed_batches_are_not_seed_replicates(design: dict[str, Any]) -> None:
    rows = materialize(design, synthetic_references(), "e1-candidate", (1001, 1002, 1003))
    assert len(rows) == 162
    assert len({m["cell"] for _, m in rows}) == 62
    assert sum(m["demand_seed"] is None for _, m in rows) == 12
    assert len({s.id for s, _ in rows}) == 162
    for case, metadata in rows:
        case.validate()
        assert case.workload["kind"] == "trace"
        assert metadata["preflight"]["schema_valid"]
    by_cell = {m["cell"]: s for s, m in rows}
    assert len(by_cell["S11_graph_256"].factory.nodes) == 256
    assert len(by_cell["S11_robots_8"].factory.nodes) == 64
    assert len(by_cell["S11_robots_8"].initial_robots) == 8


@pytest.mark.parametrize("policy", ["baseline", "optimized"])
def test_empty_multifloor_experiment_has_no_visual_roaming(design: dict[str, Any], policy: str) -> None:
    case = cases(design, ("S08_two_cars",))["S08_two_cars"]
    case.workload = {"kind": "trace", "jobs": []}
    result = run_case(case, policy, 1001)
    assert result["status"] == "SUCCESS" and result["final_audit"]["ok"]
    assert result["offered"] == result["completed_total"] == 0
    assert result["energy"]["distance_m"] == result["energy"]["consumed"] == 0
    assert not any(p["distance_floors"] for p in result["cabin_phases"]["by_cabin_phase"])
    assert replay(result)["matches"]


@pytest.mark.parametrize("ids", [
    ("S00_width_1p3", "S00_width_1p8"), ("S02_width_1p3", "S02_width_3p1"),
    ("S04_anchor", "S04_slots_4"), ("S08_anchor", "S08_two_cars"),
])
def test_physical_treatments_keep_exact_external_input(design: dict[str, Any], ids: tuple[str, str]) -> None:
    block = cases(design, ids)
    first, second = (block[i] for i in ids)
    assert first.workload == second.workload
    assert first.initial_robots == second.initial_robots
    assert first.initial_cabins == second.initial_cabins
    if ids[0].startswith("S04"):
        first.factory.chargers[0].capacity = second.factory.chargers[0].capacity
    elif ids[0].startswith("S08"):
        first.factory.elevators[1].is_active = second.factory.elevators[1].is_active
    else:
        for a, b in zip(first.factory.edges, second.factory.edges):
            a.width = b.width
    assert first.factory.to_dict() == second.factory.to_dict()


def test_priority_is_the_only_workload_change_and_deadlines_are_fixed(design: dict[str, Any]) -> None:
    block = cases(design, ("S06_urgent_0", "S06_anchor", "S06_urgent_20"), "e1-candidate")
    traces = [s.workload["jobs"] for s in block.values()]
    baseline = [{k: v for k, v in j.items() if k != "priority"} for j in traces[0]]
    for trace in traces:
        assert [{k: v for k, v in j.items() if k != "priority"} for j in trace] == baseline
    assert sum(j["priority"] == 0 for j in block["S06_anchor"].workload["jobs"]) == round(len(baseline) * .05)
    assert sum(j["priority"] == 0 for j in block["S06_urgent_20"].workload["jobs"]) == round(len(baseline) * .2)


@pytest.mark.parametrize("multiplier", [2, 4])
def test_burst_preserves_realized_job_count_attributes_and_warmup(
    design: dict[str, Any], multiplier: int,
) -> None:
    block = cases(design, ("S06_anchor", f"S06_burst_{multiplier}"), "e1-candidate")
    a, b = (s.workload["jobs"] for s in block.values())
    assert len(a) == len(b) and len(a) > 0
    for first, second in zip(a, b):
        assert {k: v for k, v in first.items() if k not in {"at", "deadline"}} == {
            k: v for k, v in second.items() if k not in {"at", "deadline"}}
        assert first["deadline"] - first["at"] == pytest.approx(second["deadline"] - second["at"])
        if first["at"] < 600:
            assert first == second
    assert any(first["at"] != second["at"] for first, second in zip(a, b))
    assert all(first["at"] <= second["at"] for first, second in zip(b, b[1:]))


def test_load_changes_do_not_change_job_keyed_noise_or_destination(design: dict[str, Any]) -> None:
    block = cases(design, ("S10_lambda_0p22", "S10_lambda_0p26"), "e1-candidate")
    a, b = (s.workload["jobs"] for s in block.values())
    assert len(a) < len(b)
    for first, second in zip(a, b):
        assert {k: v for k, v in first.items() if k not in {"at", "deadline"}} == {
            k: v for k, v in second.items() if k not in {"at", "deadline"}}


def test_hotspot_is_exact_up_to_rounding_with_single_station_eligibility(design: dict[str, Any]) -> None:
    block = cases(design, ("S05_hotspot_25", "S05_hotspot_70", "S05_hotspot_90"), "e1-candidate")
    traces = []
    for name, case in block.items():
        jobs = case.workload["jobs"]
        counts = Counter(j["station"] for j in jobs)
        assert counts["S0"] == round(int(name[-2:]) / 100 * len(jobs))
        assert max(counts[f"S{i}"] for i in range(1, 4)) - min(counts[f"S{i}"] for i in range(1, 4)) <= 1
        assert all(j["allowed"] == [j["station"]] for j in jobs)
        traces.append([(j["uid"], j["at"], j["noise"], j["priority"], j["deadline"]) for j in jobs])
    assert traces[0] == traces[1] == traces[2]


@pytest.mark.parametrize("phase,start,recovery", [("e0", 60, 90), ("e1-candidate", 300, 420)])
def test_interventions_are_observed_and_all_arms_share_the_probe(
    design: dict[str, Any], phase: str, start: int, recovery: int,
) -> None:
    ids = tuple(c["id"] for c in design["base_cells"] if c["family"] == "S09")
    block = cases(design, ids, phase)
    trace = block["S09_anchor"].workload
    for case in block.values():
        assert case.workload == trace
        assert case.warmup == 0
        assert all(case.warmup <= e.at < case.duration for e in case.interventions)
    probe = next(j for j in trace["jobs"] if j["uid"] == "probe:intervention")
    assert probe["at"] == start - 1
    assert block["S09_cancel_task"].interventions[0].target == probe["uid"]
    assert [e.at for e in block["S09_station_active"].interventions] == [start, recovery]
    assert len(block["S09_robot_retire"].interventions) == 1


def test_intervention_in_warmup_is_rejected_instead_of_hidden(design: dict[str, Any]) -> None:
    references = synthetic_references()
    references["cases"]["S09"]["warmup"] = 600
    with pytest.raises(ValueError, match="outside measurement"):
        materialize(design, references, "e1-candidate", (1001,), ("S09_station_active",))


def test_mixed_width_robots_have_a_real_wide_bypass(design: dict[str, Any]) -> None:
    case = cases(design, ("S07_width_mix",))["S07_width_mix"]
    graph = PathGraph(case.factory)
    assert graph.shortest_path(1, 2, .6)[0] == [1, 2]
    assert graph.shortest_path(1, 2, .9)[0] == [1, 3, 4, 2]


def test_floor_mix_is_input_and_eligibility_stays_on_the_required_floor(design: dict[str, Any]) -> None:
    block = cases(design, ("X03_01", "X03_02", "X03_03", "X03_04"), "e1-candidate")
    for name, case in block.items():
        stations = {s.id: s for s in case.factory.stations}
        jobs = case.workload["jobs"]
        share = .25 if name in {"X03_01", "X03_03"} else .75
        assert sum(stations[j["station"]].floor == 2 for j in jobs) == round(share * len(jobs))
        assert all(all(stations[s].floor == stations[j["station"]].floor for s in j["allowed"]) for j in jobs)
    assert block["X03_01"].workload == block["X03_03"].workload


@pytest.mark.parametrize("failure", ["unknown_factor", "heldout_phase", "trace_reference"])
def test_unsupported_design_is_not_silently_materialized(design: dict[str, Any], failure: str) -> None:
    design, references = copy.deepcopy(design), synthetic_references()
    phase = "e0"
    if failure == "unknown_factor":
        design["base_cells"][0]["factor_values"]["slots_typo"] = 2
    elif failure == "heldout_phase":
        phase = "e2"
    else:
        references["cases"]["S00"]["workload"] = {"kind": "trace", "jobs": []}
    with pytest.raises(ValueError):
        materialize(design, references, phase, (1001,), ("S00_low",))


@pytest.mark.parametrize("tamper", ["trace", "initial_hash", "settings", "path"])
def test_prepared_inputs_are_checked_before_any_output(
    tmp_path: Path, design: dict[str, Any], tamper: str,
) -> None:
    folder = tmp_path / "prepared"
    manifest = prepare_design(design, synthetic_references(), folder, cells=("S01_eligible_1",))
    row = manifest["entries"][0]
    if tamper == "trace":
        path = folder / row["path"]
        raw = json.loads(path.read_text())
        raw["workload"]["jobs"][0]["noise"] += 1
        write_json(path, raw)
    elif tamper == "settings":
        write_json(folder / "math_settings.json", {**manifest["math_settings"], "horizon": 30})
    else:
        key = "initial_hash" if tamper == "initial_hash" else "path"
        row[key] = "changed" if tamper == "initial_hash" else "../elsewhere.json"
        write_json(folder / "manifest.json", manifest)
    with pytest.raises(ValueError):
        execution.run_prepared(folder, tmp_path / "runs", timing="D0", policies=("baseline",))
    assert not (tmp_path / "runs").exists()


def test_real_prepared_pair_preserves_inputs_and_replays(tmp_path: Path, design: dict[str, Any]) -> None:
    folder = tmp_path / "prepared"
    prepare_design(design, synthetic_references(), folder, seeds=(1001, 1002, 1003), cells=("S01_eligible_1",))
    output = tmp_path / "runs"
    result = execution.run_prepared(folder, output, timing="D0", policies=("baseline", "optimized"))
    assert result["planned_runs"] == 2 and result["status"] == "SUCCESS"
    block = result["entries"][0]
    assert block["demand_seed"] is None and block["fair_inputs"] and block["complete_policy_block"]
    assert len({r["trace_hash"] for r in block["runs"]}) == 1
    for row in block["runs"]:
        saved = json.loads((output / block["scenario"] / row["result"]).read_text())
        assert saved["offered"] == saved["completed_total"] == 8
        assert replay(saved)["matches"]


def test_source_drift_and_policy_scope_are_rejected(tmp_path: Path, design: dict[str, Any],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "prepared"
    prepare_design(design, synthetic_references(), folder, phase="e1-candidate", cells=("S02_width_1p3",))
    with pytest.raises(ValueError, match="scope"):
        execution.run_prepared(folder, tmp_path / "out", timing="D1", policies=("rh_cpsat_native",))
    monkeypatch.setattr(execution, "source_manifest", lambda: {"source_hash": "changed"})
    with pytest.raises(ValueError, match="source differs"):
        execution.run_prepared(folder, tmp_path / "out", timing="D0")
    assert not (tmp_path / "out").exists()


def test_budget_exhaustion_keeps_all_unrun_entries(tmp_path: Path, design: dict[str, Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    folder = tmp_path / "prepared"
    prepare_design(design, synthetic_references(), folder, cells=("S00_low", "S01_eligible_1"))
    ticks = count()
    monkeypatch.setattr(execution, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    result = execution.run_prepared(folder, tmp_path / "runs", timing="D0", suite_wall_limit=.5)
    assert result["status"] == "PARTIAL" and result["planned_runs"] == 14
    assert len(result["entries"]) == 2
    assert all(r["status"] == "NOT_RUN_BUDGET" and r["fair_inputs"] is None for r in result["entries"])


def test_source_change_after_a_block_preserves_results_and_stops_later_entries(
    tmp_path: Path, design: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "prepared"
    prepare_design(design, synthetic_references(), folder, cells=("S01_eligible_1", "S02_width_1p3"))
    source, run = execution.source_manifest(), execution.run_suite
    changed = []

    def run_then_change(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = run(*args, **kwargs)
        changed.append(True)
        return result

    monkeypatch.setattr(execution, "run_suite", run_then_change)
    monkeypatch.setattr(execution, "source_manifest",
                        lambda: {**source, "source_hash": "changed"} if changed else source)
    result = execution.run_prepared(folder, tmp_path / "runs", timing="D0", policies=("baseline",))
    assert result["status"] == "PARTIAL"
    assert [r["status"] for r in result["entries"]] == ["SUCCESS", "SOURCE_CHANGED"]
    assert result["entries"][0]["runs"][0]["completed_total"] == 8


def test_custom_budget_is_used_except_for_explicit_budget_treatments(
    tmp_path: Path, design: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "prepared"
    prepared = prepare_design(design, synthetic_references(), folder, cells=("S00_low", "S11_budget_0p5"))
    budgets = []

    def capture(*args: Any, **kwargs: Any) -> dict[str, Any]:
        budgets.append(kwargs["decision_settings"]["end_to_end_soft_deadline_seconds"])
        return {"status": "PARTIAL", "runs": []}

    monkeypatch.setattr(execution, "run_suite", capture)
    execution.run_prepared(folder, tmp_path / "runs", timing="D1", policies=("baseline",),
                           decision_settings={**prepared["decision_settings"], "end_to_end_soft_deadline_seconds": 7})
    assert budgets == [7, .5]


def test_source_change_while_writing_does_not_publish_a_ready_manifest(
    tmp_path: Path, design: dict[str, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, write = preparation.source_manifest(), preparation.write_json
    changed = []

    def write_then_change(path: Path, value: Any) -> None:
        write(path, value)
        if path.parent.name == "inputs":
            changed.append(True)

    monkeypatch.setattr(preparation, "write_json", write_then_change)
    monkeypatch.setattr(preparation, "source_manifest",
                        lambda: {**source, "source_hash": "changed"} if changed else source)
    with pytest.raises(ValueError, match="no prepared manifest"):
        prepare_design(design, synthetic_references(), tmp_path / "prepared", cells=("S00_low",))
    assert not (tmp_path / "prepared/manifest.json").exists()


def test_research_settings_are_frozen_without_changing_unspecified_defaults(
    tmp_path: Path, design: dict[str, Any],
) -> None:
    manifest = prepare_design(design, synthetic_references(), tmp_path / "prepared", cells=("S00_low",),
                              planning_settings={"timeout": .25},
                              decision_settings={"publication_allowance_seconds": .3})
    assert manifest["math_settings"]["timeout"] == .25
    assert manifest["math_settings"]["horizon"] == 40
    assert manifest["decision_settings"]["publication_allowance_seconds"] == .3
    assert manifest["decision_settings"]["end_to_end_soft_deadline_seconds"] == 2
    assert execution.verify_prepared(tmp_path / "prepared")[0] == json.loads(
        (tmp_path / "prepared/manifest.json").read_text())


def test_legacy_rh_only_d1_cannot_be_frozen_as_a_common_comparison(
    tmp_path: Path, design: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="common staged D1"):
        prepare_design(design, synthetic_references(), tmp_path / "prepared", cells=("S00_low",),
                       planning_settings={"timing_mode": "D1"})
    assert not (tmp_path / "prepared").exists()


def test_cli_freezes_inputs_and_rejects_overwrite(tmp_path: Path, design: dict[str, Any]) -> None:
    path = tmp_path / "design.json"
    write_json(path, design)
    args = ["prepare-design", "--design", str(path), "--synthetic-reference", "--cells", "S00_low",
            "--output", str(tmp_path / "prepared")]
    assert main(args) == 0
    prepared, entries = execution.verify_prepared(tmp_path / "prepared")
    assert prepared["provenance"] == "synthetic_reference_not_team_factory" and len(entries) == 1
    assert prepared["heldout_enabled"] is False
    assert main(args) == 2
    assert all(asdict(e) for e in entries[0][1].initial_robots.values())
