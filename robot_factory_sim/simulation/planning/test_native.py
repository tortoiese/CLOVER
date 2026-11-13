"""Native free-start intervals: complete coarse parity and finer physical execution."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.backend import Backend
from simulation.planning.contracts import ResourceHold, Settings
from simulation.planning.execution import commit
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_inflight import inflight_engine
from simulation.planning.test_service_planning import joint
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.plant import RobotInitial


def native(cfg: Settings, unit: float | None = None, actions: int = 0) -> Settings:
    """Keep the physical objective reference while selecting a native time unit."""
    return replace(cfg, formulation="native", native_step=cfg.step if unit is None else unit, native_actions=actions)


@pytest.mark.parametrize("case", (0, 2, 3))
def test_complete_native_domain_matches_three_grid_optima_and_spec(case: int) -> None:
    scenario = examples()[case]
    for station in scenario.factory.stations:
        station.process_time_std = 0
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    for sid in ("S0", "S1"):
        engine.add_task("inspection", sid, 2)
    cfg = Settings(horizon=8, step=2, sigma=0, timeout=10, watchdog=12)
    grid_data = prepare(capture(engine, cfg, "parity"))
    results = [Backend(kind).solve(grid_data) for kind in ("milp", "cpsat_linear", "cpsat_interval")]
    data = prepare(capture(engine, native(cfg), "parity"))
    other = Backend("cpsat_native").solve(data)
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in (*results, other)), other.report
    assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in (*results, other)}) == 1
    assert len({r.report.semantic_spec_hash for r in (*results, other)}) == 1
    assert other.report.model_hash != results[0].report.model_hash and other.report.action_scope == "complete"
    assert decode(json.loads(json.dumps(encode(other)))) == other
    engine.close()


def test_complete_domain_fixed_ride_parity_and_real_execution() -> None:
    engine = inflight_engine()
    cfg = Settings(horizon=8, interval=8, step=2, sigma=0, timeout=10, watchdog=12)
    grid = Backend().solve(prepare(capture(engine, cfg, "fixed")))
    result = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg), "fixed")))
    assert result.report.status == "OPTIMAL", result.report
    assert (result.report.phase1_cost, result.report.phase2_cost) == (grid.report.phase1_cost, grid.report.phase2_cost)
    assert result.report.semantic_spec_hash == grid.report.semantic_spec_hash
    assert not validate(result) and commit(engine, result)[0]
    engine.env.run(until=160)
    assert engine.metrics.total_completed == 2
    assert_invariants(engine)
    engine.close()


def test_fine_native_starts_at_certified_release_instead_of_rounding_to_two_seconds() -> None:
    engine = inflight_engine()
    cfg = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)
    result = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg, 0.1, 4), "fine")))
    assert result.report.status == "OPTIMAL" and not validate(result), result.report
    action = next(a.arc for a in result.actions if a.arc.job == "down")
    assert action.start == 60 and action.service_start == 264
    assert result.report.secondary_scale == 20
    assert result.report.action_scope == "at most 4 visits per robot"
    assert commit(engine, result)[0]
    engine.env.run(until=160)
    assert engine.metrics.total_completed == 2
    assert_invariants(engine)
    engine.close()


def test_variable_charge_is_equivalent_to_consecutive_grid_quanta() -> None:
    case = examples()[0]
    case.factory.robots = case.factory.robots[:1]
    case.factory.robots[0].battery_capacity = 30
    case.factory.robots[0].charge_threshold = 0
    for station in case.factory.stations:
        station.process_time_std = 0
    engine = SimulationEngine(
        case.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(200, 200, soc_pct=10)}
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S0", 2)
    cfg = Settings(horizon=8, interval=8, step=2, sigma=0, timeout=10, watchdog=12)
    grid = Backend().solve(prepare(capture(engine, cfg, "charge")))
    result = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg), "charge")))
    assert result.report.status == "OPTIMAL" and not validate(result), result.report
    assert (result.report.phase1_cost, result.report.phase2_cost) == (grid.report.phase1_cost, grid.report.phase2_cost)
    assert result.report.semantic_spec_hash == grid.report.semantic_spec_hash
    charge = next(a for a in result.actions if a.arc.kind == "charge")
    assert charge.arc.end - charge.arc.service_start > 1 and charge.gain > 0
    assert commit(engine, result)[0]
    engine.env.run(until=7)
    assert engine.robots["R0"].charged_energy_units > 0
    following = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg), "after-charge")))
    assert not following.deferred and commit(engine, following)[0]
    engine.env.run(until=40)
    assert engine.metrics.total_completed == 1
    assert_invariants(engine)
    engine.close()


def test_native_limit_is_explicit_and_does_not_return_partial_unvalidated_actions() -> None:
    engine = joint()
    cfg = native(Settings(horizon=8, step=2, max_arcs=1, timeout=5, watchdog=7))
    data = prepare(capture(engine, cfg, "limit"))
    result = Backend("cpsat_native").solve(data)
    assert result.report.status == "BUILD_LIMIT" and result.input == data and not result.actions
    engine.close()


def test_native_visit_bound_changes_spec_and_is_independently_enforced() -> None:
    engine = joint()
    cfg = Settings(horizon=20, step=2, sigma=0, timeout=10, watchdog=12)
    a = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg, 2, 1), "one")))
    b = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg, 2, 2), "two")))
    assert a.report.status == b.report.status == "OPTIMAL"
    assert a.report.semantic_spec_hash != b.report.semantic_spec_hash
    assert not validate(a) and not validate(b)
    engine.close()


def test_fine_resource_release_is_not_rounded_to_the_two_second_grid() -> None:
    engine = joint()
    cfg = native(Settings(horizon=20, step=2, sigma=0, timeout=10, watchdog=12), 0.1, 4)
    snap = capture(engine, cfg, "release")
    facilities = tuple(replace(f, occupied=1) if f.id == "S1" else f for f in snap.facilities)
    snap = replace(
        snap, facilities=facilities, holds=(ResourceHold("external", "work:S1", 1, "slot", 0, 4.1, "actual"),)
    )
    result = Backend("cpsat_native").solve(prepare(snap))
    assert result.report.status == "OPTIMAL" and not validate(result), result.report
    first = next(a.arc for a in result.actions if a.arc.job == "first")
    assert first.service_start == 41
    engine.close()


def test_two_services_are_ordered_for_one_robot_and_replanned_after_prefix() -> None:
    case = examples()[0]
    case.factory.robots = case.factory.robots[:1]
    for station in case.factory.stations:
        station.avg_process_time, station.process_time_std = 2, 0
    engine = SimulationEngine(case.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(100, 200)})
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    for _ in range(2):
        engine.add_task("inspection", "S0", 2)
    cfg = native(Settings(horizon=8, interval=8, step=2, sigma=0, timeout=10, watchdog=12))
    result = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "serial")))
    assert result.report.status == "OPTIMAL" and not result.deferred and not validate(result)
    services = [a.arc for a in result.actions if a.arc.kind == "service"]
    assert len(services) == 2 and services[1].start >= services[0].end
    assert commit(engine, result)[0]
    engine.env.run(until=3)
    assert engine.metrics.total_completed == 1
    following = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "serial-next")))
    assert commit(engine, following)[0]
    engine.env.run(until=8)
    assert engine.metrics.total_completed == 2
    assert_invariants(engine)
    engine.close()


def test_native_phase_two_exception_keeps_the_validated_primary_solution(monkeypatch: pytest.MonkeyPatch) -> None:
    from ortools.sat.python import cp_model

    real = cp_model.CpSolver.solve
    calls = 0

    def solve(self: Any, model: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected secondary failure")
        return real(self, model, *args, **kwargs)

    monkeypatch.setattr(cp_model.CpSolver, "solve", solve)
    engine = joint()
    cfg = native(Settings(horizon=8, step=2, sigma=0, timeout=10, watchdog=12))
    result = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "retain")))
    assert result.report.phase1_status == "OPTIMAL" and result.report.phase2_status == "ERROR"
    assert result.report.status == "FEASIBLE" and not result.deferred and not validate(result)
    assert "phase1 retained" in result.report.message
    engine.close()


def test_native_cabin_node_limit_is_explicit() -> None:
    engine = inflight_engine()
    cfg = native(Settings(horizon=8, interval=8, step=2, sigma=0, timeout=5, watchdog=7, max_cabin_states=1))
    result = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "cabin-limit")))
    assert result.report.status == "BUILD_LIMIT" and "cabin node" in result.report.message
    engine.close()


def test_backend_cannot_silently_change_recorded_time_domain() -> None:
    engine = joint()
    cfg = Settings(horizon=8, step=2, sigma=0, timeout=5, watchdog=7)
    wrong_native = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "wrong-native")))
    wrong_grid = Backend().solve(prepare(capture(engine, native(cfg), "wrong-grid")))
    assert wrong_native.report.status == wrong_grid.report.status == "MODEL_INVALID"
    engine.close()


def test_deadline_zero_at_later_snapshot_keeps_exact_coarse_objective() -> None:
    engine = joint()
    for task in engine.tasks.values():
        task.deadline = 0
    engine.env.run(until=30)
    cfg = Settings(horizon=8, step=2, sigma=0, timeout=10, watchdog=12)
    grid = Backend().solve(prepare(capture(engine, cfg, "late")))
    result = Backend("cpsat_native").solve(prepare(capture(engine, native(cfg), "late")))
    assert grid.report.status == result.report.status == "OPTIMAL"
    assert (grid.report.phase1_cost, grid.report.phase2_cost) == (result.report.phase1_cost, result.report.phase2_cost)
    assert not validate(result)
    engine.close()


def test_no_return_charger_allows_safe_hold_instead_of_false_service_feasibility() -> None:
    case = examples()[0]
    case.factory.chargers = []
    engine = SimulationEngine(case.factory, demand_source="disabled", initial_robots=case.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S0", 2)
    cfg = native(Settings(horizon=8, step=2, sigma=0, timeout=10, watchdog=12))
    result = Backend("cpsat_native").solve(prepare(capture(engine, cfg, "no-charger")))
    assert result.report.status == "OPTIMAL" and len(result.deferred) == 1 and not validate(result)
    assert all(a.arc.kind == "wait" for a in result.actions)
    engine.close()


@pytest.mark.parametrize(
    "changes",
    (
        {"native_step": True},
        {"native_step": 0.3},
        {"native_actions": True},
        {"native_actions": -1},
        {"formulation": "unknown"},
        {"native_pruning": 1},
        {"native_cache": "yes"},
    ),
)
def test_native_settings_reject_ambiguous_scope_and_units(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(Settings(), **changes).validate()
