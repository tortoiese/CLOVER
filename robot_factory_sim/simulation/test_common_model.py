"""Independent regressions for energy, lifecycle, exogenous demand and physical units."""

from __future__ import annotations

import copy

import pytest

from database.seed_data import build_default_config
from simulation.audit import assert_invariants, energy_ledger
from simulation.engine import SimulationEngine
from simulation.plant import PlantSettings, RobotInitial
from simulation.robot import InterruptKind
from simulation.task_generator import TaskState
from simulation.test_traffic_rules import traffic_factory
from simulation.travel import estimate_trip
from utils.config_models import FactoryConfig
from utils.metrics import MetricsCollector


def at_work(soc: float) -> SimulationEngine:
    """Create one robot at its station before the first scheduling event."""
    cfg = traffic_factory()
    cfg.robots = cfg.robots[:1]
    cfg.robots[0].charge_threshold = 0
    cfg.stations[0].avg_process_time = 20
    cfg.stations[0].process_time_std = 0
    engine = SimulationEngine(
        cfg,
        "baseline",
        demand_source="disabled",
        plant=PlantSettings(0.05, 5, 15),
        initial_robots={"R1": RobotInitial(100, 100, soc_pct=soc)},
    )
    engine.env.run(until=0.01)
    engine.assign(engine.add_task("test", "B", 2), engine.robots["R1"])
    return engine


def test_work_depletion_holds_station_until_rescue_and_never_completes_free() -> None:
    engine = at_work(0.1)
    robot = engine.robots["R1"]
    engine.env.run(until=1)
    assert robot.live_battery() == pytest.approx(0.0505)
    assert_invariants(engine)
    engine.env.run(until=3)
    assert engine.metrics.total_completed == 0 and robot.work_energy_units == pytest.approx(0.1)
    assert robot._fault_holding and engine.stations["B"].resource.count == 1
    assert_invariants(engine)
    robot.interrupt(InterruptKind.RETIRE)
    engine.env.run(until=6)
    assert robot.alive and engine.stations["B"].resource.count == 1
    engine.env.run(until=8)
    assert not robot.alive and engine.stations["B"].resource.count == 0
    assert robot.rescue_energy_units == pytest.approx(15)
    assert engine.metrics.total_completed == 0
    assert_invariants(engine)


def test_exact_work_energy_can_complete_and_balances() -> None:
    engine = at_work(1)
    engine.env.run(until=20.02)
    assert engine.metrics.total_completed == 1
    assert engine.robots["R1"].work_energy_units == pytest.approx(1)
    assert list(engine.tasks.values())[0].status == TaskState.COMPLETED
    assert_invariants(engine)


def test_cancel_during_work_accounts_partial_energy_and_is_idempotent() -> None:
    engine = at_work(100)
    engine.env.run(until=1)
    task = next(iter(engine.tasks.values()))
    assert engine.cancel_task(task.external_uid)
    assert not engine.cancel_task(task.external_uid)
    engine.env.run(until=2)
    assert task.status == TaskState.CANCELLED and engine.metrics.total_completed == 0
    assert engine.robots["R1"].work_energy_units == pytest.approx(0.0495)
    assert_invariants(engine)


def test_duplicate_assignment_and_completion_cannot_duplicate_a_job() -> None:
    engine = at_work(100)
    task = next(iter(engine.tasks.values()))
    with pytest.raises(ValueError, match="already assigned"):
        engine.assign(task, engine.robots["R1"])
    engine.env.run(until=21)
    engine.complete_task(task, engine.robots["R1"])
    assert engine.metrics.total_completed == 1
    assert_invariants(engine)


def test_replaced_and_cancelled_deferred_preemptions_preserve_every_task() -> None:
    cfg = traffic_factory()
    cfg.robots = cfg.robots[:1]
    engine = SimulationEngine(cfg, "baseline", demand_source="disabled", initial_robots={"R1": RobotInitial(0, 100)})
    engine.add_task("original", "B", 2)
    engine.env.run(until=1)
    first, second = engine.add_task("first", "B", 0), engine.add_task("second", "B", 0)
    robot = engine.robots["R1"]
    assert engine.preempt(robot, first) and engine.preempt(robot, second)
    assert first.status == TaskState.QUEUED and second.status == TaskState.RESERVED
    assert_invariants(engine)
    assert engine.cancel_task(second.external_uid)
    engine.env.run(until=12)
    assert second.status == TaskState.CANCELLED
    assert_invariants(engine)


def test_zero_rate_and_external_mode_do_not_create_hidden_tasks() -> None:
    for source in ("stochastic", "external", "disabled"):
        engine = SimulationEngine(traffic_factory(), params={"task_rate": 0}, demand_source=source)
        engine.env.run(until=1000)
        assert not engine.tasks
        assert_invariants(engine)


