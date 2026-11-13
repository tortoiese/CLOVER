"""Capacity units, atomic multi-resource admission and protected physical lifetimes."""

from __future__ import annotations

import copy
import random
from dataclasses import replace

import pytest
import simpy

import config
from database.seed_data import build_default_config
from simulation.corridor import CorridorNetwork
from simulation.engine import SimulationEngine
from simulation.path_graph import PathGraph
from simulation.resource_pool import CapacityPool, TrafficArbiter
from simulation.robot import InterruptKind, RobotState
from simulation.test_traffic_rules import _travel, traffic_factory
from simulation.traffic_rules import (
    TrafficBlocked,
    TrafficDemand,
    capacity_mm,
    demand_mm,
)
from utils.config_models import FactoryProfile, PathEdgeConfig, PathNodeConfig


def test_width_units_match_detached_demands_runtime_and_snapshot() -> None:
    """The 1.3m boundary has 900mm capacity and a 900mm robot request everywhere."""
    cfg = traffic_factory()
    net = CorridorNetwork(simpy.Environment(), cfg)
    graph = PathGraph(cfg)
    route = graph.for_robot(.6).route_to_facility(0, 100, "B")
    assert route is not None
    assert capacity_mm(1.3, cfg.profile) == demand_mm(.6, cfg.profile) == 900
    demand, = net.section_demands(route, 0, 0, .6)
    pool = net.demand_resource(demand)
    assert demand.amount == pool.capacity == 900 and demand.unit == pool.unit == "mm"
    with pool.request(demand.amount, "R1"):
        snap = next(s for s in net.snapshots() if s.id == "edge:1")
        assert snap.occupied == snap.capacity == 900 and snap.unit == "mm"
        assert snap.resource_id == demand.resource and snap.contract == demand.contract
    assert pool.count == 0


def test_rounding_never_admits_a_fractionally_oversized_robot() -> None:
    """Capacity rounds down and demand rounds up at sub-mm input boundaries."""
    profile = FactoryProfile()
    assert capacity_mm(1.3004, profile) == 900
    assert demand_mm(.6001, profile) == 901
    graph = PathGraph(traffic_factory(width=1.3004))
    assert graph.for_robot(.6001).route_to_facility(0, 100, "B") is None


def test_heterogeneous_robot_widths_share_one_physical_budget() -> None:
    """900mm + 1400mm fits exactly; another 900mm request must wait."""
    cfg = traffic_factory(width=2.7)
    net = CorridorNetwork(simpy.Environment(), cfg)
    pool = net.edge_resource(1)
    assert pool is not None and pool.capacity == 2300
    first, second = pool.request(net.width_units(.6), "small"), pool.request(net.width_units(1.1), "large")
    third = pool.request(900, "waiting")
    assert first.triggered and second.triggered and not third.triggered and pool.count == 2300
    second.release()
    assert third.triggered and pool.count == 1800
    first.release()
    third.release()


def test_bundle_has_no_partial_hold_and_disjoint_requests_progress() -> None:
    """A blocked A+B bundle owns neither A nor B and does not block unrelated C."""
    env = simpy.Environment()
    arbiter = TrafficArbiter(env)
    a, b, c = [CapacityPool(env, 1, resource_id=name, arbiter=arbiter) for name in "ABC"]
    owner = b.request(1, "owner")
    waiting = arbiter.request({a: 1, b: 1}, "bundle")
    later = a.request(1, "later")
    independent = c.request(1, "independent")
    assert owner.triggered and independent.triggered
    assert not waiting.triggered and not later.triggered and a.count == 0
    assert [claim.robot_id for claim in a.queue] == ["bundle", "later"]
    waiting.cancel()
    assert later.triggered and a.count == 1 and not b.queue
    owner.release()
    later.release()
    independent.release()
    assert not arbiter.pending and all(pool.count == 0 for pool in (a, b, c))


