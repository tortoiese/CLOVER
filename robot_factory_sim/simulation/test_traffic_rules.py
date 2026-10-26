"""통로 폭·일방통행·자원 일괄 확보·교차로 진입과 핫리로드를 검증한다."""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any

import pytest
import simpy

from simulation.corridor import CorridorNetwork
from simulation.engine import SimulationEngine
from simulation.path_graph import PathGraph
from simulation.traffic_rules import LaneResource, TrafficBlocked, can_pass
from utils.config_models import (
    ChargingStationConfig, FactoryConfig, FactoryProfile, PathEdgeConfig, PathNodeConfig, RobotConfig,
    WorkStationConfig,
)


def traffic_factory(intersection: bool = False, width: float = 1.3) -> FactoryConfig:
    """두 방향 진입을 재현할 작은 공장을 만든다.

    Args:
        intersection: 두 진입로가 교차로로 합류하는지 여부.
        width: 통로 폭(m).

    Returns:
        두 로봇의 통행 시험 구성.
    """
    nodes = [PathNodeConfig(1, 0, 100, "charger", "C"),
             PathNodeConfig(2, 100, 100, "intersection" if intersection else "station", "B"),
             PathNodeConfig(3, 100, 0, "station", "A")]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=200, floor_height=200),
        stations=[WorkStationConfig("B", "B", 100, 100), WorkStationConfig("A", "A", 100, 0)],
        chargers=[ChargingStationConfig("C", 0, 100)],
        robots=[RobotConfig("R1", move_speed=1), RobotConfig("R2", move_speed=1)],
        nodes=nodes, edges=[PathEdgeConfig(1, 1, 2, width=width), PathEdgeConfig(2, 3, 2, width=width)],
    )


@pytest.mark.parametrize("width,expected", [(1.29, False), (1.3, True), (1.31, True), (0, False)])
def test_width_includes_clearances(width: float, expected: bool) -> None:
    """벽 여유 .4m와 측면 여유 .3m를 포함한 경계값을 검사한다."""
    assert can_pass(width, 0.6, FactoryProfile()) is expected


@pytest.mark.parametrize("width", [-1, 0, float("nan"), float("inf")])
def test_invalid_robot_width_is_rejected(width: float) -> None:
    """유효하지 않은 로봇 크기는 경로 조건으로 사용하지 않는다."""
    with pytest.raises(ValueError):
        can_pass(5, width, FactoryProfile())


def test_large_robot_uses_wide_detour_and_cannot_skip_narrow_edge() -> None:
    """로봇 폭별 경로를 캐시하며 먼 노드에 바로 붙어 제한을 우회하지 않는다."""
    factory = traffic_factory(width=1.3)
    factory.edges += [PathEdgeConfig(3, 1, 3, width=3)]
    factory.edges[1].width = 3
    graph = PathGraph(factory)
    small = graph.route_to_facility(0, 100, "B", robot_width=0.6)
    large = graph.route_to_facility(0, 100, "B", robot_width=1.1)
    assert small is not None and large is not None
    assert small.length == 100
    assert large.nodes == [3, 2]
    factory.edges = factory.edges[:1]
    graph = PathGraph(factory)
    assert graph.route_to_facility(0, 100, "B", robot_width=1.1) is None
    assert math.isinf(graph.distance_to_facility(0, 100, "B", robot_width=1.1))
    network = CorridorNetwork(simpy.Environment(), factory)
    with pytest.raises(TrafficBlocked):
        network.lanes_needed(1, 1.1)


def test_one_way_does_not_attach_to_a_distant_node_to_reverse() -> None:
    """정방향만 가능한 통로의 역주행은 대체 연결 구간으로 우회하지 않는다."""
    factory = traffic_factory()
    factory.edges = [replace(factory.edges[0], is_bidirectional=False)]
    graph = PathGraph(factory)
    assert graph.route_to_facility(0, 100, "B", robot_width=0.6) is not None
    assert graph.route_to_facility(100, 100, "C", robot_width=0.6) is None


