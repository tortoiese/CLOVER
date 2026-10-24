"""다층 공장(엘리베이터 경유 경로, 탑승 시뮬레이션, DB 저장/마이그레이션, 핫리로드) 회귀 테스트."""

import copy
import sqlite3
from pathlib import Path

from database.db_manager import DBManager
from database.seed_data import build_default_config
from simulation.engine import SimulationEngine
from simulation.path_graph import PathGraph
from utils.config_models import FactoryConfig


def _factory() -> FactoryConfig:
    """기본 3층 데모 구성."""
    return build_default_config()


def test_route_to_other_floor_rides_one_elevator_trip() -> None:
    """1층에서 3층 설비로 가는 경로는 엘리베이터 탑승 한 번(연속 구간 병합)으로 표현된다."""
    route = PathGraph(_factory()).route_to_facility(100, 150, "WS-05", 1)
    assert route is not None
    assert route.floors[-1] == 3
    assert route.lifts.count("EL-01") == 1
    assert len(route.points) == len(route.floors) == len(route.lifts)


def test_inactive_elevator_cuts_off_other_floors() -> None:
    """엘리베이터가 멈추면 같은 층은 갈 수 있지만 다른 층 설비는 도달 불가다."""
    factory = _factory()
    factory.elevators[0].is_active = False
    graph = PathGraph(factory)
    assert graph.route_to_facility(100, 150, "WS-05", 1) is None
    assert graph.distance_to_facility(100, 150, "WS-05", 1) == float("inf")
    assert graph.route_to_facility(100, 150, "WS-02", 1) is not None


def test_obstacles_only_block_their_own_floor() -> None:
    """1층의 벽이 2층의 같은 좌표 경로를 막지 않는다 (층별 장애물)."""
    factory = _factory()
    factory.obstacles = [o for o in factory.obstacles if o.floor == 1]
    graph = PathGraph(factory)
    blocked_by_floor = {floor: sum(1 for a, _, blocked in graph.edges
                                   if blocked and graph.node_floor[a] == floor) for floor in (1, 2, 3)}
    assert blocked_by_floor[1] > 0
    assert blocked_by_floor[2] == blocked_by_floor[3] == 0


def test_robots_ride_elevator_and_complete_cross_floor_work() -> None:
    """Baseline 배정(라운드로빈)에서는 로봇이 층을 오가며 엘리베이터가 실제로 이동한다."""
    engine = SimulationEngine(_factory(), "baseline")
    moved = fractional = False
    for step in range(1, 901):
        engine.env.run(until=float(step))
        moved = moved or any(e.moving for e in engine.elevators.values())
        fractional = fractional or any(abs(r.floor_position() - round(r.floor_position())) > 0.05
                                       for r in engine.robots.values())
    assert moved and fractional
    assert engine.elevators["EL-01"].rides > 0
    assert engine.metrics.total_completed > 0
    assert {r.floor for r in engine.robots.values()} <= {1, 2, 3}


def test_hot_reload_while_riding_keeps_simulation_consistent() -> None:
    """탑승 중에 층 수를 줄여 핫리로드해도 오류 없이 이어지고 로봇은 존재하는 층에만 있다."""
    engine = SimulationEngine(_factory(), "baseline")
    for step in range(1, 1200):
        engine.env.run(until=float(step))
        if any(r._riding is not None for r in engine.robots.values()):  # noqa: SLF001 — 탑승 순간 재현
            break
    else:
        raise AssertionError("엘리베이터 탑승 순간을 만들지 못했습니다.")
    reduced = copy.deepcopy(engine.factory)
    reduced.profile.floor_count = 2
    reduced.stations = [s for s in reduced.stations if s.floor <= 2]
    reduced.chargers = [c for c in reduced.chargers if c.floor <= 2]
    reduced.obstacles = [o for o in reduced.obstacles if o.floor <= 2]
    reduced.nodes = [n for n in reduced.nodes if n.floor <= 2]
    node_ids = {n.id for n in reduced.nodes}
    reduced.edges = [e for e in reduced.edges if e.from_node in node_ids and e.to_node in node_ids]
    engine.apply_config(reduced)
    engine.env.run(until=engine.now + 300)
    assert all(1 <= r.floor <= 2 for r in engine.robots.values())
    assert all(1 <= snap.floor <= 2 for snap in engine.robot_snapshots())


