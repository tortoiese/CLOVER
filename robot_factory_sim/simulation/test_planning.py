"""MILP constraints, live execution, interruption and asynchronous-result tests."""

from __future__ import annotations

import math
from dataclasses import replace
from itertools import pairwise

import pytest

from simulation.engine import SimulationEngine
from simulation.planning.milp_backend import MilpBackend
from simulation.planning.models import (
    ChargerInput,
    JobInput,
    PlanningBackend,
    Reservation,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    SolveReport,
    SolveResult,
    StationInput,
)
from simulation.planning.plan_validator import capacity_errors, validate_plan
from simulation.planning.reservation_table import ReservationTable
from simulation.planning.snapshot_builder import build_snapshot
from simulation.robot import InterruptKind
from simulation.station import ChargingStation
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)


def toy(*, count: int = 2, battery: float = 10.0, horizon: float = 6.0, capacity: int = 1) -> Snapshot:
    """Create a small independently interpretable charging/work scheduling problem."""
    return Snapshot(
        "toy",
        0,
        0.0,
        Settings(horizon=horizon, timeout=3.0, sigma=0.0),
        tuple(RobotInput(f"r{i}", "C", 0, battery, 10, 1, 1, 1) for i in range(count)),
        (JobInput(1, ("S",), 0, None, 1), JobInput(2, ("S",), 0, None, 1)),
        (StationInput("S", "S", 2),),
        (ChargerInput("C", "C", capacity, 2),),
        (RouteInput("C", "S", ((1.0, 0.0),), 1), RouteInput("S", "C", ((0.0, 0.0),), 1)),
        (),
        1.0,
        0.5,
    )


def solved(snapshot: Snapshot):
    """Require a solution and validate its independent operational meaning."""
    result = MilpBackend().solve(snapshot)
    assert result.plan is not None, result.report
    assert result.report.status == "OPTIMAL", result.report
    assert not validate_plan(result.plan)
    return result.plan


def factory(robots: int = 2, capacity: int = 1, std: float = 0.0) -> FactoryConfig:
    """Create a deterministic, one-metre route without GUI or database access."""
    return FactoryConfig(
        stations=[WorkStationConfig("S", "work", 10, 0, avg_process_time=2, process_time_std=std)],
        chargers=[ChargingStationConfig("C", 0, 0, capacity=capacity, charge_rate=2)],
        robots=[RobotConfig(f"r{i}", battery_capacity=10, move_speed=1, battery_drain_rate=1) for i in range(robots)],
        nodes=[PathNodeConfig(1, 0, 0, "charger", "C"), PathNodeConfig(2, 10, 0, "station", "S")],
        edges=[PathEdgeConfig(1, 1, 2)],
    )


def live(robots: int = 2, **kwargs) -> SimulationEngine:
    """Start an engine with its random arrival generator disabled."""
    engine = SimulationEngine(
        factory(robots),
        "milp_rh",
        params={
            "background_enabled": 0.0,
            "planning_horizon": 12.0,
            "planning_interval": 2.0,
            "planning_timeout": 3.0,
            "planning_sigma": 0.0,
            **kwargs,
        },
    )
    for robot in engine.robot_list():
        robot.x = robot.y = 0
    engine.env.run(until=0.01)
    return engine


class BrokenBackend(PlanningBackend):
    """Simulate a missing solver or a solve that never produced an incumbent."""

    def solve(self, snapshot: Snapshot) -> SolveResult:
        """Return an explicit failure so the live fallback path is exercised."""
        return SolveResult(snapshot.token, SolveReport("NOT_SOLVED", "broken", 0.0))


def test_station_capacity_and_sequential_robot() -> None:
    plan = solved(toy(count=1))
    work = [a for a in plan.actions if a.kind == "work"]
    assert len(work) == 2
    assert work[0].end <= work[1].start


