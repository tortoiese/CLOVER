"""Joint cabin paths must use actual floor continuity, not just NoOverlap."""

from __future__ import annotations

from dataclasses import replace

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.backend import Backend
from simulation.planning.contracts import RH_BACKENDS, Settings
from simulation.planning.execution import commit
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.task_generator import TaskState


def opposing_engine() -> SimulationEngine:
    """Two opposite-floor jobs require two separate rides of one cabin."""
    scenario = examples()[5]
    for station in scenario.factory.stations:
        station.process_time_std = 0
    engine = SimulationEngine(
        scenario.factory,
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
        initial_cabins=scenario.initial_cabins,
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.robots["R0"].emergency_order = "up"
    engine.robots["R1"].emergency_order = "down"
    for station, order in (("S1", "up"), ("S0", "down")):
        task = engine.add_task("inspection", station, 2, order_id=order)
        task.process_noise = 0
    return engine


@pytest.mark.parametrize("backend", tuple(RH_BACKENDS.values()))
def test_joint_model_serves_both_jobs_and_executes_two_real_rides(backend: str) -> None:
    engine = opposing_engine()
    config = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)
    data = prepare(capture(engine, config, "joint-cabin"))
    result = Backend(backend).solve(data)
    assert result.report.status == "OPTIMAL", result.report
    assert result.report.phase1_cost == 0 and not result.deferred
    assert sum(len(a.arc.cabins) for a in result.actions) == 2
    assert not validate(result)
    assert commit(engine, result) == (True, "COMMITTED")
    for at in range(1, 81):
        engine.env.run(until=at)
        assert_invariants(engine)
    assert engine.metrics.total_completed == 2 and engine.elevators["E"].rides == 2
    engine.close()


def test_restricted_control_defers_a_job_and_joint_compilers_have_same_optimum() -> None:
    engine = opposing_engine()
    config = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)
    old = Backend().solve(prepare(capture(engine, replace(config, joint_cabins=False), "old")))
    data = prepare(capture(engine, config, "new"))
    results = [Backend(kind).solve(data) for kind in RH_BACKENDS.values()]
    assert old.report.phase1_cost > 0 and len(old.deferred) == 1
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in results)
    assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in results}) == 1
    assert len({r.report.semantic_hash for r in results}) == 1
    engine.close()


def test_validator_rejects_wrong_cabin_source_floor_even_without_time_overlap() -> None:
    engine = opposing_engine()
    config = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)
    result = Backend().solve(prepare(capture(engine, config, "mutate")))
    routes = list(result.input.routes)
    selected = [a.arc for a in result.actions if a.arc.cabins]
    index = selected[0].route
    route = routes[index]
    cabins = tuple(replace(use, source_floor=999.0) for use in route.cabins)
    routes[index] = replace(route, cabins=cabins)
    changed = replace(result, input=replace(result.input, routes=tuple(routes)))
    assert "cabin floor path discontinuity or overlap" in validate(changed, reconstruct_routes=False)
    engine.close()


def test_state_growth_limit_is_explicit_and_keeps_the_request() -> None:
    engine = opposing_engine()
    config = Settings(horizon=40, step=2, sigma=0, timeout=5, watchdog=7, max_cabin_states=1)
    data = prepare(capture(engine, config, "limit"))
    result = Backend().solve(data)
    assert result.report.status == "BUILD_LIMIT" and "cabin state" in result.report.message
    assert result.input == data and not result.actions
    engine.close()


def test_missing_observed_cabin_excludes_route_instead_of_crashing_variant_builder() -> None:
    engine = opposing_engine()
    snap = capture(engine, Settings(horizon=40, step=2), "missing")
    prepared = prepare(replace(snap, cabins=()))
    assert any(reason == "unavailable_cabin" for _, _, _, reason in prepared.unavailable_routes)
    assert all(not route.cabins for route in prepared.routes)
    engine.close()


def test_changed_cabin_source_requeues_instead_of_using_old_floor_assumption() -> None:
    engine = opposing_engine()
    config = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)
    result = Backend().solve(prepare(capture(engine, config, "stale-floor")))
    assert commit(engine, result)[0]
    events = []
    engine.ledger.sink = events.append
    elevator = engine.elevators["E"]

    def external_trip():
        with elevator.resource.request() as request:
            yield request
            yield from elevator.arrive(1)
            yield from elevator.carry(2)

    engine.env.process(external_trip())
    engine.env.run(until=80)
    assert any(row["event"] == "planning_cabin_source_changed" for row in events)
    assert any(task.status == TaskState.QUEUED for task in engine.tasks.values())
    assert all(task.status != TaskState.REJECTED for task in engine.tasks.values())
    assert_invariants(engine)
    engine.close()