def test_opposite_order_bundles_do_not_hold_and_wait() -> None:
    """One bundle acquires both resources, irrespective of dict insertion order."""
    env = simpy.Environment()
    arbiter = TrafficArbiter(env)
    a, b = [CapacityPool(env, 1, arbiter=arbiter) for _ in range(2)]
    first = arbiter.request({a: 1, b: 1}, "R1")
    second = arbiter.request({b: 1, a: 1}, "R2")
    assert first.triggered and not second.triggered
    assert [claim.robot_id for claim in a.users] == ["R1"]
    first.release()
    first.release()
    assert second.triggered and a.count == b.count == 1
    second.release()
    assert a.count == b.count == 0


@pytest.mark.parametrize("amount", [0, -1, 1001, True, 1.5])
def test_invalid_demand_is_rejected_before_any_acquisition(amount: int) -> None:
    """Invalid or fractional counts cannot poison ownership state."""
    pool = CapacityPool(simpy.Environment(), 1000, unit="mm")
    with pytest.raises(TrafficBlocked):
        pool.request(amount)
    assert not pool.users and not pool.queue


def test_layout_capacity_change_is_atomic_across_resources() -> None:
    """Growing B must not admit an old bundle before A's simultaneous closure."""
    env = simpy.Environment()
    arbiter = TrafficArbiter(env)
    a, b = [CapacityPool(env, 900, arbiter=arbiter) for _ in range(2)]
    first = b.request(900)
    waiting = arbiter.request({a: 500, b: 900})
    arbiter.resize({b: 1800, a: 0})
    assert waiting.closed and not waiting.granted and a.count == 0
    assert b.count == 900 and not arbiter.pending
    first.release()


def test_shrinking_resource_drains_current_owners_without_new_admission() -> None:
    """A reduced limit protects old owners and prevents an additional grant."""
    pool = CapacityPool(simpy.Environment(), 1800, unit="mm")
    first, second = pool.request(900), pool.request(900)
    pool.resize(900)
    waiting = pool.request(900)
    assert pool.count == 1800 and not waiting.triggered
    first.release()
    assert pool.count == 900 and not waiting.triggered
    second.release()
    assert waiting.triggered and pool.count == 900
    waiting.release()


def test_physical_identity_survives_renumbering_and_direction_reversal() -> None:
    """Renaming an edge does not create an empty replacement of an occupied corridor."""
    cfg = traffic_factory()
    net = CorridorNetwork(simpy.Environment(), cfg)
    pool = net.edge_resource(1)
    assert pool is not None
    key = net.resource_id(1)
    request = pool.request(900, "R1")
    changed = copy.deepcopy(cfg)
    changed.edges[0] = replace(changed.edges[0], id=99, from_node=2, to_node=1)
    net.reconfigure(changed)
    assert net.edge_resource(99) is pool and net.resource_id(99) == key and pool.count == 900
    request.release()


def test_removed_occupied_resource_remains_visible_until_drained() -> None:
    """A removed footprint remains closed and visible while an old owner exits."""
    cfg = traffic_factory()
    net = CorridorNetwork(simpy.Environment(), cfg)
    pool = net.edge_resource(1)
    assert pool is not None
    request = pool.request(900, "R1")
    changed = copy.deepcopy(cfg)
    changed.edges = changed.edges[1:]
    net.reconfigure(changed)
    assert pool.capacity == 0 and pool.count == 900
    assert any(s.draining and s.occupants == ["R1"] for s in net.snapshots())
    with pytest.raises(TrafficBlocked):
        pool.request(900, "R2")
    request.release()
    assert not any(s.id.startswith("retired:") for s in net.snapshots())


def test_detached_request_rejects_wrong_unit_or_version() -> None:
    """A planner cannot silently mix lane counts, width mm or incompatible versions."""
    net = CorridorNetwork(simpy.Environment(), traffic_factory())
    key = net.resource_id(1)
    for demand in (TrafficDemand(key, 1, "slot"), TrafficDemand(key, 900, "mm", "old"),
                   TrafficDemand(key, 901, "mm"), TrafficDemand(key, True, "mm")):
        with pytest.raises(TrafficBlocked):
            net.demand_resource(demand)


