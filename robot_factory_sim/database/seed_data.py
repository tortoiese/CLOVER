"""기본 공장 구성 시드 데이터 (스테이션 5개, 충전소 2개, 로봇 5대)."""

from __future__ import annotations

from datetime import datetime

import config
from database.db_manager import DBManager
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    FactoryProfile,
    ObstacleConfig,
    OrderRecord,
    RobotConfig,
    WorkStationConfig,
)
from utils.layout_tools import build_grid_network

SEED_NODE_SPACING: float = 100.0


def build_default_config() -> FactoryConfig:
    """기본 공장 구성을 만든다.

    Returns:
        FactoryConfig (노드/엣지/장애물 ID는 음수 임시 ID)
    """
    profile = FactoryProfile(
        id=1,
        factory_name="클로봇 데모 공장",
        floor_width=config.DEFAULT_FLOOR_WIDTH,
        floor_height=config.DEFAULT_FLOOR_HEIGHT,
        grid_cell_size=config.DEFAULT_GRID_CELL_SIZE,
    )
    stations = [
        WorkStationConfig("WS-01", "조립 A", 100, 100, task_type="assembly", avg_process_time=6.0),
        WorkStationConfig("WS-02", "용접", 300, 100, task_type="welding", avg_process_time=8.0, process_time_std=1.5),
        WorkStationConfig("WS-03", "도장", 100, 500, task_type="painting", avg_process_time=7.0),
        WorkStationConfig("WS-04", "검사", 300, 500, task_type="inspection", avg_process_time=4.0,
                          process_time_std=0.5),
        WorkStationConfig("WS-05", "포장", 700, 100, task_type="packaging", avg_process_time=5.0),
    ]
    chargers = [
        ChargingStationConfig("CS-01", 700, 500, capacity=2, charge_rate=2.0),
        ChargingStationConfig("CS-02", 300, 300, capacity=1, charge_rate=2.5),
    ]
    robots = [
        RobotConfig(f"R-{i:02d}", move_speed=2.0, battery_drain_rate=0.35, charge_threshold=20.0)
        for i in range(1, 6)
    ]
    obstacles = [
        ObstacleConfig(-1, "wall", 500, 0, 500, 600, False, "구획 벽"),
        ObstacleConfig(-2, "door", 500, 250, 500, 350, True, "출입문"),
        ObstacleConfig(-3, "obstacle", 190, 330, 210, 470, False, "팔레트 랙"),
    ]
    nodes, edges = build_grid_network(profile.floor_width, profile.floor_height, SEED_NODE_SPACING, stations,
                                      chargers)
    return FactoryConfig(profile, stations, chargers, robots, obstacles, nodes, edges)


def build_default_orders() -> list[OrderRecord]:
    """기본 생산 주문을 만든다.

    Returns:
        OrderRecord 리스트 (order_id는 삽입 시 부여)
    """
    now = datetime.now()
    return [
        OrderRecord("", "제품A", 120, priority="normal", required_stations=["WS-01", "WS-02"], created_at=now),
        OrderRecord("", "제품C", 80, priority="high", required_stations=["WS-03", "WS-04"], created_at=now),
    ]


def seed_default_factory(db: DBManager) -> bool:
    """DB가 비어 있으면 기본 구성을 삽입한다.

    Args:
        db: 초기화된 DBManager

    Returns:
        시드 삽입 여부
    """
    if db.is_seeded():
        return False
    db.save_factory_config(build_default_config(), changed_by="seed")
    for order in build_default_orders():
        order.order_id = db.next_order_id()
        db.insert_order(order, changed_by="seed")
    return True


if __name__ == "__main__":
    seed_db = DBManager(":memory:")
    seed_db.initialize()
    print("seeded:", seed_default_factory(seed_db))
    factory = seed_db.load_factory_config()
    print(len(factory.stations), "stations /", len(factory.nodes), "nodes /", len(factory.edges), "edges")
