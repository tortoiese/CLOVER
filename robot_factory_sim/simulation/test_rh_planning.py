"""Shared-model parity, independent validation and physical execution regressions."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest
import simpy

from simulation.engine import SimulationEngine
from simulation.path_graph import PathGraph
from simulation.planning.backend import Backend
from simulation.planning.contracts import (
    CabinArc,
    CabinInput,
    Facility,
    JobInput,
    Reservation,
    ResourceInput,
    ResourceUse,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    energy_down,
    energy_up,
)
from simulation.planning.execution import commit, select_prefix
from simulation.planning.snapshot import capture, prepare
from simulation.planning.validation import capacity_errors, validate
from simulation.resource_pool import CapacityPool
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)


@pytest.fixture(scope="module", autouse=True)
def load_native_solvers() -> None:
    """Separate native library cold startup from each instance's solve budget."""
    from ortools.linear_solver import pywraplp
    from ortools.sat.python import cp_model
    assert pywraplp.Solver.CreateSolver("SCIP") is not None
    assert cp_model.CpModel() is not None


def small_snapshot(robots: int = 1, jobs: int = 2, horizon: int = 10) -> Snapshot:
    """A tiny instance with analytically checkable completion and defer costs."""
    rs = tuple(RobotInput(f"R{i}", "C", 100_000, 100_000, 5_000, 1, .1) for i in range(robots))
    facilities = (Facility("C", 0, 0, 1, "charge", rate=1000), Facility("S", 10, 0, 1, "work", duration=2))
    routes = tuple(RouteInput(r.id, a, b, ((10, 0) if b == "S" else (0, 0),), (1,), (None,), (1,), (None,), 1, 1)
                   for r in rs for a, b in (("C", "S"), ("S", "C")))
    js = tuple(JobInput(i, ("S",), 0, 4, (8, 4, 2)[i]) for i in range(jobs))
    return Snapshot("test:1", "layout", 0, Settings(horizon=horizon, interval=2, timeout=4, watchdog=5),
                    rs, js, facilities, routes, (), .01)


@pytest.mark.parametrize("robots,jobs,horizon", [(1, 1, 4), (1, 2, 5), (2, 3, 6), (1, 3, 3)])
def test_exact_model_parity_and_exhaustive_defer(robots: int, jobs: int, horizon: int) -> None:
    """Both solvers prove the same lexicographic optimum as a tiny scheduling oracle."""
    snapshot = small_snapshot(robots, jobs, horizon)
    results = [Backend(kind).solve(snapshot) for kind in ("milp", "cpsat")]
    for result in results:
        assert result.report.status == "OPTIMAL", result.report
        assert not validate(result)
    assert results[0].report.model_hash == results[1].report.model_hash
    assert results[0].report.phase1_cost == results[1].report.phase1_cost
    assert results[0].report.phase2_cost == results[1].report.phase2_cost
    # Exhaustive subsets/orderings with station capacity one, common 1s travel and 2s work.
    optimum = sum(j.weight for j in snapshot.jobs)
    for length in range(jobs + 1):
        for permutation in itertools.permutations(snapshot.jobs, length):
            if 1 + 2 * length <= horizon:
                optimum = min(optimum, sum(j.weight for j in snapshot.jobs if j not in permutation))
    assert results[0].report.phase1_cost == optimum


@pytest.mark.parametrize("kind", ["milp", "cpsat"])
def test_partial_charging_and_energy_quantization(kind: str) -> None:
    snapshot = small_snapshot(jobs=1, horizon=8)
    snapshot = replace(snapshot, robots=(replace(snapshot.robots[0], battery=5_000, reserve=5_000),))
    result = Backend(kind).solve(snapshot)
    assert result.report.status == "OPTIMAL", result.report
    assert not validate(result)
    assert any(a.arc.kind == "charge" and 0 < a.gain < 1000 for a in result.actions)
    assert energy_up(.0001) == 1 and energy_down(.0009) == 0


def test_half_open_capacity_and_grandfathered_shrink() -> None:
    first = Reservation("C", "R1", 0, 1)
    second = Reservation("C", "R2", 1, 2)
    assert not capacity_errors((first, second), {"C": 1})
    assert capacity_errors((replace(second, start=.5), first), {"C": 1})
    assert not capacity_errors((), {"C": 1}, (first, first))
    assert capacity_errors((first,), {"C": 1}, (first, first))