def test_section_reservations_match_execution_through_rear_clearance() -> None:
    """Both physical edges use the complete section interval, not independent subintervals."""
    cfg = traffic_factory()
    cfg.nodes.append(PathNodeConfig(4, 50, 100))
    cfg.edges = [PathEdgeConfig(1, 1, 4, width=1.3), PathEdgeConfig(2, 4, 2, width=1.3)]
    engine = SimulationEngine(cfg, params={"task_rate": 0}, comparison_seed=1)
    engine.env.run(until=.1)
    route = engine.path_graph.for_robot(.6).route_to_facility(0, 100, "B")
    assert route is not None
    planned = engine.corridors.planned_reservations(route, .6, .8, 1, (.1, 5.1), (5.1, 10.1))
    assert len(planned) == 2
    assert all(r.start == .1 and r.end == pytest.approx(10.5) and r.amount == 900 for r in planned)
    _travel(engine, "R1", "B", 0, 100)
    engine.env.run(until=10.3)
    assert all(pool.count == 900 for pool in engine.corridors._corridor_resource.values())
    engine.env.run(until=10.6)
    assert all(pool.count == 0 for pool in engine.corridors._corridor_resource.values())


def test_planner_must_include_clearance_between_distinct_sections() -> None:
    """An advisory schedule that omits the release tail is rejected before solving."""
    cfg = traffic_factory(width=2.7)
    cfg.nodes.append(PathNodeConfig(4, 50, 100))
    cfg.edges = [PathEdgeConfig(1, 1, 4, width=2.7), PathEdgeConfig(2, 4, 2, width=2.7)]
    net = CorridorNetwork(simpy.Environment(), cfg)
    route = PathGraph(cfg).for_robot(.6).route_to_facility(0, 100, "B")
    assert route is not None
    with pytest.raises(ValueError, match="tail clearance"):
        net.planned_reservations(route, .6, .8, 1, (0, 5), (5, 10))
    intervals = net.planned_reservations(route, .6, .8, 1, (0, 5.4), (5, 10.4))
    assert [(r.start, r.end) for r in intervals] == [(0, 5.4), (5.4, 10.8)]


def test_control_cancel_waits_until_exit_and_rear_clearance() -> None:
    """An admitted robot cannot disappear from a corridor on a retire command."""
    engine = SimulationEngine(traffic_factory(), "baseline", params={"task_rate": 0}, comparison_seed=1)
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    pool = engine.corridors.edge_resource(1)
    assert pool is not None and robot._traffic_granted
    assert robot.interrupt(InterruptKind.RETIRE)
    engine.env.run(until=9)
    assert pool.count == 900 and robot.alive
    engine.env.run(until=11)
    assert robot.position() == (100, 100) and not robot.alive and pool.count == 0


def test_duplicate_edges_and_signed_zero_use_one_width_budget() -> None:
    """Reversed duplicate rows cannot advertise more width than the physical pool."""
    cfg = traffic_factory(width=2.7)
    cfg.nodes[0].x = -0.0
    cfg.edges.append(PathEdgeConfig(99, 2, 1, width=1.3, is_bidirectional=False))
    net = CorridorNetwork(simpy.Environment(), cfg)
    assert net.edge_resource(1) is net.edge_resource(99)
    assert net.edge_resource(1).capacity == 900
    assert PathGraph(cfg).for_robot(1.1).route_to_facility(0, 100, "B") is None
    cfg.nodes[0].x = 0.0
    key = net.resource_id(1)
    net.reconfigure(cfg)
    assert net.resource_id(1) == key


def test_bundle_ownership_is_immutable_and_foreign_release_is_rejected() -> None:
    """Wrong-pool release cannot silently release another corridor's owner."""
    env = simpy.Environment()
    arbiter = TrafficArbiter(env)
    a, b = [CapacityPool(env, 900, arbiter=arbiter) for _ in range(2)]
    request = a.request(900)
    with pytest.raises(TypeError):
        request.demands[b] = 1
    with pytest.raises(ValueError):
        b.release(request)
    with pytest.raises(ValueError):
        TrafficArbiter(env).release(request)
    assert a.count == 900 and b.count == 0
    request.release()