def test_charge_before_work_and_no_parallel_behaviour() -> None:
    plan = solved(toy(battery=1, horizon=8))
    assert not plan.deferred
    for rid in {a.robot_id for a in plan.actions if a.kind == "work"}:
        actions = [a for a in plan.actions if a.robot_id == rid]
        assert next(a for a in actions if a.kind != "wait").kind == "charge"


def test_committed_station_blocks_early_start() -> None:
    snap = replace(toy(), fixed=(Reservation("S", "old", 0, 4, "work"),))
    plan = solved(snap)
    assert len(plan.deferred) == 1
    assert min(a.start for a in plan.actions if a.kind == "work") >= 4


def test_committed_charger_blocks_charging() -> None:
    snap = replace(
        toy(count=1, battery=1, horizon=8), jobs=toy().jobs[:1], fixed=(Reservation("C", "old", 0, 3, "charge"),)
    )
    plan = solved(snap)
    assert min(a.start for a in plan.actions if a.kind == "charge" and a.gain > 1e-5) >= 3


def test_unreachable_job_is_deferred() -> None:
    plan = solved(replace(toy(count=1), routes=(), jobs=toy().jobs[:1]))
    assert plan.deferred == (1,)
    assert all(a.kind != "work" for a in plan.actions)


def test_alternative_station() -> None:
    snap = toy(count=1, horizon=5)
    snap = replace(
        snap,
        jobs=(JobInput(1, ("S", "T"), 0, None, 1),),
        stations=(*snap.stations, StationInput("T", "T", 2)),
        routes=(*snap.routes, RouteInput("C", "T", ((0.0, 1.0),), 1), RouteInput("T", "C", ((0.0, 0.0),), 1)),
        fixed=(Reservation("S", "old", 0, 5, "work"),),
    )
    plan = solved(snap)
    assert next(a for a in plan.actions if a.kind == "work").resource == "T"


def test_robot_ready_time() -> None:
    snap = toy(count=1, horizon=7)
    snap = replace(snap, robots=(replace(snap.robots[0], ready=3),), jobs=snap.jobs[:1])
    plan = solved(snap)
    assert min(a.start for a in plan.actions) >= 3


def test_initial_energy_and_terminal_return() -> None:
    snap = toy(count=1, horizon=2)
    bad = replace(snap, robots=(replace(snap.robots[0], battery=0),))
    assert MilpBackend().solve(bad).plan is None
    stranded = replace(snap, robots=(replace(snap.robots[0], location="S", battery=2),), jobs=snap.jobs[:1])
    assert solved(stranded).deferred == (1,)


@pytest.mark.parametrize(("capacity", "done"), [(1, 1), (2, 2)])
def test_charger_capacity_changes_feasible_throughput(capacity: int, done: int) -> None:
    snap = toy(battery=1, horizon=5, capacity=capacity)
    snap = replace(
        snap,
        jobs=(JobInput(1, ("S",), 0, None, 1), JobInput(2, ("T",), 0, None, 1)),
        stations=(*snap.stations, StationInput("T", "T", 2)),
        routes=(*snap.routes, RouteInput("C", "T", ((0.0, 1.0),), 1), RouteInput("T", "C", ((0.0, 0.0),), 1)),
    )
    assert 2 - len(solved(snap).deferred) == done


def test_validator_rejects_teleport_and_fake_battery() -> None:
    plan = solved(toy(count=1))
    bad = replace(plan, actions=(replace(plan.actions[0], energy=-10), *plan.actions[1:]))
    assert validate_plan(bad)
    bad = replace(plan, actions=(replace(plan.actions[0], origin="nowhere"), *plan.actions[1:]))
    assert validate_plan(bad)


def test_half_open_reservation_boundaries() -> None:
    reservations = (Reservation("S", "a", 0, 2, "work"), Reservation("S", "b", 2, 3, "work"))
    assert not capacity_errors(reservations, {"S": 1})


