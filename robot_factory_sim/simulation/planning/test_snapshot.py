"""Independent observations and actual-motion acceptance for the PR9 adapter."""

from __future__ import annotations

import copy
import json
import multiprocessing
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.contracts import Settings
from simulation.planning.snapshot import capture, prepare
from simulation.travel import estimate_trip


def initialize(engine: SimulationEngine) -> None:
    """Process time-zero initialization without dispatching or changing sim time."""
    dispatch = engine.scheduler.dispatch
    engine.scheduler.dispatch = lambda plant: None
    while engine.env.peek() == 0:
        engine.env.step()
    engine.scheduler.dispatch = dispatch


def observed_engine(index: int = 0) -> SimulationEngine:
    """Build an isolated plant with all initial state set before process start."""
    scenario = examples()[index]
    engine = SimulationEngine(
        scenario.factory,
        "baseline",
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
        initial_cabins=scenario.initial_cabins,
    )
    initialize(engine)
    return engine


def test_snapshot_is_detached_frozen_and_has_no_future_samples() -> None:
    engine = observed_engine()
    task = engine.add_task("inspection", "S0", 2, external_uid="known")
    first = capture(engine, Settings(), "t1")
    task.process_noise = 100000
    # Future events reside in a runner, never in this planning observation.
    second = capture(engine, Settings(), "t1")
    assert first == second and [job.uid for job in first.jobs] == ["known"]
    assert pickle.loads(pickle.dumps(first)) == first
    assert "process_noise" not in json.dumps(asdict(first))
    with pytest.raises(FrozenInstanceError):
        first.now = 1
    engine.factory.stations[0].avg_process_time = 900
    engine.env.run(until=1)
    assert first.facilities[0].nominal_seconds == pytest.approx(8.8)
    assert first.now == 0 and json.loads(first.factory_json)["stations"][0]["avg_process_time"] == 8
    engine.close()


@pytest.mark.parametrize("index", range(6))
def test_detached_projection_matches_real_free_flow_motion(index: int) -> None:
    scenario = examples()[index]
    scenario.factory.robots = scenario.factory.robots[:1]
    rid = scenario.factory.robots[0].id
    destination = "S1"
    engine = SimulationEngine(
        scenario.factory,
        "baseline",
        demand_source="disabled",
        initial_robots={rid: scenario.initial_robots[rid]},
        initial_cabins=scenario.initial_cabins,
    )
    engine.scheduler.dispatch = lambda plant: None
    initialize(engine)
    engine.add_task("inspection", destination, 2)
    snapshot = capture(engine, Settings(), "projection")
    prepared = prepare(snapshot)
    data = next(r for r in prepared.routes if r.robot == rid and r.origin == "@" + rid and r.destination == destination)
    robot = engine.robots[rid]
    estimate = estimate_trip(engine, robot, destination)
    assert data.distance_m == pytest.approx(estimate.horizontal_m)
    assert data.seconds == pytest.approx(estimate.seconds)
    route = engine.path_graph.for_robot(robot.cfg.width).route_to_facility(*robot.position(), destination, robot.floor)
    process = engine.env.process(robot._travel_route(route))
    engine.env.run(until=process)
    assert engine.now == pytest.approx(data.seconds)
    assert robot.distance_m == pytest.approx(data.distance_m)
    assert robot.travel_energy_units == pytest.approx(data.distance_m * robot.cfg.battery_drain_rate)
    assert all(pool.count == 0 for pool in engine.corridors._pools.values())
    assert all(use.end <= data.seconds + 1e-8 for use in data.uses)
    engine.close()


def test_atomic_bundle_uses_whole_section_and_observes_real_claim_owner() -> None:
    engine = observed_engine(3)
    engine.add_task("inspection", "S1", 2)
    before = prepare(capture(engine, Settings(), "before"))
    data = next(r for r in before.routes if r.robot == "R0" and r.origin == "@R0" and r.destination == "S1")
    assert len(data.uses) == 3  # incoming edge, crossing slot, outgoing edge
    assert len({(use.start, use.end) for use in data.uses}) == 1
    assert {use.unit for use in data.uses} == {"mm", "slot"}
    engine.env.run(until=1)
    snapshot = capture(engine, Settings(), "during")
    assert len(snapshot.claims) == 3
    assert {c.owner for c in snapshot.claims} == {"R0"}
    assert len({c.request_id for c in snapshot.claims}) == 1
    assert all(claim.end is None for claim in snapshot.claims)
    assert {c.amount for c in snapshot.claims if c.unit == "mm"} == {900}
    assert {c.amount for c in snapshot.claims if c.unit == "slot"} == {1}
    engine.close()


