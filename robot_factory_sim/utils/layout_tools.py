"""레이아웃 보조 함수 (그리드 경로망 생성 등).

seed_data와 공장 설정 에디터가 공용으로 사용한다.
"""

from __future__ import annotations

import math

from utils.config_models import (
    ChargingStationConfig,
    ElevatorConfig,
    PathEdgeConfig,
    PathNodeConfig,
    WorkStationConfig,
)

ELEVATOR_NODE_TYPE: str = "elevator"


def build_grid_network(
    width: float,
    height: float,
    spacing: float,
    stations: list[WorkStationConfig],
    chargers: list[ChargingStationConfig],
    floors: int = 1,
    elevators: list[ElevatorConfig] | None = None,
    only_floor: int | None = None,
    first_id: int = -1,
) -> tuple[list[PathNodeConfig], list[PathEdgeConfig]]:
    """층마다 격자형 경로망을 만든다. 노드/엣지 ID는 음수 임시 ID로 부여된다.

    각 스테이션·충전소·엘리베이터에서 같은 층의 가장 가까운 노드를 해당 설비의 접근 노드로 연결한다.

    Args:
        width: 공장 너비 (px)
        height: 공장 높이 (px)
        spacing: 노드 간격 (px)
        stations: 작업 스테이션 목록
        chargers: 충전소 목록
        floors: 층 수
        elevators: 엘리베이터 목록
        only_floor: 지정하면 그 층의 경로망만 만든다
        first_id: 부여를 시작할 임시 ID (음수). 기존 임시 ID와 겹치지 않게 넘긴다

    Returns:
        (노드 리스트, 엣지 리스트)
    """
    offset = spacing / 2.0
    cols = max(1, int((width - offset) // spacing) + 1)
    rows = max(1, int((height - offset) // spacing) + 1)
    nodes: list[PathNodeConfig] = []
    edges: list[PathEdgeConfig] = []
    next_id = first_id
    edge_id = first_id
    targets = [only_floor] if only_floor is not None else list(range(1, max(1, floors) + 1))
    for floor in targets:
        index: dict[tuple[int, int], int] = {}
        for r in range(rows):
            for c in range(cols):
                x, y = offset + c * spacing, offset + r * spacing
                if x > width or y > height:
                    continue
                nodes.append(PathNodeConfig(next_id, x, y, floor=floor))
                index[(r, c)] = next_id
                next_id -= 1
        for (r, c), node_id in index.items():
            for dr, dc in ((0, 1), (1, 0)):
                other = index.get((r + dr, c + dc))
                if other is not None:
                    edges.append(PathEdgeConfig(edge_id, node_id, other, spacing, True))
                    edge_id -= 1

    link_facilities_to_nodes(nodes, stations, chargers, elevators or [])
    return nodes, edges


def _is_preserved(node: PathNodeConfig, elevator_ids: set[str]) -> bool:
    """설비 연결을 다시 계산할 때 그대로 둘 노드: 교차점, 그리고 ElevatorConfig 없이 노드 그룹으로 정의된 엘리베이터."""
    if node.node_type == "intersection":
        return True
    return node.node_type == "elevator" and bool(node.linked_station_id) and node.linked_station_id not in elevator_ids


def link_facilities_to_nodes(
    nodes: list[PathNodeConfig],
    stations: list[WorkStationConfig],
    chargers: list[ChargingStationConfig],
    elevators: list[ElevatorConfig] | None = None,
) -> None:
    """각 설비의 같은 층 최근접 노드를 station/charger/elevator 노드로 표시한다 (in-place).

    엘리베이터는 모든 층에 접근 노드를 하나씩 가지며, 가장 먼저 노드를 배정받는다.

    Args:
        nodes: 경로 노드 목록
        stations: 작업 스테이션 목록
        chargers: 충전소 목록
        elevators: 엘리베이터 목록
    """
    elevator_ids = {e.id for e in elevators or []}
    for node in nodes:
        if _is_preserved(node, elevator_ids):
            continue
        node.node_type = "waypoint"
        node.linked_station_id = None
    if not nodes:
        return
    floors = sorted({n.floor for n in nodes})
    facilities: list[tuple[str, str, float, float, int]] = []
    for elevator in elevators or []:
        facilities += [(ELEVATOR_NODE_TYPE, elevator.id, elevator.x, elevator.y, floor) for floor in floors]
    facilities += [("station", s.id, s.x, s.y, s.floor) for s in stations]
    facilities += [("charger", c.id, c.x, c.y, c.floor) for c in chargers]
    for node_type, facility_id, fx, fy, floor in facilities:
        on_floor = [n for n in nodes if n.floor == floor]
        if not on_floor:
            continue
        free = [n for n in on_floor if n.node_type != "intersection"] or on_floor
        candidates = [n for n in free if n.linked_station_id is None] or free
        nearest = min(candidates, key=lambda n: math.hypot(n.x - fx, n.y - fy))
        nearest.node_type = node_type
        nearest.linked_station_id = facility_id