def test_topology_change_drains_old_section_before_applying_latest_config() -> None:
    """Moving/removing a footprint cannot leave old owners crossing new traffic."""
    cfg = traffic_factory()
    engine = SimulationEngine(cfg, "baseline", params={"task_rate": 0}, comparison_seed=1)
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    pool = engine.corridors.edge_resource(1)
    changed = copy.deepcopy(cfg)
    changed.edges[0].width = 2.7
    assert "대기" in engine.apply_config(changed)[0]
    assert engine.factory is cfg and pool.count == 900 and pool.capacity == 0
    assert all(s.draining for s in engine.corridors.snapshots())
    latest = copy.deepcopy(changed)
    latest.edges[0].width = 3.1
    engine.apply_config(latest)
    engine.env.run(until=9)
    assert engine.factory is cfg and pool.count == 900
    engine.env.run(until=11)
    assert engine.factory.edges[0].width == 3.1
    assert engine._traffic_ready is None and engine._pending_traffic_config is None
    assert engine.corridors.edge_resource(1) is pool and pool.count == 0 and pool.capacity == 2700


def test_cancel_pending_topology_change_reopens_current_layout() -> None:
    """Saving the original geometry cancels the queued change without losing owners."""
    cfg = traffic_factory()
    engine = SimulationEngine(cfg, params={"task_rate": 0}, comparison_seed=1)
    pool = engine.corridors.edge_resource(1)
    request = pool.request(900)
    changed = copy.deepcopy(cfg)
    changed.edges = []
    engine.apply_config(changed)
    gate = engine._traffic_ready
    assert pool.capacity == 0 and not gate.triggered
    engine.apply_config(copy.deepcopy(cfg))
    assert gate.triggered and pool.capacity == pool.count == 900
    assert engine._pending_traffic_config is None
    request.release()


def test_reconfiguration_between_sections_replans_instead_of_reporting_unreachable() -> None:
    """After draining section one, an old route must not enter the closed next section."""
    cfg = traffic_factory(width=2.7)
    cfg.robots = cfg.robots[:1]
    cfg.nodes.append(PathNodeConfig(4, 50, 100))
    cfg.edges = [PathEdgeConfig(1, 1, 4, width=2.7), PathEdgeConfig(2, 4, 2, width=2.7)]
    engine = SimulationEngine(cfg, "baseline", params={"task_rate": 0}, comparison_seed=1)
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    changed = copy.deepcopy(cfg)
    changed.edges[1].width = 3.1
    engine.apply_config(changed)
    engine.env.run(until=9)
    assert engine.factory.edges[1].width == 3.1
    assert robot.current_task is not None and robot.position()[0] >= 50
    assert not engine._unreachable_warned


def test_disabled_elevator_never_teleports_a_stale_route() -> None:
    """A stale lift identifier fails admission instead of changing robot position."""
    cfg = build_default_config()
    engine = SimulationEngine(cfg, comparison_seed=1)
    robot = next(iter(engine.robots.values()))
    elevator = engine.elevators["EL-01"]
    elevator.removed = True
    before = robot.position(), robot.floor
    with pytest.raises(TrafficBlocked):
        next(robot._ride_process("EL-01", 50, 50, 2))
    assert (robot.position(), robot.floor) == before and elevator.resource.count == 0


def test_topology_change_cancels_waiters_without_partial_ownership() -> None:
    """A real waiting actor removes its complete request while old motion drains."""
    engine = SimulationEngine(traffic_factory(), "baseline", params={"task_rate": 0}, comparison_seed=1)
    for robot in engine.robots.values():
        robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    pool = engine.corridors.edge_resource(1)
    assert len(pool.queue) == 1
    changed = copy.deepcopy(engine.factory)
    changed.nodes[1].x = 110
    changed.stations[0].x = 110
    engine.apply_config(changed)
    engine.env.run(until=2)
    assert pool.count == 900 and not pool.queue and not engine.corridors.arbiter.pending
    engine.env.run(until=12)
    assert engine.factory.nodes[1].x == 110


