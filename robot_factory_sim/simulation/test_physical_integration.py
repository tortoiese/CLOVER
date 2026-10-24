"""물리 제약 시나리오(통로·교차점·엘리베이터)와 다층 공장 모델의 통합 회귀 테스트."""

import collections
import sqlite3
from pathlib import Path

import pytest

from database.seed_data import LAYOUT_PRESETS, build_default_config, build_scenario_config
from simulation.engine import SimulationEngine
from simulation.factory_service import FactoryService
from simulation.path_graph import PathGraph
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    FactoryProfile,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)


def _legacy_loop() -> FactoryConfig:
    """ElevatorConfig 없이 노드 그룹(EV-01)과 층간 DB 엣지로 엘리베이터를 표현한 이전 물리 시나리오 구성."""
    profile = FactoryProfile(id=1, factory_name="legacy", floor_width=1000.0, floor_height=600.0, grid_cell_size=50.0)
    stations = [WorkStationConfig("WS-A1", "조립", 150, 150), WorkStationConfig("WS-B1", "포장(2F)", 900, 300)]
    chargers = [ChargingStationConfig("CS-01", 300, 300, capacity=2)]
    nodes = [
        PathNodeConfig(-1, 150, 150, "station", "WS-A1"),
        PathNodeConfig(-2, 300, 300, "charger", "CS-01"),
        PathNodeConfig(-3, 300, 450, "elevator", "EV-01", floor=1),
        PathNodeConfig(-4, 900, 450, "elevator", "EV-01", floor=2),
        PathNodeConfig(-5, 900, 300, "station", "WS-B1", floor=2),
    ]
    edges = [PathEdgeConfig(-1, -1, -2), PathEdgeConfig(-2, -2, -3), PathEdgeConfig(-3, -3, -4),
             PathEdgeConfig(-4, -4, -5)]
    return FactoryConfig(profile, stations, chargers, [RobotConfig(f"R-{i}") for i in range(1, 4)], [], nodes, edges)


@pytest.mark.parametrize("layout", LAYOUT_PRESETS)
@pytest.mark.parametrize("difficulty", ["S1", "S2", "S3", "S4"])
def test_every_scenario_runs_and_completes_work(layout: str, difficulty: str) -> None:
    """모든 레이아웃 × 난이도가 통합 후에도 끝까지 돌고 작업을 완료한다."""
    factory, params = build_scenario_config(layout, difficulty)
    engine = SimulationEngine(factory, "optimized", params=params)
    engine.env.run(until=900)
    assert engine.metrics.total_completed > 0


def test_loop_layout_reaches_second_floor_through_elevator() -> None:
    """루프형: 2층 포장 스테이션이 엘리베이터로만 연결되고, 실제로 로봇이 타고 가서 작업한다."""
    factory, params = build_scenario_config("loop", "S1")
    assert factory.floor_count == 2 and factory.station("WS-B1").floor == 2
    assert PathGraph(factory).distance_to_facility(150, 150, "WS-B1", 1) < float("inf")
    engine = SimulationEngine(factory, "baseline", params=params)
    engine.env.run(until=1200)
    assert engine.stations["WS-B1"].processed > 0
    assert engine.elevators["EV-01"].rides > 0
    assert engine.kpis()["ev_wait"] >= 0.0


def test_node_group_elevator_without_elevator_config_still_works() -> None:
    """ElevatorConfig 없이 노드 그룹으로 정의한 엘리베이터(이전 방식)도 경로·탑승이 된다."""
    factory = _legacy_loop()
    factory.sync_facility_floors()
    assert factory.station("WS-B1").floor == 2
    graph = PathGraph(factory)
    assert [e.id for e in graph.implicit_elevators] == ["EV-01"]
    assert graph.distance_to_facility(150, 150, "WS-B1", 1) < float("inf")
    engine = SimulationEngine(factory, "baseline")
    engine.env.run(until=900)
    assert engine.stations["WS-B1"].processed > 0 and engine.elevators["EV-01"].rides > 0


def test_saving_through_service_keeps_intersections_and_capacity() -> None:
    """공장 설정 저장 경로가 교차점·노드 그룹 엘리베이터 노드를 일반 노드로 바꾸지 않는다."""
    factory, _ = build_scenario_config("corridor", "S1")
    before = collections.Counter(n.node_type for n in factory.nodes)
    service = FactoryService(":memory:")
    service.ensure_database()
    service.save_factory_config(factory, "test")
    after = collections.Counter(n.node_type for n in service.load_factory_config().nodes)
    assert before["intersection"] == after["intersection"] == 1
    assert before == after

    legacy = _legacy_loop()
    legacy.sync_facility_floors()  # DB 로드/프리셋 복원이 하는 동기화
    legacy.nodes[2].capacity = legacy.nodes[3].capacity = 2
    service.save_factory_config(legacy, "test")
    saved = service.load_factory_config()
    assert [n.capacity for n in saved.nodes if n.node_type == "elevator"] == [2, 2]
    assert saved.station("WS-B1").floor == 2


def test_database_migrated_by_physical_scenario_branch_is_upgraded(tmp_path: Path) -> None:
    """물리 시나리오 브랜치가 만든 v3 DB(층 컬럼 없음)를 열어도 컬럼이 보충되어 저장이 된다."""
    path = tmp_path / "v3.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE factory_profile (id INTEGER PRIMARY KEY, factory_name TEXT NOT NULL, floor_width REAL,
            floor_height REAL, grid_cell_size REAL, edge_clearance REAL DEFAULT 0.4, side_clearance REAL DEFAULT 0.3,
            floor_travel_sec REAL DEFAULT 4.0, door_time_sec REAL DEFAULT 6.0, updated_at TIMESTAMP);
        CREATE TABLE work_stations (id TEXT PRIMARY KEY, label TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
            width REAL, height REAL, task_type TEXT, avg_process_time REAL, process_time_std REAL, is_active INTEGER,
            updated_at TIMESTAMP);
        CREATE TABLE charging_stations (id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL, capacity INTEGER,
            charge_rate REAL, is_active INTEGER, updated_at TIMESTAMP);
        CREATE TABLE path_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, x REAL NOT NULL, y REAL NOT NULL,
            node_type TEXT, linked_station_id TEXT, floor INTEGER DEFAULT 1, capacity INTEGER, updated_at TIMESTAMP);
        INSERT INTO factory_profile(id, factory_name, floor_width, floor_height, grid_cell_size)
            VALUES (1, 'v3', 800, 600, 50);
        INSERT INTO work_stations(id, label, x, y) VALUES ('WS-01', 'a', 100, 100);
        INSERT INTO path_nodes(x, y) VALUES (50, 50);
        PRAGMA user_version = 3;
        """
    )
    conn.commit()
    conn.close()
    service = FactoryService(path)
    service.ensure_database()
    assert service.add_charger(400, 300, floor=1).startswith("CS-")
    assert service.load_factory_config().floor_count == 1


def test_traveled_distance_is_counted_once() -> None:
    """이동거리(ROI 입력)가 중복 가산되지 않는다: 직선 한 구간의 거리 = 구간 길이."""
    import math

    import config

    factory = build_default_config()
    engine = SimulationEngine(factory, "baseline", params={"task_rate": 0.0})
    robot = next(iter(engine.robots.values()))
    robot.x, robot.y = 100.0, 100.0
    robot._segment = (100.0, 100.0, 300.0, 100.0, 0.0, 10.0, 0.0)  # noqa: SLF001
    robot._apply_segment(1.0)  # noqa: SLF001
    assert math.isclose(robot.distance_m, 200.0 * config.METERS_PER_PIXEL)
