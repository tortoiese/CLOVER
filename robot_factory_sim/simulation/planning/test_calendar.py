"""Temporal capacities, owner deduplication and observed release certificates."""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.backend import Backend
from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import RH_BACKENDS, ResourceHold, Settings
from simulation.planning.execution import commit
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_service_planning import joint
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.plant import RobotInitial


def test_owner_maximum_sweep_and_different_owners_add() -> None:
    engine = joint()
    snapshot = capture(engine, Settings(horizon=20, step=1), "calendar")
    resource = "charge:C0"
    facilities = tuple(replace(f, capacity=3, occupied=1) if f.id == "C0" else f for f in snapshot.facilities)
    holds = (
        ResourceHold("a", resource, 1, "slot", 0, 4, "actual"),
        ResourceHold("a", resource, 2, "slot", 2, 6, "prefix"),
        ResourceHold("b", resource, 1, "slot", 3, 5, "prefix"),
    )
    calendar = build_calendar(prepare(replace(snapshot, facilities=facilities, holds=holds)))
    assert [calendar.at(resource, t) for t in range(7)] == [2, 2, 1, 0, 0, 1, 3]
    assert calendar.blocked(resource, 7) == ((0, 2, 1), (2, 3, 2), (3, 5, 3), (5, 6, 2))
    engine.close()


def test_unknown_external_occupancy_and_overcapacity_drain_remain_blocked() -> None:
    engine = joint()
    snap = capture(engine, Settings(horizon=20, step=2), "opaque")
    facilities = tuple(replace(f, capacity=1, occupied=2) if f.id == "S1" else f for f in snap.facilities)
    calendar = build_calendar(prepare(replace(snap, facilities=facilities)))
    assert calendar.at("work:S1", 0) == calendar.at("work:S1", 10000) == 0
    engine.close()


def test_two_actual_claims_are_summed_even_when_their_owner_is_same() -> None:
    engine = joint()
    snap = capture(engine, Settings(horizon=20, step=1), "sum")
    facilities = tuple(replace(f, capacity=3, occupied=2) if f.id == "C0" else f for f in snap.facilities)
    holds = tuple(ResourceHold("a", "charge:C0", 1, "slot", 0, None, "actual") for _ in range(2))
    calendar = build_calendar(prepare(replace(snap, facilities=facilities, holds=holds)))
    assert calendar.at("charge:C0", 0) == 1
    engine.close()


def test_waiting_owner_blocks_slot_after_a_known_current_release() -> None:
    engine = joint()
    snap = capture(engine, Settings(horizon=20, step=1), "queue")
    facilities = tuple(replace(f, capacity=1, occupied=1, waiting=1) if f.id == "C0" else f for f in snap.facilities)
    hold = ResourceHold("a", "charge:C0", 1, "slot", 0, 4, "actual")
    calendar = build_calendar(prepare(replace(snap, facilities=facilities, holds=(hold,))))
    assert calendar.at("charge:C0", 0) == calendar.at("charge:C0", 100) == 0
    engine.close()


@pytest.mark.parametrize("backend", tuple(RH_BACKENDS.values()))
def test_known_release_rounds_up_and_all_three_compilers_match(backend: str) -> None:
    engine = joint()
    snap = capture(engine, Settings(horizon=20, step=2, sigma=0, timeout=10, watchdog=12), "release")
    facilities = tuple(replace(f, occupied=1) if f.id == "S1" else f for f in snap.facilities)
    hold = ResourceHold("external", "work:S1", 1, "slot", 0, 4.1, "actual")
    data = prepare(replace(snap, facilities=facilities, holds=(hold,)))
    calendar = build_calendar(data)
    assert calendar.at("work:S1", 2) == 0 and calendar.at("work:S1", 3) == 1
    result = Backend(backend).solve(data)
    assert result.report.status == "OPTIMAL" and result.report.phase1_cost == 0, result.report
    assert not validate(result)
    service = next(a.arc for a in result.actions if a.arc.job == "first")
    assert service.service_start >= 3
    assert decode(json.loads(json.dumps(encode(result)))) == result
    engine.close()


def test_validator_detects_capacity_drop_in_the_middle_of_selected_service() -> None:
    engine = joint()
    snap = capture(engine, Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7), "drop")
    result = Backend().solve(prepare(snap))
    assert not validate(result)
    changed = replace(snap, holds=(ResourceHold("future", "work:S1", 1, "slot", 5, None, "prefix"),))
    assert "resource capacity exceeded" in validate(replace(result, input=replace(result.input, snapshot=changed)))
    engine.close()


def test_tail_release_is_observed_only_after_motion_and_never_guessed_during_it() -> None:
    scenario = examples()[0]
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    robot = engine.robots["R0"]
    assert engine.assign(engine.add_task("inspection", "S1", 2), robot)
    engine.env.run(until=1)
    assert all(h.end is None for h in capture(engine, Settings(), "moving").holds if h.unit == "mm")
    while robot._traffic_release_at is None:
        engine.env.step()
    end = robot._traffic_release_at
    snapshot = capture(engine, Settings(), "tail")
    holds = [h for h in snapshot.holds if h.unit == "mm"]
    assert holds and all(h.end == end for h in holds)
    engine.env.run(until=end + 0.01)
    assert not any(h.unit == "mm" for h in capture(engine, Settings(), "released").holds)
    assert_invariants(engine)
    engine.close()


def test_finished_route_does_not_reserve_old_corridors_while_working() -> None:
    engine = joint()
    data = prepare(capture(engine, Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7), "prefix"))
    result = Backend().solve(data)
    assert commit(engine, result)[0]
    engine.env.run(until=6)
    assert any(r._work_started is not None for r in engine.robots.values())
    snap = capture(engine, Settings(), "working")
    assert any(h.unit == "mm" for h in snap.committed)
    assert not any(h.unit == "mm" for h in snap.holds)
    assert all(h.end is None for h in snap.holds if h.resource.startswith("work:"))
    assert_invariants(engine)
    engine.close()


def test_active_fixed_charge_has_a_release_bound_without_double_counting_slot() -> None:
    scenario = examples()[0]
    scenario.factory.robots = scenario.factory.robots[:1]
    scenario.factory.robots[0].battery_capacity = 30
    scenario.factory.robots[0].charge_threshold = 0
    engine = SimulationEngine(
        scenario.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(200, 200, soc_pct=10)}
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S0", 2)
    config = Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7)
    result = Backend().solve(prepare(capture(engine, config, "fixed-charge")))
    assert commit(engine, result)[0]
    engine.env.run(until=0.1)
    until = engine.robots["R0"]._planned_charge_until
    assert until is not None and until > engine.now
    snapshot = capture(engine, config, "charging")
    calendar = build_calendar(prepare(snapshot))
    resource = "charge:C"
    capacity = calendar.capacities[resource][1]
    assert calendar.at(resource, 0) == capacity - 1
    release_tick = math.ceil((until - engine.now) / config.step - 1e-9)
    assert calendar.at(resource, release_tick) == capacity
    engine.env.run(until=until + 0.01)
    assert engine.robots["R0"]._planned_charge_until is None
    assert_invariants(engine)
    engine.close()