def test_live_milp_completes_work_and_reserves_prefix() -> None:
    engine = live()
    for _ in range(4):
        engine.add_task("test", "S", 2)
    engine.env.run(until=1)
    committed = [r for r in engine.robot_list() if r.planned_actions]
    assert committed
    assert engine.scheduler.stats.accepted > 0
    engine.env.run(until=40)
    assert engine.metrics.total_completed == 4
    assert not engine.pending_tasks
    assert not engine.scheduler.table.reservations()
    assert engine.scheduler.stats.invalid == 0


def test_live_low_battery_charges_without_depleting() -> None:
    engine = live()
    for robot in engine.robot_list():
        robot.battery = 1
    for _ in range(3):
        engine.add_task("test", "S", 2)
    engine.env.run(until=50)
    assert engine.metrics.total_completed == 3
    assert sum(r.charge_sessions for r in engine.robot_list()) > 0
    assert all(not r._depleted for r in engine.robot_list())


def test_solver_failure_falls_back_through_reservations() -> None:
    engine = live()
    engine.scheduler.backend = BrokenBackend()
    for _ in range(3):
        engine.add_task("test", "S", 0)
    engine.env.run(until=40)
    assert engine.metrics.total_completed == 3
    assert engine.scheduler.stats.fallbacks > 0
    assert not engine.scheduler.table.reservations()


def test_overrun_uses_actual_resource_and_replans() -> None:
    engine = live()
    engine.sample_process_time = lambda station, task: 5.0
    starts = []
    original = engine.stations["S"].begin

    def begin(task, robot_id):
        assert engine.stations["S"].current_task is None
        starts.append(engine.now)
        original(task, robot_id)

    engine.stations["S"].begin = begin
    for _ in range(3):
        engine.add_task("test", "S", 2)
    engine.env.run(until=70)
    assert engine.metrics.total_completed == 3
    assert all(b - a >= 5 - 1e-6 for a, b in pairwise(starts))
    assert engine.scheduler.stats.overruns > 0


def test_interruption_releases_and_requeues_once() -> None:
    engine = live(robots=1)
    task = engine.add_task("test", "S", 2)
    engine.env.run(until=0.6)
    robot = engine.robot_list()[0]
    assert robot.planned_actions
    assert robot.interrupt(InterruptKind.PREEMPT)
    engine.env.run(until=0.7)
    assert not robot.planned_actions
    assert not engine.scheduler.table.reservations()
    assert sum(t is task for t in engine.pending_tasks) == 1
    engine.env.run(until=30)
    assert engine.metrics.total_completed == 1


def test_job_cancelled_during_planned_wait_is_not_started() -> None:
    from utils.config_models import OrderRecord
    from utils.event_types import OrderStatus

    engine = live(robots=1)
    epoch = engine.epoch
    order = OrderRecord("O", "p", 1, required_stations=["S"], created_at=epoch)
    engine.sync_orders([order])
    engine.env.run(until=1.1)
    engine.sync_orders([replace(order, status=OrderStatus.CANCELLED)])
    engine.env.run(until=20)
    assert engine.orders["O"].completed == 0
    assert all(r.current_task is None for r in engine.robot_list())


def test_async_stale_result_does_not_modify_new_layout() -> None:
    engine = live()
    requests = []
    engine.scheduler.set_submitter(requests.append)
    engine.add_task("test", "S", 2)
    engine.env.run(until=1)
    assert requests
    result = MilpBackend().solve(requests[0])
    modified = replace(engine.factory, stations=[replace(engine.factory.stations[0], is_active=False)])
    engine.apply_config(modified)
    engine.scheduler.deliver(result)
    engine.env.run(until=2)
    assert engine.scheduler.stats.stale > 0
    assert engine.stations["S"].processed == 0


def test_duplicate_async_result_is_ignored() -> None:
    engine = live()
    requests = []
    engine.scheduler.set_submitter(requests.append)
    engine.add_task("test", "S", 2)
    engine.env.run(until=0.6)
    result = MilpBackend().solve(requests[0])
    engine.scheduler.deliver(result)
    engine.env.run(until=1.1)
    accepted = engine.scheduler.stats.accepted
    engine.scheduler.deliver(result)
    assert engine.scheduler.stats.accepted == accepted
    assert engine.scheduler.stats.stale > 0