def test_atomic_multi_lane_requests_finish_without_partial_hold() -> None:
    """큰 로봇 두 대가 각각 일부 차선을 잡고 교착하지 않는다."""
    env = simpy.Environment()
    resource = LaneResource(env, capacity=3)
    completed = []

    def drive(robot_id: str) -> Any:
        with resource.request(2, robot_id) as request:
            yield request
            yield env.timeout(2)
            completed.append(robot_id)

    env.process(drive("R1"))
    env.process(drive("R2"))
    env.run(until=1)
    assert resource.count == 2
    assert len(resource.users) == len(resource.queue) == 1
    env.run(until=5)
    assert completed == ["R1", "R2"]
    assert resource.count == 0 and not resource.queue


def test_fifo_and_interruption_release_requests() -> None:
    """큰 요청 앞에 작은 요청이 끼어들지 않으며 취소된 대기열은 정리된다."""
    env = simpy.Environment()
    resource = LaneResource(env, capacity=3)
    order = []

    def occupy(name: str, units: int, duration: float) -> Any:
        try:
            with resource.request(units, name) as request:
                yield request
                order.append(name)
                yield env.timeout(duration)
        except simpy.Interrupt:
            pass

    env.process(occupy("first", 2, 2))
    waiting = env.process(occupy("cancelled", 2, 2))
    env.process(occupy("last", 1, 1))
    env.run(until=0.5)
    assert order == ["first"]
    waiting.interrupt()
    env.run(until=5)
    assert order == ["first", "last"]
    assert not resource.users and not resource.queue


def _travel(engine: SimulationEngine, robot_id: str, target: str, x: float, y: float) -> simpy.Process:
    robot = engine.robots[robot_id]
    robot.x, robot.y = x, y
    route = engine.path_graph.route_to_facility(x, y, target, robot_width=robot.cfg.width)
    assert route is not None
    return engine.env.process(robot._travel_route(route))


def test_narrow_corridor_opposite_directions_do_not_overlap() -> None:
    """한 차선의 양방향 로봇은 진입점에서 기다린 뒤 순서대로 통과한다."""
    engine = SimulationEngine(traffic_factory(), comparison_seed=1)
    engine.env.run(until=0.1)
    _travel(engine, "R1", "B", 0, 100)
    _travel(engine, "R2", "C", 100, 100)
    engine.env.run(until=1)
    assert engine.robots["R2"].position() == (100, 100)
    assert engine.robots["R2"].wait_resource == "edge:1"
    snap = next(item for item in engine.corridors.snapshots() if item.id == "edge:1")
    assert snap.occupants == ["R1"] and snap.queue == ["R2"]
    engine.env.run(until=25)
    assert engine.robots["R1"].position() == (100, 100)
    assert engine.robots["R2"].position() == (0, 100)
    assert not engine.robots["R2"].wait_reason
    assert engine.corridors.edge_resource(1).count == 0


def test_intersection_waits_before_entry_and_reports_reason() -> None:
    """두 진입로가 비어 있어도 교차로 중심에 동시에 진입하지 않는다."""
    engine = SimulationEngine(traffic_factory(intersection=True, width=3), comparison_seed=1)
    engine.env.run(until=0.1)
    _travel(engine, "R1", "B", 0, 100)
    _travel(engine, "R2", "B", 100, 0)
    engine.env.run(until=1)
    robot = engine.robots["R2"]
    assert robot.position() == (100, 0)
    assert robot.wait_resource == "node:2"
    assert robot.wait_reason == "교차로 진입 대기"
    assert engine.corridors.intersection_resource(2).count == 1
    snapshot = next(item for item in engine.robot_snapshots() if item.id == "R2")
    assert snapshot.wait_seconds > 0 and snapshot.state == "waiting"
    engine.env.run(until=30)
    assert not robot.wait_reason
    assert engine.kpis()["intersection_wait"] > 0