def test_candidate_aging_and_exclusion_accounting_are_explicit() -> None:
    engine = observed_engine()
    engine.scheduler.dispatch = lambda plant: None
    old = engine.add_task("inspection", "S0", 3, external_uid="old")
    engine.env.run(until=300)
    urgent = engine.add_task("inspection", "S1", 0, external_uid="urgent")
    snapshot = capture(engine, Settings(max_tasks=1), "aging")
    assert snapshot.pending_count == 2 and snapshot.jobs[0].uid == old.external_uid
    assert snapshot.excluded[0].uid == urgent.external_uid and snapshot.excluded[0].reason == "candidate_limit"
    engine.close()


def test_cabin_fractional_position_is_preserved_and_unknown_release_is_not_guessed() -> None:
    engine = observed_engine(5)
    engine.add_task("inspection", "S1", 2)
    cabin = engine.elevators["E"]
    # A physical empty relocation is already in progress at observation time.
    engine.env.process(cabin._move(2))
    engine.scheduler.dispatch = lambda plant: None
    engine.env.run(until=1)
    snapshot = capture(engine, Settings(), "fractional")
    assert snapshot.cabins[0].floor == pytest.approx(1.25)
    assert snapshot.cabins[0].moving
    prepared = prepare(snapshot)
    assert not any(r.origin == "@R0" and r.destination == "S1" for r in prepared.routes)
    assert any(reason == "unknown_cabin_release" for _, _, _, reason in prepared.unavailable_routes)
    engine.close()


def test_worker_preparation_reads_the_captured_factory_not_subsequent_changes() -> None:
    engine = observed_engine(2)
    engine.add_task("inspection", "S1", 2)
    snapshot = capture(engine, Settings(), "frozen")
    before = prepare(snapshot)
    changed = copy.deepcopy(engine.factory)
    changed.edges[0].width = 0.5
    engine.apply_config(changed)
    assert prepare(snapshot) == before
    blocked = prepare(capture(engine, Settings(), "blocked"))
    assert any(row[-1] == "no_width_safe_path" for row in blocked.unavailable_routes)
    engine.close()


def test_route_limit_deadline_and_old_schema_fail_explicitly() -> None:
    engine = observed_engine()
    engine.add_task("inspection", "S0", 2)
    snapshot = capture(engine, Settings(max_routes=1), "bounded")
    with pytest.raises(ValueError, match="ROUTE_LIMIT"):
        prepare(snapshot)
    with pytest.raises(TimeoutError):
        prepare(snapshot, deadline=time.monotonic() - 1)
    with pytest.raises(ValueError, match="contract"):
        prepare(replace(snapshot, schema="clover-plan-v2"))
    engine.close()


def test_drain_has_no_new_route_permissions() -> None:
    engine = observed_engine()
    engine.add_task("inspection", "S0", 2)
    engine.corridors.pause_admission()
    prepared = prepare(capture(engine, Settings(), "drain"))
    assert not prepared.routes
    assert all(row[-1] == "traffic_drain" for row in prepared.unavailable_routes)
    engine.close()


def test_real_spawn_worker_prepares_identical_detached_routes() -> None:
    engine = observed_engine(3)
    engine.add_task("inspection", "S1", 2)
    snapshot = capture(engine, Settings(), "worker")
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as worker:
        prepared = worker.submit(prepare, snapshot).result(timeout=15)
    assert prepared == prepare(snapshot)
    engine.close()


def test_decimal_reserve_has_no_binary_float_extra_quantum() -> None:
    scenario = examples()[0]
    scenario.factory.robots[0].battery_capacity = 9.99
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    robot = capture(engine, Settings(), "energy").robots[0]
    assert robot.capacity == 9990 and robot.battery == 9990 and robot.reserve == 999
    engine.close()