def test_snapshot_uses_live_energy_units_and_current_order_deadline() -> None:
    from datetime import timedelta

    from utils.config_models import OrderRecord

    engine = live(robots=1)
    robot = engine.robot_list()[0]
    robot.cfg = replace(robot.cfg, battery_capacity=200)
    robot.battery = 50
    order = OrderRecord("O", "p", 2, created_at=engine.epoch, deadline=engine.epoch + timedelta(seconds=30))
    engine.sync_orders([order])
    engine.release_order_tasks()
    engine.sync_orders([replace(order, deadline=engine.epoch + timedelta(seconds=60))])
    snap = build_snapshot(engine, Settings(), ReservationTable(), "x")
    assert snap.robots[0].battery == 50
    assert snap.robots[0].reserve == 20
    assert all(j.deadline == 60 for j in snap.jobs)


def test_charger_resize_preserves_occupants_and_queue() -> None:
    import simpy

    env = simpy.Environment()
    charger = ChargingStation(env, ChargingStationConfig("C", 0, 0, capacity=2))
    first, second, third = [charger.resource.request() for _ in range(3)]
    old = charger.resource
    charger.update_config(replace(charger.cfg, capacity=1))
    assert charger.resource is old
    assert old.count == 2 and not third.triggered
    old.release(first)
    env.run(until=0.1)
    assert old.count == 1 and not third.triggered
    old.release(second)
    env.run(until=0.2)
    assert third.triggered and old.count == 1


@pytest.mark.parametrize("mode", ["baseline", "optimized"])
def test_original_schedulers_still_run(mode: str) -> None:
    engine = SimulationEngine(factory(1), mode, params={"background_enabled": 0.0})
    engine.env.run(until=0.01)
    engine.add_task("test", "S", 2)
    engine.env.run(until=30)
    assert engine.metrics.total_completed == 1


def test_bad_planning_parameters_are_rejected() -> None:
    engine = live()
    with pytest.raises(ValueError):
        engine.update_param("planning_step", math.nan)
    with pytest.raises(ValueError):
        engine.update_param("planning_max_tasks", 2.5)


def test_phase_one_only_is_not_claimed_fully_optimal(monkeypatch) -> None:
    from types import SimpleNamespace

    from simulation.planning import milp_backend

    values = iter((0.0, 0.0, 0.0, 100.0, 100.0))
    monkeypatch.setattr(milp_backend, "time", SimpleNamespace(monotonic=lambda: next(values, 100.0)))
    result = MilpBackend().solve(toy(count=1))
    assert result.plan is not None
    assert result.report.phase1_status == "OPTIMAL"
    assert result.report.phase2_status == "NOT_SOLVED"
    assert result.report.status == "FEASIBLE"


def test_async_timely_result_is_shifted_and_not_claimed_current_optimal() -> None:
    engine = live(robots=1)
    requests = []
    engine.scheduler.set_submitter(requests.append)
    engine.add_task("test", "S", 2)
    engine.env.run(until=0.6)
    result = MilpBackend().solve(requests[0])
    engine.scheduler.deliver(result)
    engine.env.run(until=1.1)
    assert engine.scheduler.stats.accepted == 1
    assert engine.scheduler.planning_state()["execution_status"] == "SHIFTED_FEASIBLE"
    assert engine.robot_list()[0].planned_actions[0].start >= 1.0


def test_changed_dedication_rejects_unstarted_work() -> None:
    engine = live(robots=1)
    task = engine.add_task("test", "S", 2)
    engine.env.run(until=0.6)
    engine.stations["S"].dedicated_order = "another-order"
    engine.env.run(until=5)
    assert engine.metrics.total_completed == 0
    assert sum(t is task for t in engine.pending_tasks) == 1