def test_weighted_pool_is_atomic_and_drains() -> None:
    env = simpy.Environment()
    pool = CapacityPool(env, 10)
    first, second = pool.request(6), pool.request(6)
    assert first.triggered and not second.triggered and pool.count == 6
    pool.resize(4)
    assert pool.count == 6 and not second.triggered
    pool.release(first)
    env.run(until=1)
    assert not second.triggered
    pool.resize(7)
    assert second.triggered and pool.count == 6


def factory() -> FactoryConfig:
    """An isolated two-node factory with one charger and work station."""
    return FactoryConfig(chargers=[ChargingStationConfig("C", 0, 0, charge_rate=5)],
                         stations=[WorkStationConfig("S", "S", 10, 0, avg_process_time=2, process_time_std=0)],
                         robots=[RobotConfig("R", move_speed=1)],
                         nodes=[PathNodeConfig(1, 0, 0, linked_station_id="C"),
                                PathNodeConfig(2, 10, 0, linked_station_id="S")],
                         edges=[PathEdgeConfig(1, 1, 2, width=1.2)])


def test_hot_reload_preserves_occupied_corridor_and_width_filter() -> None:
    cfg = factory()
    engine = SimulationEngine(cfg, params={"task_rate": 0}, comparison_seed=3)
    resource = engine.corridors.edge_resource(1)
    first = resource.request(engine.corridors.width_units(.6))
    assert first.triggered
    engine.apply_config(replace(cfg, stations=[replace(cfg.stations[0], avg_process_time=3)]))
    assert engine.corridors.edge_resource(1) is resource
    assert not resource.request(engine.corridors.width_units(.6)).triggered
    graph = PathGraph(cfg).for_robot(2)
    assert graph.route_to_facility(0, 0, "S") is None
    assert graph.route_to_facility(-1, 0, "S") is None


@pytest.mark.parametrize("mode", ["rh_milp", "rh_cpsat"])
def test_real_engine_prefix_completion_and_charge_intent(mode: str) -> None:
    engine = SimulationEngine(factory(), mode, params={"task_rate": 0}, comparison_seed=3)
    engine.scheduler.settings = Settings(horizon=12, interval=2)
    robot = engine.robots["R"]
    robot.x = robot.y = 0
    engine.add_task(product="P", station_id="S", priority=1)
    engine.env.run(until=12)
    assert robot.tasks_done == 1
    assert engine.scheduler.accepted >= 1
    assert engine.scheduler.invalid == 0
    robot.battery = 20
    engine.send_to_charge(robot)
    engine.env.run(until=30)
    assert robot.battery_pct >= 74.99
    assert not robot.forced_charge


def test_snapshot_excludes_noise_and_is_detached() -> None:
    engine = SimulationEngine(factory(), params={"task_rate": 0}, comparison_seed=3)
    task = engine.add_task(product="P", station_id="S", priority=1)
    engine.env.run(until=.01)
    request = capture(engine, Settings(), "test:1", (), set())
    assert "noise" not in repr(request.snapshot)
    before = request.factory
    task.process_noise = 9876
    engine.factory.stations[0].avg_process_time = 99
    assert request.factory == before
    assert prepare(request).facilities[0].duration != 99


def test_reject_corrupt_energy_and_duplicate_job() -> None:
    result = Backend("cpsat").solve(small_snapshot())
    work = next(a for a in result.actions if a.arc.kind == "work")
    corrupt = replace(work, arc=replace(work.arc, energy=0))
    assert validate(replace(result, actions=tuple(corrupt if a is work else a for a in result.actions)))
    assert validate(replace(result, actions=result.actions + (work,)))


def test_graph_limit_no_charger_and_invalid_settings() -> None:
    result = Backend().solve(replace(small_snapshot(), settings=Settings(horizon=10, max_arcs=1)))
    assert result.report.status == "GRAPH_LIMIT"
    snapshot = small_snapshot()
    result = Backend("cpsat").solve(replace(snapshot, facilities=tuple(f for f in snapshot.facilities if f.kind != "charge")))
    assert result.report.status == "INFEASIBLE"
    with pytest.raises(ValueError):
        Settings(max_tasks=33).validate()


def test_atomic_commit_rejection_has_no_side_effects() -> None:
    engine = SimulationEngine(factory(), "rh_cpsat", params={"task_rate": 0}, comparison_seed=3)
    engine.scheduler.next_solve = 100
    engine.env.run(until=.01)
    engine.add_task(product="P", station_id="S", priority=1)
    request = capture(engine, Settings(horizon=12, interval=2), "test:1", (), set())
    result = Backend("cpsat").solve(prepare(request))
    prefix = select_prefix(result)
    engine.pending_tasks.clear()
    assert not commit(engine, prefix)
    assert not engine.scheduler.reservations
    assert engine.robots["R"].current_task is None