def test_fault_overriding_a_deferred_preemption_returns_its_reserved_job() -> None:
    cfg = traffic_factory()
    cfg.robots = cfg.robots[:1]
    cfg.robots[0].charge_threshold = 0
    engine = SimulationEngine(
        cfg,
        "baseline",
        demand_source="disabled",
        plant=PlantSettings(0.05, 5, 15),
        initial_robots={"R1": RobotInitial(0, 100, soc_pct=0.6)},
    )
    engine.add_task("original", "B", 2)
    engine.env.run(until=1)
    urgent = engine.add_task("urgent", "B", 0)
    assert engine.preempt(engine.robots["R1"], urgent)
    engine.env.run(until=8)
    assert urgent.status == TaskState.QUEUED and urgent in engine.pending_tasks
    assert_invariants(engine)


def test_demo_cap_records_every_offered_and_rejected_job() -> None:
    cfg = traffic_factory()
    for station in cfg.stations:
        station.avg_process_time, station.process_time_std = 1000, 0
    engine = SimulationEngine(cfg, "baseline", seed=42, params={"task_rate": 2})
    engine.env.run(until=30)
    assert len(engine.tasks) == 65
    assert sum(t.status == TaskState.REJECTED for t in engine.tasks.values()) == 23
    assert all(t.terminal_at == t.created_at for t in engine.tasks.values() if t.status == TaskState.REJECTED)
    assert len(engine.pending_tasks) == 40
    assert_invariants(engine)


def test_incompatible_fallback_does_not_change_the_operation() -> None:
    cfg = traffic_factory()
    cfg.stations[1].task_type = "painting"
    engine = SimulationEngine(cfg, "optimized", demand_source="disabled")
    task = engine.add_task("assembly", "B", 2, ("A", "B"))
    assert task.allowed_stations == ("B",)
    changed = copy.deepcopy(cfg)
    changed.stations[0].is_active = False
    engine.apply_config(changed)
    engine.env.run(until=10)
    assert task.operation == "assembly" and task.status == TaskState.QUEUED
    assert engine.metrics.total_completed == 0
    assert_invariants(engine)


def test_capability_change_cannot_complete_the_old_operation() -> None:
    engine = at_work(100)
    engine.env.run(until=1)
    changed = copy.deepcopy(engine.factory)
    changed.stations[0].task_type = "painting"
    engine.apply_config(changed)
    engine.env.run(until=25)
    assert engine.metrics.total_completed == 0
    task = next(iter(engine.tasks.values()))
    assert task.status == TaskState.QUEUED and task.operation == "assembly"
    assert_invariants(engine)


def test_full_run_mean_is_not_the_last_500_samples() -> None:
    metrics = MetricsCollector()
    for i in range(600):
        metrics.record_wait(100 if i < 100 else 0)
    assert len(metrics.wait_times) == 500
    assert metrics.compute(1000, [])["avg_wait"] == pytest.approx(10000 / 600)
    assert metrics.sample_counts["arrival_to_start"] == 600


def test_no_station_offers_are_explicitly_rejected_and_conserved() -> None:
    engine = SimulationEngine(FactoryConfig(), params={"task_rate": 1})
    engine.env.run(until=20)
    assert engine.tasks and all(t.status == TaskState.REJECTED for t in engine.tasks.values())
    assert_invariants(engine)


def test_physical_estimate_matches_deterministic_multifloor_execution() -> None:
    cfg = build_default_config()
    rid = cfg.robots[0].id
    engine = SimulationEngine(cfg, demand_source="disabled", initial_robots={rid: RobotInitial(100, 150)})
    engine.env.run(until=0.01)
    robot = engine.robots[rid]
    estimate = estimate_trip(engine, robot, "WS-05")
    assert estimate.horizontal_m == pytest.approx(155)
    assert estimate.routing_cost_px * 0.1 > estimate.horizontal_m
    route = engine.path_graph.for_robot(robot.cfg.width).route_to_facility(100, 150, "WS-05", 1)
    process = engine.env.process(robot._travel_route(route))
    start = engine.now
    engine.env.run(until=process)
    assert engine.now - start == pytest.approx(estimate.seconds)
    assert robot.distance_m == pytest.approx(estimate.horizontal_m)
    assert energy_ledger(engine)["max_balance_error"] < 1e-8


def test_missing_charger_cannot_be_treated_as_zero_return_energy() -> None:
    cfg = traffic_factory()
    cfg.chargers = []
    engine = SimulationEngine(cfg, "optimized", demand_source="disabled")
    engine.add_task("test", "B", 2)
    engine.env.run(until=20)
    assert engine.metrics.total_completed == 0
    assert all(t.status == TaskState.QUEUED for t in engine.tasks.values())
    assert_invariants(engine)


def test_return_energy_starts_at_actual_facility_access_node() -> None:
    from simulation.evaluation.fixtures import examples

    scenario = examples()[0]
    # The station's centre differs from its explicit approach node. The robot
    # works at that node, so starting the return at the centre invents energy.
    scenario.factory.stations[0].x = 200
    engine = SimulationEngine(scenario.factory, "optimized", demand_source="disabled",
                              initial_robots={"R0": RobotInitial(200, 200, soc_pct=18),
                                              "R1": RobotInitial(200, 200)})
    robot = engine.robots["R0"]
    outward = estimate_trip(engine, robot, "S0").horizontal_m
    assert outward == pytest.approx(10)
    assert engine.scheduler._energy_needed(robot, engine, "S0", outward) == pytest.approx(10.4)
    engine.close()