def test_planned_charging_can_be_recalled_and_releases_slot() -> None:
    engine = live(robots=1)
    robot = engine.robot_list()[0]
    robot.battery = 1
    engine.add_task("test", "S", 2)
    engine.env.run(until=0.6)
    assert robot.planned_actions
    assert robot.interrupt(InterruptKind.RECALL)
    engine.env.run(until=0.7)
    assert not engine.scheduler.table.reservations()
    assert not robot.planned_actions
    engine.env.run(until=40)
    assert engine.metrics.total_completed == 1


def test_task_keyed_draw_is_independent_of_call_order() -> None:
    from simulation.task_generator import Task

    a = SimulationEngine(factory(std=1), "baseline", seed=123, params={"background_enabled": 0.0})
    b = SimulationEngine(factory(std=1), "optimized", seed=123, params={"background_enabled": 0.0})
    a.trace_processing = b.trace_processing = True
    first, other = Task(1, "S", "p", 2, 0), Task(2, "S", "p", 2, 0)
    expected = a.sample_process_time(a.stations["S"], first)
    b.sample_process_time(b.stations["S"], other)
    assert b.sample_process_time(b.stations["S"], first) == expected


def test_legacy_charge_target_cannot_cause_zero_time_reentry() -> None:
    from simulation.scheduler import OptimizedScheduler

    engine = SimulationEngine(factory(1), "optimized", params={"background_enabled": 0.0})
    robot = engine.robot_list()[0]
    robot.cfg = replace(robot.cfg, charge_threshold=95)
    engine.update_param("min_charge_mode", 1)
    assert isinstance(engine.scheduler, OptimizedScheduler)
    assert engine.scheduler.charge_target(robot, engine) == 95
    robot.battery = robot.capacity
    robot.cfg = replace(robot.cfg, charge_threshold=120)
    assert not engine.scheduler.should_charge(robot, engine)


def test_missing_solver_package_keeps_legacy_modes_importable(monkeypatch) -> None:
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("ortools"):
            raise ImportError("simulated missing solver package")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    result = MilpBackend().solve(toy(count=1))
    assert result.plan is None and result.report.status == "ERROR"
    engine = SimulationEngine(factory(1), "baseline", params={"background_enabled": 0.0})
    engine.env.run(until=1)


def test_no_solve_after_arc_budget_overflow() -> None:
    snap = replace(toy(count=1), settings=replace(toy().settings, max_arcs=1))
    result = MilpBackend().solve(snap)
    assert result.plan is None
    assert "arc budget" in result.report.message.lower()


@pytest.mark.parametrize("battery", [1.0, 10.0])
def test_greedy_incumbent_independently_satisfies_model(battery: float) -> None:
    from simulation.planning.models import Action, Plan
    from simulation.planning.problem_builder import build_problem
    from simulation.planning.warm_start import greedy_hint

    snap = toy(battery=battery, horizon=8)
    problem = build_problem(snap)
    hint = greedy_hint(problem)
    assert hint is not None
    selected, gains, completed = hint
    balances = {r.id: r.battery for r in snap.robots}
    routes = {(r.origin, r.destination): r for r in snap.routes}
    actions = []
    for i in sorted(selected, key=lambda n: (problem.arcs[n].robot_id, problem.arcs[n].start)):
        a = problem.arcs[i]
        if a.kind == "end":
            continue
        gain = gains.get(i, 0.0)
        balances[a.robot_id] += gain - a.energy
        route = routes.get((a.origin, a.destination))
        actions.append(
            Action(
                a.robot_id,
                a.kind,
                a.start,
                a.end,
                a.origin,
                a.destination,
                a.resource,
                a.job_id,
                a.energy,
                gain,
                a.distance,
                route.points if a.kind == "move" and route else (),
                balances[a.robot_id],
            )
        )
    plan = Plan(
        snap,
        tuple(actions),
        tuple(j.id for j in snap.jobs if j.id not in completed),
        SolveReport("FEASIBLE", "greedy_hint", 0.0),
    )
    assert not validate_plan(plan)
