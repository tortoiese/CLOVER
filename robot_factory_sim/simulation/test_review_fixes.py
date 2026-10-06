"""충전과 경로 계산 리뷰 수정의 회귀 테스트."""

from dataclasses import replace

import pytest
import simpy

from simulation.engine import SimulationEngine
from simulation.path_graph import PathGraph, edge_blocked
from simulation.station import ChargingStation
from utils.config_models import (
    ChargingStationConfig, FactoryConfig, ObstacleConfig, PathEdgeConfig, PathNodeConfig, RobotConfig,
)


@pytest.mark.parametrize("threshold,minimum", [(95.0, False), (75.0, True), (100.0, True)])
def test_high_threshold_charge_makes_time_progress(threshold: float, minimum: bool) -> None:
    """충전 목표 이상 임계값에서도 로봇이 한 번 충전하고 대기한다."""
    factory = FactoryConfig(
        chargers=[ChargingStationConfig("C", 0, 0, charge_rate=10)],
        robots=[RobotConfig("R", charge_threshold=threshold)],
        nodes=[PathNodeConfig(1, 0, 0, linked_station_id="C")],
    )
    engine = SimulationEngine(factory, params={"task_rate": 0.0})
    engine.update_param("min_charge_mode", float(minimum))
    robot = engine.robots["R"]
    robot.x = robot.y = 0
    robot.battery = 40
    # env.run만 쓰면 회귀 시 테스트 자체가 멈추므로 이벤트 횟수를 제한한다.
    for _ in range(2000):
        if engine.env.now >= 20:
            break
        engine.env.step()
    assert engine.env.now >= 20
    assert robot.charge_sessions == 1
    assert robot.battery_pct >= threshold - 1e-6
    assert not engine.scheduler.should_charge(robot, engine)


def test_charger_growth_preserves_users_and_queue() -> None:
    """용량 증가가 기존 FIFO 요청을 승인하고 자원 객체를 유지한다."""
    env = simpy.Environment()
    cfg = ChargingStationConfig("C", 0, 0, capacity=1)
    charger = ChargingStation(env, cfg)
    resource = charger.resource
    first, second, third = [resource.request() for _ in range(3)]
    assert first.triggered and not second.triggered
    charger.update_config(replace(cfg, capacity=2))
    assert charger.resource is resource
    assert resource.count == 2
    assert second.triggered and not third.triggered
    assert charger.waiting == 1
    resource.release(first)
    env.run(until=1)
    assert third.triggered
    assert resource.count == 2


def test_charger_shrink_drains_existing_users_before_admission() -> None:
    """축소 시 충전을 중단하지 않고 새 한도 아래에서만 대기를 승인한다."""
    env = simpy.Environment()
    cfg = ChargingStationConfig("C", 0, 0, capacity=2)
    charger = ChargingStation(env, cfg)
    first, second, third = [charger.resource.request() for _ in range(3)]
    charger.update_config(replace(cfg, capacity=1))
    charger.resource.release(first)
    env.run(until=1)
    assert not third.triggered
    charger.resource.release(second)
    env.run(until=2)
    assert third.triggered and charger.resource.count == 1


def test_charger_growth_fills_all_new_slots() -> None:
    """여러 슬롯을 추가하면 추가된 슬롯 수만큼 기존 대기를 즉시 승인한다."""
    env = simpy.Environment()
    cfg = ChargingStationConfig("C", 0, 0, capacity=1)
    charger = ChargingStation(env, cfg)
    requests = [charger.resource.request() for _ in range(5)]
    charger.update_config(replace(cfg, capacity=4))
    assert all(request.triggered for request in requests[:4])
    assert not requests[4].triggered
    assert charger.resource.count == 4 and charger.waiting == 1


def test_blocked_start_has_no_route_or_finite_distance() -> None:
    """그래프 진입 구간이 벽으로 차단되면 경로와 거리 모두 도달 불가다."""
    factory = FactoryConfig(
        nodes=[PathNodeConfig(1, 10, 0, linked_station_id="C")],
        chargers=[ChargingStationConfig("C", 10, 0)],
        obstacles=[ObstacleConfig(1, "wall", 5, -10, 5, 10)],
    )
    graph = PathGraph(factory)
    assert graph.route_to_facility(0, 0, "C") is None
    assert graph.distance_to_facility(0, 0, "C") == float("inf")


def test_visible_alternative_start_routes_around_wall() -> None:
    """막힌 최근접 노드 대신 연결 가능한 노드로 우회하며 거리도 일치한다."""
    factory = FactoryConfig(
        nodes=[PathNodeConfig(1, 10, 0, linked_station_id="C"),
               PathNodeConfig(2, 0, 30), PathNodeConfig(3, 10, 30)],
        edges=[PathEdgeConfig(1, 2, 3), PathEdgeConfig(2, 3, 1)],
        chargers=[ChargingStationConfig("C", 10, 0)],
        obstacles=[ObstacleConfig(1, "wall", 5, -10, 5, 10)],
    )
    graph = PathGraph(factory)
    route = graph.route_to_facility(0, 0, "C")
    assert route is not None
    assert route.points[0] == (0, 30)
    previous = (0.0, 0.0)
    for point in route.points:
        assert not edge_blocked(previous, point, factory.obstacles)
        previous = point
    assert route.length == graph.distance_to_facility(0, 0, "C")
