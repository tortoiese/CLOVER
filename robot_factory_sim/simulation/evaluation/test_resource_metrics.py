"""Independent queue-exposure oracles and actual plant observations at cutoff."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import asdict
from typing import Any

import pytest
import simpy

from simulation.audit import assert_invariants
from simulation.elevator import Elevator
from simulation.engine import SimulationEngine
from simulation.evaluation.digest import PhysicalDigest
from simulation.evaluation.fixtures import examples, joint_batch
from simulation.evaluation.resource_metrics import ResourceMetrics
from simulation.evaluation.runner import replay, run_case
from simulation.ledger import EventLedger
from simulation.planning.backend import Backend
from simulation.planning.contracts import Settings
from simulation.planning.execution import commit
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_snapshot import initialize
from simulation.plant import RobotInitial
from utils.config_models import ElevatorConfig


def test_hand_calculated_window_exposure_and_censored_cohort() -> None:
    metrics = ResourceMetrics(5, 10, 3)
    events = []
    for identity, start, end, outcome in (
        ("a", 0, 7, "granted"),
        ("b", 2, None, None),
        ("c", 6, 8.5, "withdrawn"),
        ("d", 7, 9, "granted"),
    ):
        row = {"wait_id": identity, "robot": identity, "robot_instance": 0, "channel": "charge", "resource": "charge:C"}
        events.append({**row, "event": "resource_wait_start", "time": start})
        if end is not None:
            events.append(
                {**row, "event": "resource_wait_end", "time": end, "outcome": outcome, "elapsed": end - start}
            )
    for event in sorted(events, key=lambda r: r["time"]):
        metrics.record(event)
    result = metrics.waits_snapshot(10)
    stats = result["total"]
    assert stats["queue_robot_seconds"] == 11.5 and stats["mean_queue_length"] == 2.3
    assert stats["carry_in_episodes"] == 2 and stats["active_at_end"] == 1
    assert stats["cohort_episodes"] == 2 and stats["cohort_right_censored"] == 0
    assert stats["granted_wait"]["mean"] == 2 and stats["withdrawn_elapsed"]["mean"] == 2.5
    assert stats["active_over_threshold"] == 1 and result["open_episodes"][0]["age"] == 8


def station_queue(warmup: float = 0) -> tuple[SimulationEngine, ResourceMetrics]:
    """Put two robots at the same station; the first job lasts 1000 seconds."""
    scenario = examples()[0]
    scenario.factory.stations[0].avg_process_time = 1000
    scenario.factory.stations[0].process_time_std = 0
    engine = SimulationEngine(
        scenario.factory, demand_source="disabled", initial_robots={rid: RobotInitial(100, 200) for rid in ("R0", "R1")}
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    metrics = ResourceMetrics(warmup, 150, 120)
    engine.ledger.sink = metrics.record
    for i, rid in enumerate(("R0", "R1")):
        task = engine.add_task("inspection", "S0", 2, external_uid=f"j{i}")
        task.process_noise = 0
        assert engine.assign(task, engine.robots[rid])
    return engine, metrics


def test_end_of_run_long_wait_is_visible_when_completed_wait_mean_is_zero() -> None:
    engine, metrics = station_queue()
    engine.env.run(until=150)
    data = metrics.waits_snapshot(engine.now)
    stats = data["by_channel"]["station"]
    assert stats["granted_wait"]["mean"] == 0
    assert stats["cohort_right_censored"] == stats["active_over_threshold"] == 1
    assert stats["right_censored_ages"]["max"] == 150 and stats["queue_robot_seconds"] == 150
    assert data["open_episodes"][0]["robot"] == "R1"
    assert_invariants(engine)
    engine.close()
    # Cleanup at the ownership boundary does not retroactively close the saved snapshot.
    assert stats["cohort_right_censored"] == 1


def test_wait_before_warmup_contributes_exposure_without_becoming_new_cohort() -> None:
    engine, metrics = station_queue(50)
    engine.env.run(until=150)
    stats = metrics.waits_snapshot(engine.now)["by_channel"]["station"]
    assert stats["carry_in_episodes"] == 1 and stats["cohort_episodes"] == 0
    assert stats["queue_robot_seconds"] == 100 and stats["mean_queue_length"] == 1
    assert stats["granted_wait"]["mean"] is None
    engine.close()


def test_cancel_pending_request_is_withdrawn_not_granted_or_right_censored() -> None:
    engine, metrics = station_queue()
    engine.env.run(until=10)
    assert engine.cancel_task("j1")
    engine.env.run(until=11)
    stats = metrics.waits_snapshot(engine.now)["by_channel"]["station"]
    assert stats["cohort_withdrawn"] == 1 and stats["withdrawn_elapsed"]["mean"] == 10
    assert stats["cohort_granted"] == 1 and stats["cohort_right_censored"] == 0
    assert stats["queue_robot_seconds"] == 10
    assert_invariants(engine)
    engine.close()


def test_opposite_traffic_queue_is_one_atomic_wait_not_one_sample_per_claim() -> None:
    scenario = examples()[2]
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    metrics = ResourceMetrics(0, 5, 120)
    engine.ledger.sink = metrics.record
    for rid, station in (("R0", "S1"), ("R1", "S0")):
        assert engine.assign(engine.add_task("inspection", station, 2), engine.robots[rid])
    engine.env.run(until=5)
    data = metrics.waits_snapshot(5)
    assert data["by_channel"]["corridor"]["cohort_right_censored"] == 1
    row = next(r for r in data["open_episodes"] if r["channel"] == "corridor")
    assert row["demands"][0]["unit"] == "mm" and row["age"] == 5
    assert_invariants(engine)
    engine.close()


def test_charging_queue_records_active_wait() -> None:
    scenario = examples()[0]
    for robot in scenario.factory.robots:
        robot.charge_threshold = 0
    scenario.factory.chargers[0].charge_rate = 0.01
    scenario.factory.chargers[0].capacity = 1
    engine = SimulationEngine(
        scenario.factory,
        demand_source="disabled",
        initial_robots={rid: RobotInitial(200, 200, soc_pct=10) for rid in ("R0", "R1")},
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    metrics = ResourceMetrics(0, 10, 120)
    engine.ledger.sink = metrics.record
    for robot in engine.robots.values():
        engine.send_to_charge(robot)
    engine.env.run(until=10)
    assert metrics.waits_snapshot(10)["by_channel"]["charge"]["cohort_right_censored"] == 1
    assert_invariants(engine)
    engine.close()


def test_rh_charge_executor_uses_same_queue_observation() -> None:
    scenario = examples()[0]
    scenario.factory.chargers[0].capacity = 1
    scenario.factory.robots = scenario.factory.robots[:1]
    scenario.factory.robots[0].battery_capacity = 30
    scenario.factory.robots[0].charge_threshold = 0
    engine = SimulationEngine(
        scenario.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(200, 200, soc_pct=10)}
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S0", 2)
    result = Backend().solve(
        prepare(capture(engine, Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7), "charge"))
    )
    assert commit(engine, result)[0]
    blocker = engine.chargers["C"].resource.request()
    metrics = ResourceMetrics(0, 5, 120)
    engine.ledger.sink = metrics.record
    engine.env.run(until=5)
    assert metrics.waits_snapshot(5)["by_channel"]["charge"]["cohort_right_censored"] == 1
    assert_invariants(engine)
    engine.chargers["C"].resource.release(blocker)
    engine.close()


def cabin_metrics(warmup: float = 0) -> tuple[simpy.Environment, Elevator, ResourceMetrics]:
    """Use one known 4-second floor movement and 3-second half door cycle."""
    env = simpy.Environment()
    metrics = ResourceMetrics(warmup, 20, 120)
    ledger = EventLedger(lambda: float(env.now), metrics.record)
    elevator = Elevator(env, ElevatorConfig("E", "E", 0, 0, travel_time=4), door_time=6, observer=ledger.emit)
    elevator._floor = 2.0
    return env, elevator, metrics


@pytest.mark.parametrize("warmup,cutoff,seconds,distance", ((0, 2, 2, 0.5), (1, 3, 2, 0.5), (10, 3, 0, 0)))
def test_partial_empty_cabin_motion_is_exact_and_window_clipped(
    warmup: float, cutoff: float, seconds: float, distance: float
) -> None:
    env, elevator, metrics = cabin_metrics(warmup)
    process = env.process(elevator.arrive(1))
    env.run(until=cutoff)
    result = metrics.cabins_snapshot(cutoff)
    stats = result["totals"]["empty_move"]
    assert stats["seconds"] == seconds and stats["distance_floors"] == distance
    assert result["active_at_end"][0]["current_floor"] == elevator.floor_position()
    assert result["window"][0] <= result["window"][1]
    process._generator.close()


def test_empty_loaded_and_door_phases_do_not_overlap_or_double_count() -> None:
    env, elevator, metrics = cabin_metrics()

    def trip() -> Generator[Any, Any, None]:
        yield from elevator.arrive(1)
        yield from elevator.carry(2)

    env.run(until=env.process(trip()))
    assert env.now == 14 and elevator.rides == 1
    result = metrics.cabins_snapshot(14)
    assert {kind: r["seconds"] for kind, r in result["totals"].items()} == {
        "empty_move": 4,
        "boarding": 3,
        "loaded_move": 4,
        "alighting": 3,
    }
    assert result["empty_distance_fraction"] == 0.5 and not result["active_at_end"]


def test_interrupted_motion_records_actual_partial_distance() -> None:
    env, elevator, metrics = cabin_metrics()

    def move() -> Generator[Any, Any, None]:
        try:
            yield from elevator._move(1, "empty_move")
        except simpy.Interrupt:
            pass

    process = env.process(move())
    env.run(until=2)
    process.interrupt()
    env.run(until=process)
    stats = metrics.cabins_snapshot(2)["totals"]["empty_move"]
    assert elevator.floor_position() == 1.5
    assert stats["interrupted"] == 1 and stats["completed"] == 0
    assert stats["seconds"] == 2 and stats["distance_floors"] == 0.5


def test_cabin_queue_and_empty_relocation_are_observed_separately() -> None:
    scenario = examples()[5]
    scenario.factory.elevators[0].travel_time = 1000
    engine = SimulationEngine(
        scenario.factory,
        demand_source="disabled",
        initial_cabins={"E": 2},
        initial_robots={rid: RobotInitial(100, 200) for rid in ("R0", "R1")},
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    metrics = ResourceMetrics(0, 150, 120)
    engine.ledger.sink = metrics.record
    for rid in ("R0", "R1"):
        assert engine.assign(engine.add_task("inspection", "S1", 2), engine.robots[rid])
    engine.env.run(until=150)
    assert metrics.waits_snapshot(150)["by_channel"]["cabin"]["cohort_right_censored"] == 1
    cabins = metrics.cabins_snapshot(150)
    assert cabins["totals"]["empty_move"]["active"] == 1
    assert cabins["active_at_end"][0]["current_floor"] == pytest.approx(engine.elevators["E"].floor_position())
    assert_invariants(engine)
    engine.close()


@pytest.mark.parametrize("policy", ("optimized", "rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_interval"))
def test_saved_observation_metrics_match_after_physical_replay(policy: str) -> None:
    result = run_case(
        joint_batch(),
        policy,
        17,
        planning_settings=asdict(Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7))
        if policy != "optimized"
        else None,
    )
    assert result["status"] == "SUCCESS", result["failure"]
    repeated = replay(result)
    assert repeated["matches"]
    assert repeated["result"]["resource_waits"] == result["resource_waits"]
    assert repeated["result"]["cabin_phases"] == result["cabin_phases"]


def test_projection_ignores_only_added_measurements_and_wall_timing() -> None:
    original = [
        {"event": "task_offered", "sequence": 0, "time": 0, "uid": "j"},
        {"event": "dispatch", "sequence": 1, "time": 1, "wall_seconds": 0.1},
    ]
    changed = [
        original[0],
        {"event": "resource_wait_start", "sequence": 1, "time": 0.5},
        {**original[1], "sequence": 2, "wall_seconds": 20},
    ]
    first, second = PhysicalDigest(), PhysicalDigest()
    for row in original:
        first.record(row)
    for row in changed:
        second.record(row)
    assert first.hexdigest() == second.hexdigest()


def test_zero_time_movement_reports_actual_floor_before_timeout_is_processed() -> None:
    env, elevator, metrics = cabin_metrics()
    elevator.cfg.travel_time = 0
    process = env.process(elevator._move(1))
    env.step()
    pending = metrics.cabins_snapshot(0)["active_at_end"][0]
    assert pending["current_floor"] == elevator.floor_position() == 1
    env.run(until=process)
    assert metrics.cabins_snapshot(0.1)["totals"]["other_move"]["distance_floors"] == 1


def test_recreated_cabin_gets_distinct_instance_identity() -> None:
    scenario = examples()[5]
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    before = engine.elevators["E"]
    after = engine._create_elevator(scenario.factory.elevators[0])
    assert after.id == before.id and after.instance != before.instance
    engine.close()
