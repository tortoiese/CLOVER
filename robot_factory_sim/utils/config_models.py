"""공장 구성 / 주문 데이터 모델.

DB 레이어가 생성하고 simulation / gui 레이어가 소비하는 공용 dataclass들.
GUI가 database 패키지를 직접 참조하지 않도록 utils에 둔다.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import config
from utils.event_types import OrderStatus


@dataclass
class FactoryProfile:
    """공장 메타 정보."""

    id: int = 1
    factory_name: str = config.DEFAULT_FACTORY_NAME
    floor_width: float = config.DEFAULT_FLOOR_WIDTH
    floor_height: float = config.DEFAULT_FLOOR_HEIGHT
    grid_cell_size: float = config.DEFAULT_GRID_CELL_SIZE


@dataclass
class WorkStationConfig:
    """작업 스테이션 설정. (x, y)는 중심 좌표."""

    id: str
    label: str
    x: float
    y: float
    width: float = config.DEFAULT_STATION_WIDTH
    height: float = config.DEFAULT_STATION_HEIGHT
    task_type: str = config.DEFAULT_TASK_TYPE
    avg_process_time: float = config.DEFAULT_PROCESS_TIME
    process_time_std: float = config.DEFAULT_PROCESS_TIME_STD
    is_active: bool = True


@dataclass
class ChargingStationConfig:
    """충전 스테이션 설정. (x, y)는 중심 좌표."""

    id: str
    x: float
    y: float
    capacity: int = config.DEFAULT_CHARGER_CAPACITY
    charge_rate: float = config.DEFAULT_CHARGE_RATE
    is_active: bool = True


@dataclass
class RobotConfig:
    """로봇 설정."""

    id: str
    robot_type: str = config.DEFAULT_ROBOT_TYPE
    battery_capacity: float = config.DEFAULT_BATTERY_CAPACITY
    move_speed: float = config.DEFAULT_MOVE_SPEED
    battery_drain_rate: float = config.DEFAULT_BATTERY_DRAIN_RATE
    charge_threshold: float = config.DEFAULT_CHARGE_THRESHOLD
    is_active: bool = True


@dataclass
class ObstacleConfig:
    """벽/문/장애물. (x1,y1)-(x2,y2) 선분 또는 사각형. id가 음수면 아직 DB에 없는 신규 항목."""

    id: int
    obstacle_type: str
    x1: float
    y1: float
    x2: float
    y2: float
    is_passable: bool = False
    label: str = ""


@dataclass
class PathNodeConfig:
    """경로 노드. id가 음수면 신규 항목."""

    id: int
    x: float
    y: float
    node_type: str = "waypoint"
    linked_station_id: str | None = None


@dataclass
class PathEdgeConfig:
    """경로 엣지. id가 음수면 신규 항목."""

    id: int
    from_node: int
    to_node: int
    distance: float | None = None
    is_bidirectional: bool = True


@dataclass
class FactoryConfig:
    """공장 전체 구성 스냅샷."""

    profile: FactoryProfile = field(default_factory=FactoryProfile)
    stations: list[WorkStationConfig] = field(default_factory=list)
    chargers: list[ChargingStationConfig] = field(default_factory=list)
    robots: list[RobotConfig] = field(default_factory=list)
    obstacles: list[ObstacleConfig] = field(default_factory=list)
    nodes: list[PathNodeConfig] = field(default_factory=list)
    edges: list[PathEdgeConfig] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON 직렬화 가능한 dict로 변환한다.

        Returns:
            구성 전체를 담은 dict
        """
        return asdict(self)

    def to_json(self) -> str:
        """JSON 문자열로 변환한다.

        Returns:
            JSON 문자열
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FactoryConfig:
        """dict에서 구성을 복원한다.

        Args:
            data: to_dict()로 만든 dict

        Returns:
            복원된 FactoryConfig
        """
        return cls(
            profile=FactoryProfile(**data.get("profile", {})),
            stations=[WorkStationConfig(**s) for s in data.get("stations", [])],
            chargers=[ChargingStationConfig(**c) for c in data.get("chargers", [])],
            robots=[RobotConfig(**r) for r in data.get("robots", [])],
            obstacles=[ObstacleConfig(**o) for o in data.get("obstacles", [])],
            nodes=[PathNodeConfig(**n) for n in data.get("nodes", [])],
            edges=[PathEdgeConfig(**e) for e in data.get("edges", [])],
        )

    @classmethod
    def from_json(cls, text: str) -> FactoryConfig:
        """JSON 문자열에서 구성을 복원한다.

        Args:
            text: to_json()으로 만든 문자열

        Returns:
            복원된 FactoryConfig
        """
        return cls.from_dict(json.loads(text))

    def station(self, station_id: str) -> WorkStationConfig | None:
        """ID로 스테이션을 찾는다.

        Args:
            station_id: 스테이션 ID

        Returns:
            스테이션 설정 또는 None
        """
        return next((s for s in self.stations if s.id == station_id), None)


@dataclass
class OrderRecord:
    """생산 주문 레코드."""

    order_id: str
    product_type: str
    quantity: int
    completed: int = 0
    priority: str = "normal"
    deadline: datetime | None = None
    required_stations: list[str] = field(default_factory=list)
    status: str = OrderStatus.PENDING
    created_at: datetime | None = None

    @property
    def remaining(self) -> int:
        """남은 수량."""
        return max(0, self.quantity - self.completed)

    @property
    def is_active(self) -> bool:
        """진행 중인 주문인지 여부."""
        return self.status in OrderStatus.ACTIVE
