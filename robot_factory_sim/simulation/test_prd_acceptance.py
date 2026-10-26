"""PRD regression gates for transactions, geometry, units and dependency guards."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import simpy

from simulation.deadlock import cycle_components, observe_and_recover, resource_cycles
from simulation.energy import measure
from simulation.engine import SimulationEngine
from simulation.experiment_replay import read_experiment, replay_experiment
from simulation.experiments import Demand, Intervention, run_experiment
from simulation.path_graph import Route
from simulation.planning.backend import Backend
from simulation.planning.execution import commit, select_prefix
from simulation.planning.model import build_model
from simulation.planning.process import SolverProcess, solve_request
from simulation.planning.replay import export_result, read_snapshot
from simulation.planning.snapshot import capture
from simulation.planning.units import cost_ticks, decimal, gain_limit
from simulation.planning.validation import validate
from simulation.test_rh_planning import factory, small_snapshot
from simulation.test_rh_runtime import _sleeping_child, idle_engine
from utils.comparison_models import ComparisonConditions, calculate_economics
from utils.config_models import ElevatorConfig, RobotConfig, WorkStationConfig
from utils.event_types import RobotState


def test_assignment_exception_rolls_back_entire_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = idle_engine()
    task = engine.add_task("P", "S", 1)
    initial = asdict(task)
    request = capture(engine, engine.scheduler.settings, "transaction", (), set())
    prefix = select_prefix(solve_request(request, "cpsat"))
    original = engine.assign

    def fail_after_mutation(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise RuntimeError("injected after queue mutation")

    monkeypatch.setattr(engine, "assign", fail_after_mutation)
    assert not commit(engine, prefix)
    assert asdict(task) == initial and engine.pending_tasks == [task]
    assert not engine.scheduler.reservations and not engine.scheduler.busy_lifts
    assert not engine.robots["R"].planned_actions and engine.robots["R"].current_task is None
    assert not engine.robots["R"]._wake.triggered
    assert engine.task_events[-1]["event"] == "arrival"


def test_intersection_permission_precedes_arrival() -> None:
    cfg = factory()
    cfg.nodes[1].node_type = "intersection"
    engine = SimulationEngine(cfg, "baseline", comparison_seed=3)
    robot = engine.robots["R"]
    robot.x = robot.y = 0
    pool = engine.corridors.intersection_resource(2)
    blocker = pool.request()
    route = Route([(10, 0)], 10, [1], [None], [2], [1])
    process = engine.env.process(robot._travel_route(route))
    engine.env.run(until=2)
    assert robot.position() == (0, 0) and not process.triggered
    pool.release(blocker)
    engine.env.run(until=5)
    assert robot.position() == (10, 0)


def test_actual_wait_for_cycle_is_distinct_from_long_wait() -> None:
    env = simpy.Environment()
    a, b = simpy.Resource(env), simpy.Resource(env)

    def hold(first: simpy.Resource, second: simpy.Resource):
        with first.request() as one:
            yield one
            yield env.timeout(.1)
            with second.request() as two:
                yield two

    one, two = env.process(hold(a, b)), env.process(hold(b, a))
    env.run(until=1)
    assert resource_cycles((a, b), {one: "R0", two: "R1"}) == (("R0", "R1"),)
    assert not cycle_components({"R0": {"R1"}, "R1": set()})


@pytest.mark.parametrize("step", [.5, 1, 2])
def test_fractional_grid_parity_and_physical_feasibility(step: float) -> None:
    source = small_snapshot(jobs=1, horizon=8)
    snapshot = replace(source, settings=replace(source.settings, step=step, horizon=8.5, interval=2))
    results = [Backend(kind).solve(snapshot) for kind in ("milp", "cpsat")]
    for result in results:
        assert result.report.status == "OPTIMAL", result.report
        assert not validate(result)
        assert max(a.end for a in result.actions) == {.5: 8.5, 1: 8, 2: 8}[step]
    assert results[0].report.model_hash == results[1].report.model_hash
    assert results[0].report.phase2_cost == results[1].report.phase2_cost


def test_decimal_boundaries_and_nonzero_cost_preservation() -> None:
    assert gain_limit(1, 2, .0015) == 3
    assert gain_limit(1000, .5) == 500
    assert cost_ticks(decimal("0.00000001")) == 1
    assert cost_ticks(decimal("0.0000005")) == 1


def test_runtime_budget_does_not_change_mathematical_hash() -> None:
    snapshot = small_snapshot()
    first = build_model(snapshot)
    second = build_model(replace(snapshot, token="other", settings=replace(snapshot.settings, timeout=2, watchdog=3)))
    assert first.fingerprint == second.fingerprint


def test_unknown_model_version_is_rejected(tmp_path: Path) -> None:
    result = Backend("cpsat").solve(small_snapshot())
    path = tmp_path / "saved.json"
    export_result(result, path)
    data = json.loads(path.read_text())
    data["snapshot"]["model_version"] = "unknown-future-model"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="incompatible model_version"):
        read_snapshot(path)


def test_wrong_route_floor_is_rejected_independently() -> None:
    snapshot = small_snapshot()
    corrupted = replace(snapshot, routes=tuple(replace(r, floors=(2,)) if r.destination == "S" else r
                                               for r in snapshot.routes))
    result = Backend("cpsat").solve(corrupted)
    assert result.report.status == "OPTIMAL"
    assert validate(result)


def test_unsupported_cabin_capacity_holds_with_explicit_reason() -> None:
    from utils.config_models import ElevatorConfig
    engine = idle_engine()
    engine.factory.elevators = [ElevatorConfig("E", "E", 0, 0, capacity=2)]
    engine.add_task("P", "S", 1)
    engine.env.run(until=3)
    assert engine.scheduler.last_result.report.status == "UNSUPPORTED"
    assert "CAPACITY" in engine.scheduler.last_result.report.message
    assert len(engine.pending_tasks) == 1


def test_fifo_cannot_assign_robot_through_impossible_width() -> None:
    cfg = factory()
    cfg.robots[0].width = 2
    engine = SimulationEngine(cfg, "baseline", comparison_seed=3)
    engine.robots["R"].x = engine.robots["R"].y = 0
    task = engine.add_task("P", "S", 1)
    engine.env.run(until=3)
    assert engine.robots["R"].current_task is None and task in engine.pending_tasks


def test_planned_wait_executes_without_assigning_future_jobs() -> None:
    from simulation.planning.contracts import Action, Arc
    engine = idle_engine()
    task = engine.add_task("P", "S", 1)
    request = capture(engine, engine.scheduler.settings, "leading-wait", (), set())
    result = solve_request(request, "cpsat")
    robot = result.snapshot.robots[0]
    waits = tuple(Action(Arc(robot.id, robot.location, robot.location, t, t + 1, "wait"),
                         result.snapshot.now + t, result.snapshot.now + t + 1) for t in range(3))
    horizon_end = result.snapshot.now + result.snapshot.settings.horizon
    shifted = tuple(replace(a, start=a.start + 3, end=min(a.end + 3, horizon_end))
                    for a in result.actions if a.start + 3 < horizon_end)
    result = replace(result, actions=waits + shifted)
    assert not validate(result)
    prefix = select_prefix(result)
    assert max(a.end for a in prefix.actions) == result.snapshot.now + engine.scheduler.settings.interval
    assert commit(engine, prefix)
    assert task in engine.pending_tasks and engine.robots["R"].current_task is None
    assert not engine.scheduler.reservations


def test_cached_path_entry_cannot_survive_changed_obstacles() -> None:
    from utils.config_models import ObstacleConfig
    engine = idle_engine()
    task = engine.add_task("P", "S", 1)
    assert engine.can_assign(task, engine.robots["R"])
    assert engine.can_assign(task, engine.robots["R"])
    engine.apply_config(replace(engine.factory, obstacles=[ObstacleConfig(1, "wall", 5, -10, 5, 10)]))
    assert not engine.can_assign(task, engine.robots["R"])


@pytest.mark.parametrize("kind", ["milp", "cpsat"])
def test_hint_changes_search_but_not_optimal_objectives(kind: str) -> None:
    snapshot = small_snapshot(jobs=1)
    hinted, plain = Backend(kind).solve(snapshot), Backend(kind, use_hint=False).solve(snapshot)
    assert hinted.report.status == plain.report.status == "OPTIMAL"
    assert hinted.report.model_hash == plain.report.model_hash
    assert hinted.report.phase1_cost == plain.report.phase1_cost
    assert hinted.report.phase2_cost == plain.report.phase2_cost
    assert hinted.report.phase1_proof == "NONNEGATIVE_ZERO_WITNESS"
    assert hinted.report.phase1_cost == hinted.report.phase1_bound == 0


def test_memory_watchdog_reaps_owned_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil
    engine = idle_engine()
    request = capture(engine, engine.scheduler.settings, "memory", (), set())
    process = SolverProcess("milp")
    context = mp.get_context("spawn")
    process.connection, child = context.Pipe()
    process.process = context.Process(target=_sleeping_child, args=(child,))
    process.process.start()
    child.close()
    process.pending = request
    process.started = time.monotonic()
    monkeypatch.setattr(psutil, "Process", lambda pid: SimpleNamespace(
        memory_info=lambda: SimpleNamespace(rss=2048 * 1024 * 1024)))
    try:
        result = process.poll()
        assert result.report.status == "MEMORY_LIMIT"
        assert process.process is None
    finally:
        process.close()


def test_missing_solver_has_explicit_unavailable_status(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = Backend("milp")

    def missing(*args: object, **kwargs: object) -> None:
        raise ImportError("ortools absent")

    monkeypatch.setattr(backend, "_mip", missing)
    result = backend.solve(small_snapshot())
    assert result.report.status == "UNAVAILABLE"
    assert "Install requirements" in result.report.message
    assert not result.actions


@pytest.mark.parametrize("mode", ["baseline_fifo", "rh_milp", "rh_cpsat"])
def test_cancel_during_work_never_requeues_or_completes(mode: str) -> None:
    engine = idle_engine(mode) if mode.startswith("rh_") else SimulationEngine(
        factory(), mode, params={"task_rate": 0}, comparison_seed=3)
    robot = engine.robots["R"]
    robot.x = robot.y = 0
    task = engine.add_task("P", "S", 1, external_uid="trace:0")
    for _ in range(100):
        if task.started_at is not None:
            break
        engine.env.run(until=engine.now + .1)
    assert task.started_at is not None
    assert engine.cancel_external_task("trace:0")
    engine.env.run(until=engine.now + 15)
    assert engine.metrics.total_completed == 0 and task not in engine.pending_tasks
    assert robot.current_task is None and engine.cancelled_tasks == 1
    assert abs(measure(engine)["energy_balance_error"]) < 1e-8
    engine.scheduler.close() if hasattr(engine.scheduler, "close") else None


def test_critical_arrival_invalidates_pending_without_losing_jobs() -> None:
    engine = idle_engine()
    first = engine.add_task("ordinary", "S", 3)
    engine.scheduler.pending = capture(engine, engine.scheduler.settings, "pending", (), set())
    critical = engine.add_task("urgent", "S", 0)
    assert engine.scheduler.pending is None
    assert engine.scheduler.cancelled_requests == 1
    assert engine.pending_tasks == [first, critical]


def test_independent_validator_rejects_wall_crossing() -> None:
    snapshot = small_snapshot()
    # The solver receives the same route; only the independent checker sees this wall.
    snapshot = replace(snapshot, obstacles=((1, "wall", .5, -10, .5, 10, False, "", 1),))
    result = Backend("cpsat").solve(snapshot)
    assert any("obstacle" in error for error in validate(result))


def test_robot_specific_economics_and_missing_conversion() -> None:
    conditions = ComparisonConditions(wh_per_battery_unit=3, electricity_per_kwh=100,
                                      robot_energy_profiles={"R0": [2, .5]}, revenue_per_task=100,
                                      movement_cost_per_m=2, ai_cost=5)
    result = calculate_economics(2, 5, 10, conditions, True, {"R0": 2, "R1": 3})
    assert result["energy_wh"] == pytest.approx(18)
    assert result["total_cost"] == pytest.approx(26.8)
    missing = replace(conditions, wh_per_battery_unit=None)
    assert calculate_economics(2, 5, 10, missing, True, {"R0": 2, "R1": 3})["energy_wh"] is None
    zero = replace(missing, robot_energy_profiles={"R0": [0, 1], "R1": [0, 1]})
    assert calculate_economics(2, 5, 10, zero, True, {"R0": 2, "R1": 3})["energy_wh"] == 0


def test_experiment_replay_preserves_events_metrics_and_hashes(tmp_path: Path) -> None:
    trace = (Demand(0, 0, "S", ("S",), "P", 1, 0),
             Demand(1, 25, "S", ("S",), "late", 1, 0))
    events = (Intervention(5, "station_active", "S", False), Intervention(7, "station_active", "S", True),
              Intervention(26, "cancel_task", "trace:1"))
    data = run_experiment(factory(), ComparisonConditions(duration=30, repeats=2),
                          ("baseline_fifo", "weighted_heuristic"), traces=(trace, trace), interventions=events)
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(data))
    replayed = replay_experiment(path)
    assert replayed["summaries"] == data["summaries"]
    assert replayed["trace_hashes"] == data["trace_hashes"]
    assert replayed["replay_source_matches"]
    for rows in replayed["samples"].values():
        for row in rows:
            assert row["conservation_error"] == 0
            assert row["wait_distributions"]["station_queue"]["sample_count"] >= 1
            assert row["wait_distributions"]["intersection_queue"]["p95"] is None
            assert len([e for e in row["planning_events"] if e["event"] == "external_intervention"]) == 3
    data["traces"][0][0]["noise"] = 9
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="trace hash mismatch"):
        read_experiment(path)


@pytest.mark.parametrize("changes", [{"at": -1}, {"noise": float("nan")}, {"allowed": ("missing",)}])
def test_malformed_external_trace_is_rejected(changes: dict) -> None:
    demand = replace(Demand(0, 0, "S", ("S",), "P", 1, 0), **changes)
    with pytest.raises(ValueError, match="invalid external demand"):
        run_experiment(factory(), ComparisonConditions(duration=30, repeats=2),
                       ("baseline_fifo",), traces=((demand,), (demand,)))


def test_cycle_recovery_releases_victim_through_resource_context() -> None:
    cfg = factory()
    cfg.robots.append(RobotConfig("R2"))
    cfg.stations.append(WorkStationConfig("S2", "S2", 20, 0))
    engine = SimulationEngine(cfg, "baseline", params={"task_rate": 0})
    a, b = (station.resource for station in engine.stations.values())
    interrupted = []

    def abba(robot: object, first: simpy.Resource, second: simpy.Resource):
        try:
            with first.request() as one:
                yield one
                yield engine.env.timeout(.1)
                robot._set_state(RobotState.WAITING)
                with second.request() as two:
                    yield two
        except simpy.Interrupt:
            interrupted.append(robot.robot_id)

    for robot, first, second in ((engine.robots["R"], a, b), (engine.robots["R2"], b, a)):
        robot.process = engine.env.process(abba(robot, first, second))
    engine.env.run(until=1)
    assert a.count == b.count == 1
    observe_and_recover(engine)
    engine.env.run(until=1.1)
    assert interrupted == ["R2"]
    assert a.count == b.count == 0
    assert engine.deadlock_events[-1]["recovery"]


def test_charge_removal_settles_partial_gain_and_releases_slot() -> None:
    engine = idle_engine()
    robot = engine.robots["R"]
    robot.battery = robot.initial_energy_units = 20
    engine.send_to_charge(robot)
    for _ in range(100):
        engine.env.run(until=engine.now + .1)
        if robot._charge_step is not None:
            break
    assert robot._charge_step is not None
    pool = engine.chargers["C"].resource
    before = robot.live_battery()
    engine.apply_config(replace(engine.factory, chargers=[]))
    engine.env.run(until=engine.now + 1)
    assert before <= robot.battery <= robot.capacity
    assert pool.count == 0 and robot._charge_step is None
    assert abs(measure(engine)["energy_balance_error"]) < 1e-8


def test_cancel_during_cabin_ride_finishes_without_teleport_or_early_release() -> None:
    cfg = factory()
    cfg.elevators = [ElevatorConfig("E", "E", 0, 0, travel_time=2)]
    cfg.profile.door_time_sec = 1
    cfg.profile.floor_count = 2
    engine = SimulationEngine(cfg, "baseline", params={"task_rate": 0})
    robot = engine.robots["R"]
    robot.x = robot.y = 0
    caught = []

    def ride():
        try:
            yield from robot._ride_process("E", 0, 0, 2)
        except simpy.Interrupt:
            caught.append(True)

    process = engine.env.process(ride())
    for _ in range(100):
        engine.env.run(until=engine.now + .1)
        if robot._riding is not None:
            break
    assert robot._riding is not None
    cabin = engine.elevators["E"]
    process.interrupt("cancel")
    engine.env.run(until=engine.now + .1)
    assert cabin.resource.count == 1 and not caught
    engine.env.run(until=engine.now + 10)
    assert caught and robot.floor == 2 and robot.position() == (0, 0)
    assert cabin.resource.count == 0 and not cabin.moving