@pytest.mark.parametrize("kind", ["milp", "cpsat"])
def test_single_cabin_requires_empty_reposition(kind: str) -> None:
    snapshot = small_snapshot(robots=2, jobs=2, horizon=12)
    routes = tuple(replace(r, points=((0, 0),), floors=(2,), lifts=("E",), distance_m=0, seconds=2,
                           rides=(CabinArc("E", 1 if r.origin == "C" else 2,
                                           2 if r.destination == "S" else 1, 0, 2, "ride"),))
                   for r in snapshot.routes if r.origin == "C")
    routes += tuple(RouteInput(r.id, "S", "D", (), (), (), (), (), 0, 1) for r in snapshot.robots)
    snapshot = replace(snapshot, scope="traffic", settings=replace(snapshot.settings, scope="traffic"), routes=routes,
                       robots=tuple(replace(r, emergency_order=f"O{i}") for i, r in enumerate(snapshot.robots)),
                       jobs=tuple(replace(j, order_id=f"O{i}") for i, j in enumerate(snapshot.jobs)),
                       facilities=(snapshot.facilities[0], replace(snapshot.facilities[1], x=0, floor=2),
                                   Facility("D", 0, 0, 2, "charge", rate=1000)),
                       cabins=(CabinInput("E", 1, (1, 2), 1, 1),),
                       resources=(ResourceInput("elevator:E", 1, "elevator"),))
    result = Backend(kind).solve(snapshot)
    assert result.report.status == "OPTIMAL", result.report
    assert not validate(result)
    rides = [c for c in result.cabins if c.kind == "ride"]
    assert len(rides) >= 2
    assert any(c.kind == "empty" for c in result.cabins)
    broken = replace(result, cabins=tuple(replace(c, origin=9) if c is rides[0] else c for c in result.cabins))
    assert validate(broken)


@pytest.mark.parametrize("kind", ["milp", "cpsat"])
def test_corridor_width_demand_prevents_overlapping_moves(kind: str) -> None:
    snapshot = small_snapshot(robots=2, jobs=2)
    snapshot = replace(snapshot, scope="traffic", settings=replace(snapshot.settings, scope="traffic"),
                       robots=tuple(replace(r, emergency_order=f"O{i}") for i, r in enumerate(snapshot.robots)),
                       jobs=tuple(replace(j, order_id=f"O{i}") for i, j in enumerate(snapshot.jobs)),
                       resources=(ResourceInput("corridor:A", 900, "corridor"),),
                       routes=tuple(replace(r, uses=(ResourceUse("corridor:A", 0, 1, 900),))
                                    for r in snapshot.routes))
    result = Backend(kind).solve(snapshot)
    assert result.report.status == "OPTIMAL", result.report
    assert not validate(result)
    outward = [a for a in result.actions if a.arc.kind == "move" and a.arc.destination == "S"]
    assert len(outward) == 2 and outward[0].start != outward[1].start


@pytest.mark.parametrize("mode", ["rh_milp", "rh_cpsat"])
def test_real_multifloor_traffic_plan_and_cabin_execution(mode: str) -> None:
    from utils.config_models import ElevatorConfig
    cfg = factory()
    cfg.profile.floor_count = 2
    cfg.profile.door_time_sec = 1
    cfg.stations[0].floor = 2
    cfg.nodes = [PathNodeConfig(1, 0, 0, linked_station_id="C"),
                 PathNodeConfig(2, 10, 0, "elevator", "E", floor=1),
                 PathNodeConfig(3, 10, 0, "elevator", "E", floor=2),
                 PathNodeConfig(4, 20, 0, linked_station_id="S", floor=2)]
    cfg.edges = [PathEdgeConfig(1, 1, 2), PathEdgeConfig(2, 3, 4)]
    cfg.elevators = [ElevatorConfig("E", "E", 10, 0, travel_time=1)]
    engine = SimulationEngine(cfg, mode, comparison_seed=3, params={"task_rate": 0})
    engine.scheduler.settings = Settings(horizon=20, interval=2, scope="traffic")
    engine.robots["R"].x = engine.robots["R"].y = 0
    engine.add_task(product="P", station_id="S", priority=1)
    engine.env.run(until=18)
    assert engine.metrics.total_completed == 1, engine.scheduler.planning_state()
    assert engine.robots["R"].floor == 2
    assert engine.elevators["E"].rides == 1
    assert engine.scheduler.invalid == 0