def test_opposite_robots_do_not_meet_inside_multi_edge_narrow_section() -> None:
    """좁은 통로가 여러 엣지로 나뉘어도 중간에서 마주 보며 멈추지 않는다."""
    factory = traffic_factory()
    factory.nodes.append(PathNodeConfig(4, 50, 100))
    factory.edges = [PathEdgeConfig(1, 1, 4, width=1.3), PathEdgeConfig(2, 4, 2, width=1.3)]
    engine = SimulationEngine(factory, comparison_seed=1)
    engine.env.run(until=0.1)
    _travel(engine, "R1", "B", 0, 100)
    _travel(engine, "R2", "C", 100, 100)
    engine.env.run(until=6)
    assert engine.robots["R2"].position() == (100, 100)
    assert engine.robots["R1"].position()[0] > 50
    engine.env.run(until=25)
    assert engine.robots["R2"].position() == (0, 100)
    assert all(not resource.users and not resource.queue for resource in engine.corridors._corridor_resource.values())


def test_intersection_reserves_exit_before_entering_center() -> None:
    """출구가 막혀 있으면 교차로 중앙으로 들어가기 전에 기다린다."""
    factory = traffic_factory(intersection=True, width=3)
    factory.nodes.append(PathNodeConfig(4, 200, 100, "station", "D"))
    factory.stations.append(WorkStationConfig("D", "D", 200, 100))
    factory.edges.append(PathEdgeConfig(3, 2, 4, width=1.3))
    engine = SimulationEngine(factory, comparison_seed=1)
    engine.env.run(until=0.1)
    resource = engine.corridors.edge_resource(3)

    def occupy_exit() -> Any:
        with resource.request(robot_id="other") as request:
            yield request
            yield engine.env.timeout(5)

    engine.env.process(occupy_exit())
    _travel(engine, "R1", "D", 0, 100)
    engine.env.run(until=2)
    assert engine.robots["R1"].position() == (0, 100)
    assert engine.robots["R1"].wait_resource == "edge:3"
    engine.env.run(until=30)
    assert engine.robots["R1"].position() == (200, 100)


def test_non_geometry_reload_keeps_traffic_reservations() -> None:
    """충전 속도만 바꿀 때 통로 점유와 대기 자원을 새로 만들지 않는다."""
    engine = SimulationEngine(traffic_factory(), comparison_seed=1)
    network = engine.corridors
    factory = copy.deepcopy(engine.factory)
    factory.chargers[0].charge_rate += 1
    engine.apply_config(factory)
    assert engine.corridors is network


def test_geometry_reload_cancels_old_waiting_requests() -> None:
    """실제 로봇 프로세스가 기다리는 중 경로를 바꿔도 이전 자원에 남지 않는다."""
    engine = SimulationEngine(traffic_factory(), "baseline", comparison_seed=1)
    for robot in engine.robots.values():
        robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    assert any(robot.wait_reason for robot in engine.robots.values())
    resource = engine.corridors.edge_resource(1)
    factory = copy.deepcopy(engine.factory)
    factory.edges[0].width = 3
    engine.apply_config(factory)
    engine.env.run(until=40)
    assert not resource.users and not resource.queue
    assert engine.metrics.total_completed > 0


def test_width_change_updates_snapshot_without_resetting_resource() -> None:
    """로봇 폭 변경으로 통과 가능 표시를 갱신하되 통로 점유는 보존한다."""
    engine = SimulationEngine(traffic_factory(), comparison_seed=1)
    network = engine.corridors
    factory = copy.deepcopy(engine.factory)
    for robot in factory.robots:
        robot.width = 2
    engine.apply_config(factory)
    assert engine.corridors is network
    assert not next(item for item in network.snapshots() if item.id == "edge:1").passable


def test_robot_size_update_waits_for_occupied_section_to_finish() -> None:
    """확보한 통로 안에서 크기를 즉시 늘려 점유 조건을 바꾸지 않는다."""
    engine = SimulationEngine(traffic_factory(), "baseline", comparison_seed=1)
    robot = engine.robots["R1"]
    robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    assert robot._traffic_section_active
    robot.update_config(replace(robot.cfg, width=1.1, length=1.5))
    assert robot.cfg.width == 0.6
    engine.env.run(until=12)
    assert robot.cfg.width == 1.1 and robot.cfg.length == 1.5
