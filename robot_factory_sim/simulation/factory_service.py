"""FactoryService — GUI가 공장 구성/주문을 읽고 쓰는 단일 창구.

의존성 방향(gui → simulation → database)을 지키기 위해 GUI는 database 패키지 대신 이 클래스를 사용한다.
여기서 DB에 쓴 변경은 config_changelog에 기록되고, ConfigWatcher가 감지하여 SimWorker에 핫리로드된다.
모든 메서드는 짧은 SQLite 트랜잭션이므로 GUI 스레드에서 호출해도 된다.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject

import config
from database.config_watcher import ConfigWatcher
from database.db_manager import DBManager
from database.order_manager import OrderManager, ProductionEventWatcher
from database.seed_data import seed_default_factory
from utils.config_models import ChargingStationConfig, FactoryConfig, ObstacleConfig, OrderRecord, RobotConfig
from utils.event_types import Tables
from utils.layout_tools import link_facilities_to_nodes

AI_ACTOR: str = "ai_agent"


class FactoryService:
    """공장 구성 / 주문 / 프리셋 서비스."""

    def __init__(self, db_path: str | Path = config.DB_PATH) -> None:
        """서비스를 생성한다.

        Args:
            db_path: DB 파일 경로
        """
        self._db_path = str(db_path)
        self._db = DBManager(self._db_path)
        self._orders = OrderManager(self._db)

    @property
    def db_path(self) -> str:
        """DB 경로."""
        return self._db_path

    # ------------------------------------------------------------------
    # 초기화 / 상태
    # ------------------------------------------------------------------
    def ensure_database(self) -> bool:
        """스키마 적용·마이그레이션 후 비어 있으면 기본 구성을 시드한다.

        Returns:
            새로 시드했는지 여부
        """
        self._db.initialize()
        return seed_default_factory(self._db)

    def ping(self) -> bool:
        """DB 연결 확인.

        Returns:
            연결 가능 여부
        """
        return self._db.ping()

    def create_watchers(self, parent: QObject | None = None) -> tuple[ConfigWatcher, ProductionEventWatcher]:
        """DB 폴링 워처 2종을 생성한다 (시작은 호출자가 한다).

        Args:
            parent: Qt 부모

        Returns:
            (ConfigWatcher, ProductionEventWatcher)
        """
        return ConfigWatcher(self._db_path, parent=parent), ProductionEventWatcher(self._db_path, parent=parent)

    # ------------------------------------------------------------------
    # 공장 구성
    # ------------------------------------------------------------------
    def load_factory_config(self) -> FactoryConfig:
        """공장 구성을 읽는다.

        Returns:
            FactoryConfig
        """
        return self._db.load_factory_config()

    def save_factory_config(self, factory: FactoryConfig, changed_by: str = "config_editor") -> int:
        """공장 구성을 저장한다 (설비 ↔ 경로 노드 연결을 다시 계산).

        Args:
            factory: 저장할 구성
            changed_by: 변경 주체

        Returns:
            변경된 레코드 수
        """
        link_facilities_to_nodes(factory.nodes, factory.stations, factory.chargers)
        return self._db.save_factory_config(factory, changed_by)

    def list_presets(self) -> list[str]:
        """프리셋 이름 목록.

        Returns:
            이름 리스트
        """
        return self._db.list_presets()

    def save_preset(self, name: str, factory: FactoryConfig) -> None:
        """프리셋을 저장한다.

        Args:
            name: 프리셋 이름
            factory: 구성
        """
        self._db.save_preset(name, factory)

    def load_preset(self, name: str) -> FactoryConfig | None:
        """프리셋을 읽는다 (DB에는 반영하지 않음).

        Args:
            name: 프리셋 이름

        Returns:
            FactoryConfig 또는 None
        """
        return self._db.load_preset(name)

    def delete_preset(self, name: str) -> bool:
        """프리셋을 삭제한다.

        Args:
            name: 프리셋 이름

        Returns:
            삭제 여부
        """
        return self._db.delete_preset(name)

    @staticmethod
    def _next_id(prefix: str, existing: set[str]) -> str:
        """PREFIX-NN 형식의 미사용 ID."""
        index = 1
        while f"{prefix}-{index:02d}" in existing:
            index += 1
        return f"{prefix}-{index:02d}"

    def add_robots(self, count: int, changed_by: str = AI_ACTOR) -> list[str]:
        """로봇을 추가한다. 파라미터는 기존 첫 로봇(없으면 config 기본값)을 따른다.

        Args:
            count: 추가 대수
            changed_by: 변경 주체

        Returns:
            추가된 로봇 ID
        """
        robots = self._db.list_records(Tables.ROBOTS)
        template = robots[0] if robots else RobotConfig("R-00")
        existing = {r.id for r in robots}
        added = []
        for _ in range(max(0, count)):
            robot_id = self._next_id("R", existing)
            existing.add(robot_id)
            self._db.upsert_record(Tables.ROBOTS, RobotConfig(
                robot_id, template.robot_type, template.battery_capacity, template.move_speed,
                template.battery_drain_rate, template.charge_threshold, True,
            ), changed_by)
            added.append(robot_id)
        return added

    def add_charger(self, x: float, y: float, capacity: int = config.DEFAULT_CHARGER_CAPACITY,
                    changed_by: str = AI_ACTOR) -> str:
        """충전소를 추가한다.

        Args:
            x: 중심 X
            y: 중심 Y
            capacity: 슬롯 수
            changed_by: 변경 주체

        Returns:
            새 충전소 ID
        """
        factory = self._db.load_factory_config()
        self._check_inside(factory, x, y)
        charger_id = self._next_id("CS", {c.id for c in factory.chargers})
        factory.chargers.append(ChargingStationConfig(charger_id, float(x), float(y), capacity=int(capacity)))
        self.save_factory_config(factory, changed_by)
        return charger_id

    def move_station(self, station_id: str, x: float, y: float, changed_by: str = AI_ACTOR) -> bool:
        """스테이션을 옮긴다.

        Args:
            station_id: 스테이션 ID
            x: 새 중심 X
            y: 새 중심 Y
            changed_by: 변경 주체

        Returns:
            성공 여부
        """
        factory = self._db.load_factory_config()
        station = factory.station(station_id)
        if station is None:
            return False
        self._check_inside(factory, x, y)
        station.x, station.y = float(x), float(y)
        self.save_factory_config(factory, changed_by)
        return True

    def move_obstacle(self, obstacle_type: str, coords: tuple[float, float, float, float],
                      label: str | None = None, changed_by: str = AI_ACTOR) -> ObstacleConfig | None:
        """벽/문/장애물을 옮긴다. label이 없으면 해당 종류의 첫 항목을 옮긴다.

        Args:
            obstacle_type: wall | door | obstacle
            coords: (x1, y1, x2, y2)
            label: 대상 라벨 (선택)
            changed_by: 변경 주체

        Returns:
            수정된 장애물 또는 None
        """
        obstacles = self._db.list_records(Tables.OBSTACLES)
        target = next(
            (o for o in obstacles if o.obstacle_type == obstacle_type and (label is None or o.label == label)), None)
        if target is None:
            return None
        target.x1, target.y1, target.x2, target.y2 = (float(v) for v in coords)
        self._db.upsert_record(Tables.OBSTACLES, target, changed_by)
        return target

    def set_station_active(self, station_id: str, active: bool, changed_by: str = AI_ACTOR) -> bool:
        """스테이션 가동/고장 상태를 바꾼다.

        Args:
            station_id: 스테이션 ID
            active: 가동 여부
            changed_by: 변경 주체

        Returns:
            변경 여부
        """
        stations = {s.id: s for s in self._db.list_records(Tables.WORK_STATIONS)}
        station = stations.get(station_id)
        if station is None or station.is_active == active:
            return False
        station.is_active = active
        self._db.upsert_record(Tables.WORK_STATIONS, station, changed_by)
        return True

    @staticmethod
    def _check_inside(factory: FactoryConfig, x: float, y: float) -> None:
        """좌표가 공장 바닥 안인지 확인한다."""
        profile = factory.profile
        if not (0 <= x <= profile.floor_width and 0 <= y <= profile.floor_height) or math.isnan(x + y):
            raise ValueError(f"좌표 ({x:.0f}, {y:.0f})가 공장 범위(0~{profile.floor_width:.0f}, "
                             f"0~{profile.floor_height:.0f}) 밖입니다.")

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def list_orders(self, active_only: bool = False) -> list[OrderRecord]:
        """주문 목록.

        Args:
            active_only: 진행 중 주문만

        Returns:
            OrderRecord 리스트
        """
        return self._orders.list_orders(active_only)

    def create_order(
        self,
        product_type: str,
        quantity: int,
        priority: str = "normal",
        deadline: datetime | None = None,
        required_stations: list[str] | None = None,
        created_at: datetime | None = None,
        changed_by: str = "operator",
    ) -> OrderRecord:
        """주문을 등록한다 (critical이면 긴급 이벤트도 기록).

        Args:
            product_type: 제품명
            quantity: 수량
            priority: 우선순위
            deadline: 납기 (시뮬레이션 시계 기준)
            required_stations: 사용할 스테이션
            created_at: 생성 시각 (시뮬레이션 시계 기준)
            changed_by: 변경 주체

        Returns:
            생성된 주문
        """
        return self._orders.create_order(product_type, quantity, priority, deadline, required_stations, created_at,
                                         changed_by)

    def cancel_order(self, order_id: str, changed_by: str = "operator") -> bool:
        """주문을 취소한다.

        Args:
            order_id: 주문 ID
            changed_by: 변경 주체

        Returns:
            취소 여부
        """
        return self._orders.cancel_order(order_id, changed_by)

    def update_order_quantity(self, order_id: str, quantity: int, changed_by: str = "operator") -> bool:
        """주문 수량을 변경한다.

        Args:
            order_id: 주문 ID
            quantity: 새 수량
            changed_by: 변경 주체

        Returns:
            변경 여부
        """
        return self._orders.update_quantity(order_id, quantity, changed_by)

    def station_ids(self) -> list[str]:
        """스테이션 ID 목록.

        Returns:
            ID 리스트
        """
        return [s.id for s in self._db.list_records(Tables.WORK_STATIONS)]

    def describe(self) -> dict[str, Any]:
        """DB 요약 (디버그/상태 표시용).

        Returns:
            테이블별 개수
        """
        factory = self._db.load_factory_config()
        return {
            "stations": len(factory.stations),
            "chargers": len(factory.chargers),
            "robots": len(factory.robots),
            "orders": len(self._orders.list_orders()),
        }


if __name__ == "__main__":
    service = FactoryService(":memory:")
    print("seeded:", service.ensure_database())
    print(service.describe())
    print("added:", service.add_robots(2))
    print("charger:", service.add_charger(400, 300))
    print("door:", service.move_obstacle("door", (500, 0, 500, 100)))
