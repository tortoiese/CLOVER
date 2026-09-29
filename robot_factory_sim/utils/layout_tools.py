"""레이아웃 보조 함수 (그리드 경로망 생성 등).

seed_data와 공장 설정 에디터가 공용으로 사용한다.
"""

from __future__ import annotations

import math

from utils.config_models import ChargingStationConfig, PathEdgeConfig, PathNodeConfig, WorkStationConfig


def build_grid_network(
    width: float,
    height: float,
    spacing: float,
    stations: list[WorkStationConfig],
    chargers: list[ChargingStationConfig],
) -> tuple[list[PathNodeConfig], list[PathEdgeConfig]]:
    """격자형 경로망을 만든다. 노드/엣지 ID는 음수 임시 ID로 부여된다.

    각 스테이션·충전소에서 가장 가까운 노드를 해당 설비의 접근 노드로 연결한다.

    Args:
        width: 공장 너비 (px)
        height: 공장 높이 (px)
        spacing: 노드 간격 (px)
        stations: 작업 스테이션 목록
        chargers: 충전소 목록

    Returns:
        (노드 리스트, 엣지 리스트)
    """
    offset = spacing / 2.0
    cols = max(1, int((width - offset) // spacing) + 1)
    rows = max(1, int((height - offset) // spacing) + 1)
    nodes: list[PathNodeConfig] = []
    index: dict[tuple[int, int], int] = {}
    next_id = -1
    for r in range(rows):
        for c in range(cols):
            x, y = offset + c * spacing, offset + r * spacing
            if x > width or y > height:
                continue
            nodes.append(PathNodeConfig(next_id, x, y))
            index[(r, c)] = next_id
            next_id -= 1

    edges: list[PathEdgeConfig] = []
    edge_id = -1
    for (r, c), node_id in index.items():
        for dr, dc in ((0, 1), (1, 0)):
            other = index.get((r + dr, c + dc))
            if other is not None:
                edges.append(PathEdgeConfig(edge_id, node_id, other, spacing, True))
                edge_id -= 1

    link_facilities_to_nodes(nodes, stations, chargers)
    return nodes, edges


def link_facilities_to_nodes(
    nodes: list[PathNodeConfig],
    stations: list[WorkStationConfig],
    chargers: list[ChargingStationConfig],
) -> None:
    """각 설비의 최근접 노드를 station/charger 노드로 표시한다 (in-place).

    Args:
        nodes: 경로 노드 목록
        stations: 작업 스테이션 목록
        chargers: 충전소 목록
    """
    for node in nodes:
        node.node_type = "waypoint"
        node.linked_station_id = None
    if not nodes:
        return
    facilities: list[tuple[str, str, float, float]] = [("station", s.id, s.x, s.y) for s in stations]
    facilities += [("charger", c.id, c.x, c.y) for c in chargers]
    for node_type, facility_id, fx, fy in facilities:
        candidates = [n for n in nodes if n.linked_station_id is None] or nodes
        nearest = min(candidates, key=lambda n: math.hypot(n.x - fx, n.y - fy))
        nearest.node_type = node_type
        nearest.linked_station_id = facility_id