def test_db_roundtrip_keeps_floors_and_elevators() -> None:
    """층 수·층별 설비·엘리베이터가 DB에 저장되고 그대로 읽힌다."""
    db = DBManager(":memory:")
    db.initialize()
    db.save_factory_config(_factory(), changed_by="test")
    loaded = db.load_factory_config()
    assert loaded.floor_count == 3
    assert {s.id: s.floor for s in loaded.stations} == {"WS-01": 1, "WS-02": 1, "WS-03": 2, "WS-04": 2, "WS-05": 3}
    assert [(e.id, e.travel_time, e.capacity) for e in loaded.elevators] == [("EL-01", 4.0, 1)]
    assert {n.floor for n in loaded.nodes} == {1, 2, 3}
    assert sum(1 for n in loaded.nodes if n.node_type == "elevator") == 3


def test_v2_database_is_migrated_to_single_floor(tmp_path: Path) -> None:
    """층 개념이 없던 기존 DB는 컬럼이 보충되고 1층 구성으로 열린다."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE factory_profile (id INTEGER PRIMARY KEY, factory_name TEXT NOT NULL, floor_width REAL,
                                      floor_height REAL, grid_cell_size REAL, updated_at TIMESTAMP);
        CREATE TABLE work_stations (id TEXT PRIMARY KEY, label TEXT NOT NULL, x REAL NOT NULL, y REAL NOT NULL,
                                    task_type TEXT, avg_process_time REAL, updated_at TIMESTAMP);
        CREATE TABLE charging_stations (id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL, capacity INTEGER,
                                        updated_at TIMESTAMP);
        CREATE TABLE obstacles (id INTEGER PRIMARY KEY AUTOINCREMENT, obstacle_type TEXT NOT NULL, x1 REAL, y1 REAL,
                                x2 REAL, y2 REAL, updated_at TIMESTAMP);
        CREATE TABLE path_nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, x REAL NOT NULL, y REAL NOT NULL,
                                 node_type TEXT, updated_at TIMESTAMP);
        INSERT INTO factory_profile(id, factory_name, floor_width, floor_height, grid_cell_size)
            VALUES (1, 'old', 800, 600, 50);
        INSERT INTO work_stations(id, label, x, y) VALUES ('WS-01', 'a', 100, 100);
        INSERT INTO path_nodes(x, y) VALUES (50, 50);
        PRAGMA user_version = 2;
        """
    )
    conn.commit()
    conn.close()
    db = DBManager(path)
    db.initialize()
    factory = db.load_factory_config()
    assert factory.floor_count == 1
    assert [s.floor for s in factory.stations] == [1]
    assert [n.floor for n in factory.nodes] == [1]
    assert factory.elevators == []
    db.close()


if __name__ == "__main__":
    graph = PathGraph(_factory())
    print(graph.route_to_facility(100, 150, "WS-05", 1))


def test_demo_orders_force_elevator_use_in_both_schedulers() -> None:
    """데모 구성: 로봇은 1층 도크에서 출발하고, 위층 전용 주문을 처리하려면 두 스케줄러 모두 엘리베이터를 탄다."""
    from datetime import datetime, timedelta

    from utils.config_models import OrderRecord

    factory = _factory()
    epoch = datetime.now()
    order = OrderRecord("ORD-DEMO", "시연-상층", 30, priority="high", required_stations=["WS-03", "WS-04", "WS-05"],
                        deadline=epoch + timedelta(seconds=1800), created_at=epoch)
    for mode in ("baseline", "optimized"):
        engine = SimulationEngine(factory, mode, epoch=epoch, params={"task_rate": 0.01})
        assert {r.floor for r in engine.robots.values()} == {1}
        engine.sync_orders([order])
        engine.env.run(until=900)
        assert engine.elevators["EL-01"].rides >= 5, mode
        assert engine.orders["ORD-DEMO"].completed > 0, mode
