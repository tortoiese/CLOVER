"""An admitted protected ride supplies a future cabin state, never queue foresight."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.backend import Backend
from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import RH_BACKENDS, Settings
from simulation.planning.execution import commit
from simulation.planning.incumbent import check, work_hint
from simulation.planning.model import build_model
from simulation.planning.process import SolverProcess
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.plant import RobotInitial
from simulation.robot import InterruptKind
from simulation.task_generator import TaskState

SETTINGS = Settings(horizon=40, interval=40, step=2, sigma=0, timeout=10, watchdog=12)


def inflight_engine(at: float = 4) -> SimulationEngine:
    """Put R0 in a real upward ride while R1 is available at the upper lobby."""
    case = examples()[5]
    for station in case.factory.stations:
        station.process_time_std = 0
    case.factory.stations[1].avg_process_time = 100
    engine = SimulationEngine(
        case.factory,
        demand_source="disabled",
        initial_cabins={"E": 1},
        initial_robots={"R0": RobotInitial(200, 200), "R1": RobotInitial(200, 200, floor=2)},
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    up = engine.add_task("inspection", "S1", 2, external_uid="up")
    up.process_noise = 0
    assert engine.assign(up, engine.robots["R0"])
    engine.env.run(until=at)
    if at >= 3:
        assert engine.robots["R0"]._riding is engine.elevators["E"]
    down = engine.add_task("inspection", "S0", 2, external_uid="down")
    down.process_noise = 0
    return engine


@pytest.mark.parametrize("backend", tuple(RH_BACKENDS.values()))
def test_busy_cabin_fixed_transition_matches_all_compilers_and_real_execution(backend: str) -> None:
    engine = inflight_engine()
    snap = capture(engine, SETTINGS, "inflight")
    cabin = snap.cabins[0]
    assert cabin.floor == pytest.approx(1.25) and cabin.release_floor == 2 and cabin.release_at == 10
    assert cabin.release_owner == "robot:0:R0"
    data = prepare(snap)
    calendar = build_calendar(data)
    assert calendar.at("cabin:E", 0) == 0 and calendar.at("cabin:E", 3) == 1
    model = build_model(data)
    fixed = model.cabin_fixed["E"]
    assert (fixed.source, fixed.destination, fixed.start, fixed.end) == (1.25, 2, 0, 3)
    assert model.variables[model.cabin_fixed_variables["E"]].lower == 1
    check(model, work_hint(model, float("inf")).values)
    result = Backend(backend).solve(data)
    assert result.report.status == "OPTIMAL" and result.report.phase1_cost == 0, result.report
    assert not validate(result) and decode(json.loads(json.dumps(encode(result)))) == result
    action = next(a.arc for a in result.actions if a.arc.job == "down")
    trip = data.routes[action.route].cabins[0]
    assert trip.source_floor == 2 and action.start * SETTINGS.step + trip.start >= 6
    assert commit(engine, result) == (True, "COMMITTED")
    engine.env.run(until=160)
    assert engine.metrics.total_completed == 2 and engine.elevators["E"].rides == 2
    assert_invariants(engine)
    engine.close()


def test_fixed_transition_has_equal_optima_hash_and_unknown_control_has_no_trip() -> None:
    engine = inflight_engine()
    snap = capture(engine, SETTINGS, "parity")
    data = prepare(snap)
    results = [Backend(kind).solve(data) for kind in RH_BACKENDS.values()]
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in results)
    assert len({(r.report.phase1_cost, r.report.phase2_cost, r.report.semantic_hash) for r in results}) == 1
    old = replace(snap.cabins[0], release_at=None, release_floor=None, release_owner=None)
    unknown = prepare(replace(snap, cabins=(old,)))
    assert all(not r.cabins for r in unknown.routes)
    assert any(reason == "unknown_cabin_release" for _, _, _, reason in unknown.unavailable_routes)
    assert Backend().solve(unknown).deferred == ("down",)
    engine.close()


@pytest.mark.parametrize("at", (0.1, 2.9))
def test_boarding_is_interruptible_and_has_no_release_certificate(at: float) -> None:
    engine = inflight_engine(at)
    snap = capture(engine, SETTINGS, "boarding")
    assert snap.cabins[0].occupied == 1 and snap.cabins[0].release_at is None
    assert all(not r.cabins for r in prepare(snap).routes)
    engine.close()


def test_alighting_interrupts_and_config_change_cannot_extend_certified_release() -> None:
    engine = inflight_engine()
    robot, cabin = engine.robots["R0"], engine.elevators["E"]
    robot.process.interrupt({"kind": InterruptKind.REPLAN})
    cabin.update_config(replace(cabin.cfg, travel_time=100))
    cabin.door_time = 100
    engine.env.run(until=8)
    assert capture(engine, SETTINGS, "doors").cabins[0].release_at == 10
    robot.process.interrupt({"kind": InterruptKind.RETIRE})
    engine.env.run(until=9.5)
    assert robot.alive and cabin.occupied == 1
    robot.process.interrupt({"kind": InterruptKind.REPLAN})
    engine.env.run(until=10.001)
    assert not robot.alive and robot.floor == 2 and cabin.occupied == 0 and cabin.rides == 1
    assert robot._cabin_release_at is None and robot._cabin_request is None
    assert next(task for task in engine.tasks.values() if task.external_uid == "up").status == TaskState.QUEUED
    assert_invariants(engine)
    engine.close()


def test_existing_queue_and_future_same_cabin_obligation_stay_blocked() -> None:
    engine = inflight_engine()
    cabin = engine.elevators["E"]

    def external_wait():
        with cabin.resource.request() as request:
            yield request
            yield engine.env.timeout(100)

    process = engine.env.process(external_wait())
    engine.env.run(until=4.01)
    snap = capture(engine, SETTINGS, "queued")
    assert snap.cabins[0].release_at == 10 and snap.cabins[0].waiting == 1
    data = prepare(snap)
    assert all(not r.cabins for r in data.routes)
    assert build_calendar(data).at("cabin:E", 100) == 0
    process.interrupt()
    # Consume the intentionally cancelled external process as an expected failure.
    process.defused = True
    engine.close()


def test_later_unobserved_use_of_same_cabin_is_not_released_with_current_ride() -> None:
    engine = inflight_engine()
    engine.robots["R0"].route_lifts.append("E")
    snap = capture(engine, SETTINGS, "later-lift")
    assert any(h.source == "future" and h.resource == "cabin:E" and h.end is None for h in snap.holds)
    assert build_calendar(prepare(snap)).at("cabin:E", 100) == 0
    engine.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"release_floor": None},
        {"release_owner": None},
        {"release_at": float("nan")},
        {"release_floor": 1.5},
        {"release_at": 4},
        {"release_owner": "wrong-owner"},
        {"release_floor": 99},
    ],
)
def test_partial_or_unowned_release_certificate_is_rejected(changes: dict) -> None:
    engine = inflight_engine()
    snap = capture(engine, SETTINGS, "bad-certificate")
    with pytest.raises(ValueError, match="MODEL_INVALID"):
        prepare(replace(snap, cabins=(replace(snap.cabins[0], **changes),)))
    engine.close()


def test_unowned_certificate_rejected_by_worker_backend_and_independent_validator() -> None:
    engine = inflight_engine()
    snap = capture(engine, SETTINGS, "unowned")
    data = prepare(snap)
    invalid = replace(snap, holds=tuple(h for h in snap.holds if h.resource != "cabin:E"))
    assert Backend().solve(replace(data, snapshot=invalid)).report.status == "MODEL_INVALID"
    result = Backend().solve(data)
    errors = validate(replace(result, input=replace(data, snapshot=invalid)), reconstruct_routes=False)
    assert errors and "matching actual hold" in errors[0]
    worker = SolverProcess()
    try:
        rejected = worker.solve(invalid, time.perf_counter())
        assert rejected.report.status == "MODEL_INVALID", rejected.report
    finally:
        worker.close()
        engine.close()


def test_validator_uses_future_arrival_floor_not_current_fractional_floor() -> None:
    engine = inflight_engine()
    result = Backend().solve(prepare(capture(engine, SETTINGS, "wrong-floor")))
    action = next(a.arc for a in result.actions if a.arc.job == "down")
    routes = list(result.input.routes)
    route = routes[action.route]
    routes[action.route] = replace(route, cabins=tuple(replace(c, source_floor=1.25) for c in route.cabins))
    changed = replace(result, input=replace(result.input, routes=tuple(routes)))
    assert "cabin floor path discontinuity or overlap" in validate(changed, reconstruct_routes=False)
    engine.close()


def test_passed_sections_and_cabin_leave_prefix_before_whole_route_finishes() -> None:
    case = examples()[5]
    engine = SimulationEngine(
        case.factory, demand_source="disabled", initial_robots=case.initial_robots, initial_cabins=case.initial_cabins
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.robots["R0"].emergency_order = "up"
    engine.robots["R1"].emergency_order = "none"
    engine.add_task("inspection", "S1", 2, order_id="up")
    result = Backend().solve(prepare(capture(engine, replace(SETTINGS, horizon=60, interval=60), "progress")))
    assert commit(engine, result)[0]
    robot = engine.robots["R0"]
    while robot._riding is None and engine.now < 80:
        engine.env.step()
    assert robot._riding is not None and robot.planned_route_progress() > 0
    route = result.input.routes[robot.planned_action[1].arc.route]
    passed = {
        u.resource
        for u, (_, end) in zip(route.uses, route.use_sections, strict=True)
        if end < robot.planned_route_progress()
    }
    assert passed
    snap = capture(engine, SETTINGS, "during-ride")
    assert not any(h.source == "prefix" and h.resource in passed for h in snap.holds)
    end = snap.cabins[0].release_at
    assert end is not None
    cabin_holds = [h for h in snap.holds if h.resource == "cabin:E"]
    assert {h.source for h in cabin_holds} == {"actual", "prefix"}
    assert all(h.end == end for h in cabin_holds)
    engine.env.run(until=end + 0.01)
    assert not robot.planned_route_completed() and robot.route
    after = capture(engine, SETTINGS, "after-ride")
    assert not any(h.resource == "cabin:E" for h in after.holds)
    assert any(h.source == "prefix" and h.unit == "mm" for h in after.holds)
    assert_invariants(engine)
    engine.close()


def test_intermediate_tail_has_same_finite_actual_and_prefix_release() -> None:
    case = examples()[5]
    engine = SimulationEngine(
        case.factory,
        demand_source="disabled",
        initial_robots=case.initial_robots,
        initial_cabins=case.initial_cabins,
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.robots["R0"].emergency_order = "up"
    engine.robots["R1"].emergency_order = "none"
    engine.add_task("inspection", "S1", 2, order_id="up")
    result = Backend().solve(prepare(capture(engine, replace(SETTINGS, horizon=60, interval=60), "tail")))
    assert commit(engine, result)[0]
    robot = engine.robots["R0"]
    while robot._traffic_release_at is None and engine.now < 80:
        engine.env.step()
    end = robot._traffic_release_at
    request = robot._traffic_request
    assert end is not None and robot.route
    resources = {pool.resource_id for pool in request.demands}
    snap = capture(engine, SETTINGS, "intermediate-tail")
    current = [h for h in snap.holds if h.resource in resources]
    assert {h.source for h in current} == {"actual", "prefix"}
    assert all(h.end == end for h in current)
    engine.env.run(until=end - 0.00001)
    assert request.granted and all(pool.count > 0 for pool in request.demands)
    engine.env.run(until=end + 0.00001)
    assert request.closed and robot.route and not robot.planned_route_completed()
    assert not any(h.resource in resources for h in capture(engine, SETTINGS, "past-tail").holds)
    assert_invariants(engine)
    engine.close()
