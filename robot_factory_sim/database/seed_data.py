"""기본 공장 구성 시드 데이터 (3층, 스테이션 5개, 충전소 3개, 엘리베이터 1대, 로봇 5대)."""

from __future__ import annotations

from datetime import datetime

import config
from database.db_manager import DBManager
from utils.config_models import (
    ChargingStationConfig,
    ElevatorConfig,
    FactoryConfig,
    FactoryProfile,
    ObstacleConfig,
    OrderRecord,
    RobotConfig,
    WorkStationConfig,
)
from utils.layout_tools import build_grid_network

SEED_NODE_SPACING: float = 100.0


DEMO_PRESET_NAME: str = "3층 데모 공장"


def build_default_config() -> FactoryConfig:
    """기본 공장 구성(3층 + 엘리베이터)을 만든다.

    1층 조립·용접, 2층 도장·검사, 3층 포장 라인이며 엘리베이터 EL-01이 세 층을 잇는다.

    Returns:
        FactoryConfig (노드/엣지/장애물 ID는 음수 임시 ID)
    """
    profile = FactoryProfile(
        id=1,
        factory_name="클로봇 데모 공장",
        floor_width=config.DEFAULT_FLOOR_WIDTH,
        floor_height=config.DEFAULT_FLOOR_HEIGHT,
        grid_cell_size=config.DEFAULT_GRID_CELL_SIZE,
        floor_count=3,
    )
    stations = [
        WorkStationConfig("WS-01", "조립 A", 100, 100, task_type="assembly", avg_process_time=6.0, floor=1),
        WorkStationConfig("WS-02", "용접", 300, 100, task_type="welding", avg_process_time=8.0, process_time_std=1.5,
                          floor=1),
        WorkStationConfig("WS-03", "도장", 100, 500, task_type="painting", avg_process_time=7.0, floor=2),
        WorkStationConfig("WS-04", "검사", 300, 500, task_type="inspection", avg_process_time=4.0,
                          process_time_std=0.5, floor=2),
        WorkStationConfig("WS-05", "포장", 700, 100, task_type="packaging", avg_process_time=5.0, floor=3),
    ]
    chargers = [
        ChargingStationConfig("CS-01", 700, 250, capacity=2, charge_rate=2.0, floor=1),
        ChargingStationConfig("CS-02", 300, 300, capacity=1, charge_rate=2.5, floor=2),
        ChargingStationConfig("CS-03", 200, 300, capacity=1, charge_rate=2.0, floor=3),
    ]
    elevators = [ElevatorConfig("EL-01", "화물 엘리베이터", 650, 550)]
    robots = [
        RobotConfig(f"R-{i:02d}", move_speed=2.0, battery_drain_rate=0.35, charge_threshold=20.0)
        for i in range(1, 6)
    ]
    obstacles = [
        ObstacleConfig(-1, "wall", 500, 0, 500, 600, False, "구획 벽", 1),
        ObstacleConfig(-2, "door", 500, 250, 500, 350, True, "출입문", 1),
        ObstacleConfig(-3, "obstacle", 190, 330, 210, 470, False, "팔레트 랙", 1),
        ObstacleConfig(-4, "wall", 500, 0, 500, 600, False, "구획 벽", 2),
        ObstacleConfig(-5, "door", 500, 250, 500, 350, True, "출입문", 2),
        ObstacleConfig(-6, "obstacle", 190, 330, 210, 470, False, "도장 부스", 2),
        ObstacleConfig(-7, "wall", 450, 200, 800, 200, False, "포장 구역 벽", 3),
        ObstacleConfig(-8, "door", 600, 200, 700, 200, True, "포장 구역 문", 3),
    ]
    nodes, edges = build_grid_network(profile.floor_width, profile.floor_height, SEED_NODE_SPACING, stations,
                                      chargers, profile.floor_count, elevators)
    return FactoryConfig(profile, stations, chargers, robots, obstacles, nodes, edges, elevators)


def build_default_orders() -> list[OrderRecord]:
    """기본 생산 주문을 만든다.

    Returns:
        OrderRecord 리스트 (order_id는 삽입 시 부여)
    """
    now = datetime.now()
    return [
        OrderRecord("", "제품A", 90, priority="normal", required_stations=["WS-01", "WS-03", "WS-05"], created_at=now),
        OrderRecord("", "제품C", 80, priority="high", required_stations=["WS-03", "WS-04"], created_at=now),
    ]


def ensure_demo_preset(db: DBManager) -> None:
    """3층 데모 구성을 프리셋으로 남긴다 (기존 DB에서도 공장 설정 > 프리셋으로 불러올 수 있게).

    Args:
        db: 초기화된 DBManager
    """
    if DEMO_PRESET_NAME not in db.list_presets():
        db.save_preset(DEMO_PRESET_NAME, build_default_config(), changed_by="seed")


def seed_default_factory(db: DBManager) -> bool:
    """DB가 비어 있으면 기본 구성을 삽입한다.

    Args:
        db: 초기화된 DBManager

    Returns:
        시드 삽입 여부
    """
    if db.is_seeded():
        ensure_demo_preset(db)
        return False
    db.save_factory_config(build_default_config(), changed_by="seed")
    ensure_demo_preset(db)
    for order in build_default_orders():
        order.order_id = db.next_order_id()
        db.insert_order(order, changed_by="seed")
    return True


if __name__ == "__main__":
    seed_db = DBManager(":memory:")
    seed_db.initialize()
    print("seeded:", seed_default_factory(seed_db))
    factory = seed_db.load_factory_config()
    print(len(factory.stations), "stations /", len(factory.nodes), "nodes /", len(factory.edges), "edges /",
          factory.floor_count, "floors /", len(factory.elevators), "elevators")