def test_direct_interrupt_during_rear_clearance_cannot_release_early() -> None:
    """Even a direct SimPy interrupt during exit clearance waits for the same boundary."""
    engine = SimulationEngine(traffic_factory(), "baseline", params={"task_rate": 0}, comparison_seed=1)
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.env.run(until=10.1)
    pool = engine.corridors.edge_resource(1)
    robot.process.interrupt({"kind": InterruptKind.RETIRE})
    engine.env.run(until=10.3)
    assert pool.count == 900 and robot.alive
    engine.env.run(until=10.6)
    assert pool.count == 0 and not robot.alive


def test_stalled_robot_keeps_resources_through_rescue_and_retire(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fault or cancel cannot free the footprint of a physically stalled robot."""
    monkeypatch.setattr(config, "FAULT_RECOVERY_TIME", 2)
    cfg = traffic_factory()
    cfg.robots = cfg.robots[:1]
    engine = SimulationEngine(cfg, "baseline", params={"task_rate": 0}, comparison_seed=1)
    # Drive the normal process with a forced task so the scheduler's energy filter
    # does not remove the intentionally depleted fault-injection candidate.
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    task = engine.pending_tasks.pop()
    engine.assign(task, robot)
    robot.battery = .01
    engine.env.run(until=.5)
    pool = engine.corridors.edge_resource(1)
    assert robot.state == RobotState.FAULT and pool.count == 900
    stopped = robot.position()
    robot.process.interrupt({"kind": InterruptKind.RETIRE})
    engine.env.run(until=1.5)
    assert robot.position() == stopped and pool.count == 900 and robot.alive
    engine.env.run(until=14)
    assert robot.position() == (100, 100) and pool.count == 0 and not robot.alive
    assert engine.metrics.total_completed == 0


def test_elevator_reference_and_retire_wait_for_real_arrival() -> None:
    """The lookup and rider use one cabin; cancelling never teleports a rider."""
    cfg = build_default_config()
    engine = SimulationEngine(cfg, params={"task_rate": 0}, comparison_seed=1)
    elevator = engine.elevators["EL-01"]
    robot = next(iter(engine.robots.values()))
    robot.floor = 1
    destination = next(n for n in cfg.nodes if n.node_type == "elevator" and n.floor == 2)

    def ride():
        try:
            yield from robot._ride_process("EL-01", destination.x, destination.y, 2)
        except simpy.Interrupt as interrupt:
            robot._handle_interrupt(interrupt.cause)

    engine.env.process(ride())
    engine.env.run(until=elevator.door_time / 2 + .1)
    assert robot._riding is elevator and elevator.resource.count == 1
    assert all(r is elevator.resource for r in engine.corridors._elevator_resource.values())
    assert robot.interrupt(InterruptKind.RETIRE)
    engine.env.run(until=elevator.door_time / 2 + .2)
    assert robot.floor == 1 and elevator.resource.count == 1 and robot.alive
    engine.env.run(until=20)
    assert robot.floor == 2 and not robot.alive and elevator.resource.count == 0
    resized = copy.deepcopy(cfg)
    resized.elevators[0].capacity = 2
    with pytest.raises(ValueError, match="UNSUPPORTED_CABIN_CAPACITY"):
        engine.apply_config(resized)
    assert elevator.resource.capacity == 1
    assert engine.factory.elevators[0].capacity == 1
    assert all(r is elevator.resource for r in engine.corridors._elevator_resource.values())


def test_randomized_bundle_contention_preserves_all_or_none_ownership() -> None:
    """Heterogeneous requests and cancellations stay within each physical budget."""
    rng = random.Random(9)
    env = simpy.Environment()
    arbiter = TrafficArbiter(env)
    pools = [CapacityPool(env, 2700, unit="mm", arbiter=arbiter) for _ in range(4)]
    requests = []
    for number in range(300):
        if requests and rng.random() < .45:
            rng.choice(requests).release()
        else:
            selected = rng.sample(pools, rng.randint(1, 3))
            requests.append(arbiter.request(dict.fromkeys(selected, rng.choice((900, 1400))), str(number)))
        assert all(0 <= pool.count <= pool.capacity for pool in pools)
        for request in requests:
            owns = [claim in pool.users for pool, claim in request.claims.items()]
            assert all(owns) if request.granted and not request.closed else not any(owns)
    for request in requests:
        request.release()
    assert not arbiter.pending and all(pool.count == 0 for pool in pools)
