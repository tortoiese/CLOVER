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
    edge_clearance: float = config.EDGE_CLEARANCE
    side_clearance: float = config.SIDE_CLEARANCE
    floor_travel_sec: float = config.FLOOR_TRAVEL_SEC
    door_time_sec: float = config.DOOR_TIME_SEC
    floor_count: int = config.DEFAULT_FLOOR_COUNT


@dataclass
class WorkStationConfig:
    """작업 스테이션 설정. (x, y)는 중심 좌표, floor는 1부터 시작하는 층 번호."""

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
    floor: int = 1


@dataclass
class ChargingStationConfig:
    """충전 스테이션 설정. (x, y)는 중심 좌표."""

    id: str
    x: float
    y: float
    capacity: int = config.DEFAULT_CHARGER_CAPACITY
    charge_rate: float = config.DEFAULT_CHARGE_RATE
    is_active: bool = True
    floor: int = 1


@dataclass
class ElevatorConfig:
    """엘리베이터 설정. 모든 층의 같은 (x, y)를 관통하는 승강로이며 (x, y)는 중심 좌표."""

    id: str
    label: str
    x: float
    y: float
    width: float = config.DEFAULT_ELEVATOR_SIZE
    height: float = config.DEFAULT_ELEVATOR_SIZE
    travel_time: float = config.FLOOR_TRAVEL_SEC
    capacity: int = config.DEFAULT_ELEVATOR_CAPACITY
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
    width: float = config.DEFAULT_ROBOT_WIDTH
    length: float = config.DEFAULT_ROBOT_LENGTH


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
    floor: int = 1


@dataclass
class PathNodeConfig:
    """경로 노드. id가 음수면 신규 항목.

    node_type이 "intersection"이면 동시에 하나만 통과 가능한 교차점이다.
    node_type이 "elevator"이면 linked_station_id를 엘리베이터 그룹(샤프트) ID로 쓰고,
    같은 그룹·다른 floor를 가진 두 노드를 연결하는 엣지가 엘리베이터 구간이 된다.
    """

    id: int
    x: float
    y: float
    node_type: str = "waypoint"
    linked_station_id: str | None = None
    floor: int = 1
    capacity: int | None = None  # 엘리베이터 노드의 동시 운행 차량 수 (None이면 config fallback)


@dataclass
class PathEdgeConfig:
    """경로 엣지. id가 음수면 신규 항목."""

    id: int
    from_node: int
    to_node: int
    distance: float | None = None
    is_bidirectional: bool = True
    width: float = config.DEFAULT_EDGE_WIDTH


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
    elevators: list[ElevatorConfig] = field(default_factory=list)

    @property
    def floor_count(self) -> int:
        """층 수 (1 이상)."""
        return max(1, int(self.profile.floor_count))

    def sync_facility_floors(self) -> None:
        """스테이션/충전소의 층을 연결된 경로 노드의 층에 맞춘다 (층 개념 없이 만든 구성 호환).

        예전 물리 시나리오는 위층 설비를 노드의 floor로만 표시했으므로, 노드가 가리키는 층을 설비의 층으로 삼는다.
        """
        node_floor = {n.linked_station_id: n.floor for n in self.nodes
                      if n.linked_station_id and n.node_type in ("station", "charger")}
        for facility in (*self.stations, *self.chargers):
            if facility.id in node_floor:
                facility.floor = node_floor[facility.id]

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
        factory = cls(
            profile=FactoryProfile(**data.get("profile", {})),
            stations=[WorkStationConfig(**s) for s in data.get("stations", [])],
            chargers=[ChargingStationConfig(**c) for c in data.get("chargers", [])],
            robots=[RobotConfig(**r) for r in data.get("robots", [])],
            obstacles=[ObstacleConfig(**o) for o in data.get("obstacles", [])],
            nodes=[PathNodeConfig(**n) for n in data.get("nodes", [])],
            edges=[PathEdgeConfig(**e) for e in data.get("edges", [])],
            elevators=[ElevatorConfig(**e) for e in data.get("elevators", [])],
        )
        factory.sync_facility_floors()
        return factory

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
