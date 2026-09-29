"""경로 그래프 + Dijkstra 최단경로.

- 노드/엣지는 DB(path_nodes, path_edges) 기반.
- 통과 불가 장애물(벽/장애물)과 교차하는 엣지는 차단된다. 단, 교차 지점을 문(is_passable=1)이 덮으면 통과 가능.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass

from utils.config_models import FactoryConfig, ObstacleConfig

WALL_HALF_THICKNESS: float = 4.0
DOOR_HALF_THICKNESS: float = 8.0

Point = tuple[float, float]


@dataclass(frozen=True)
class Route:
    """이동 경로."""

    points: list[Point]
    length: float  # px


def _segment_intersects_segment(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """두 선분의 교차 여부."""

    def orient(a: Point, b: Point, c: Point) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: Point, b: Point, c: Point) -> bool:
        return min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and \
            min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9

    d1, d2 = orient(q1, q2, p1), orient(q1, q2, p2)
    d3, d4 = orient(p1, p2, q1), orient(p1, p2, q2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    return (abs(d1) < 1e-9 and on_segment(q1, q2, p1)) or (abs(d2) < 1e-9 and on_segment(q1, q2, p2)) or \
        (abs(d3) < 1e-9 and on_segment(p1, p2, q1)) or (abs(d4) < 1e-9 and on_segment(p1, p2, q2))


def _rect_of(obstacle: ObstacleConfig) -> tuple[float, float, float, float]:
    """장애물의 충돌 사각형 (선분형은 두께를 부여)."""
    pad = DOOR_HALF_THICKNESS if obstacle.is_passable else WALL_HALF_THICKNESS
    x1, x2 = sorted((obstacle.x1, obstacle.x2))
    y1, y2 = sorted((obstacle.y1, obstacle.y2))
    if obstacle.obstacle_type == "obstacle" and x2 - x1 > 1 and y2 - y1 > 1:
        return x1, y1, x2, y2
    return x1 - pad, y1 - pad, x2 + pad, y2 + pad


def _clip_segment_to_rect(p1: Point, p2: Point, rect: tuple[float, float, float, float]) -> tuple[float, float] | None:
    """Liang–Barsky: 선분이 사각형 안에 있는 매개변수 구간 [t0, t1]. 없으면 None."""
    x_min, y_min, x_max, y_max = rect
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, p1[0] - x_min), (dx, x_max - p1[0]), (-dy, p1[1] - y_min), (dy, y_max - p1[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return t0, t1


def edge_blocked(p1: Point, p2: Point, obstacles: list[ObstacleConfig]) -> bool:
    """엣지가 통과 불가 장애물에 막혔는지 판정한다.

    Args:
        p1: 시작점
        p2: 끝점
        obstacles: 장애물 목록

    Returns:
        차단 여부
    """
    doors = [_rect_of(o) for o in obstacles if o.is_passable]
    for obstacle in obstacles:
        if obstacle.is_passable:
            continue
        span = _clip_segment_to_rect(p1, p2, _rect_of(obstacle))
        if span is None:
            continue
        # 차단 구간의 중간점이 문 영역 안이면 통과 허용
        mid_t = (span[0] + span[1]) / 2.0
        mid = (p1[0] + (p2[0] - p1[0]) * mid_t, p1[1] + (p2[1] - p1[1]) * mid_t)
        if any(d[0] <= mid[0] <= d[2] and d[1] <= mid[1] <= d[3] for d in doors):
            continue
        return True
    return False


class PathGraph:
    """경로망 그래프."""

    def __init__(self, factory: FactoryConfig) -> None:
        """구성으로부터 그래프를 만든다.

        Args:
            factory: 공장 구성
        """
        self.nodes: dict[int, Point] = {n.id: (n.x, n.y) for n in factory.nodes}
        self.adjacency: dict[int, list[tuple[int, float]]] = {n: [] for n in self.nodes}
        self.edges: list[tuple[int, int, bool]] = []  # (a, b, blocked)
        self.facility_nodes: dict[str, int] = {}
        self._dist_cache: dict[int, tuple[dict[int, float], dict[int, int]]] = {}

        for edge in factory.edges:
            if edge.from_node not in self.nodes or edge.to_node not in self.nodes:
                continue
            a, b = self.nodes[edge.from_node], self.nodes[edge.to_node]
            blocked = edge_blocked(a, b, factory.obstacles)
            self.edges.append((edge.from_node, edge.to_node, blocked))
            if blocked:
                continue
            length = math.hypot(a[0] - b[0], a[1] - b[1])
            self.adjacency[edge.from_node].append((edge.to_node, length))
            if edge.is_bidirectional:
                self.adjacency[edge.to_node].append((edge.from_node, length))

        linked = {n.linked_station_id: n.id for n in factory.nodes if n.linked_station_id}
        facilities = [(s.id, s.x, s.y) for s in factory.stations] + [(c.id, c.x, c.y) for c in factory.chargers]
        for facility_id, fx, fy in facilities:
            node = linked.get(facility_id)
            self.facility_nodes[facility_id] = node if node is not None else self.nearest_node(fx, fy)

    def nearest_node(self, x: float, y: float) -> int:
        """좌표에서 가장 가까운 노드 (그래프가 비면 -1).

        Args:
            x: X 좌표
            y: Y 좌표

        Returns:
            노드 ID
        """
        if not self.nodes:
            return -1
        return min(self.nodes, key=lambda n: math.hypot(self.nodes[n][0] - x, self.nodes[n][1] - y))

    def _dijkstra(self, source: int) -> tuple[dict[int, float], dict[int, int]]:
        """단일 출발 Dijkstra (결과 캐시)."""
        cached = self._dist_cache.get(source)
        if cached is not None:
            return cached
        dist: dict[int, float] = {source: 0.0}
        prev: dict[int, int] = {}
        heap: list[tuple[float, int]] = [(0.0, source)]
        while heap:
            d, node = heapq.heappop(heap)
            if d > dist.get(node, math.inf):
                continue
            for neighbor, weight in self.adjacency.get(node, []):
                nd = d + weight
                if nd < dist.get(neighbor, math.inf):
                    dist[neighbor] = nd
                    prev[neighbor] = node
                    heapq.heappush(heap, (nd, neighbor))
        self._dist_cache[source] = (dist, prev)
        return dist, prev

    def shortest_path(self, source: int, target: int) -> tuple[list[int], float] | None:
        """노드 간 최단경로.

        Args:
            source: 출발 노드
            target: 도착 노드

        Returns:
            (노드 ID 리스트, 거리) 또는 도달 불가 시 None
        """
        if source not in self.nodes or target not in self.nodes:
            return None
        dist, prev = self._dijkstra(source)
        if target not in dist:
            return None
        path = [target]
        while path[-1] != source:
            path.append(prev[path[-1]])
        path.reverse()
        return path, dist[target]

    def route_to_facility(self, x: float, y: float, facility_id: str) -> Route | None:
        """현재 위치에서 설비 접근 노드까지 경로.

        Args:
            x: 현재 X
            y: 현재 Y
            facility_id: 스테이션/충전소 ID

        Returns:
            Route 또는 도달 불가 시 None
        """
        target = self.facility_nodes.get(facility_id)
        if target is None or target < 0:
            return None
        start = self.nearest_node(x, y)
        result = self.shortest_path(start, target)
        if result is None:
            return None
        nodes, length = result
        points = [self.nodes[n] for n in nodes]
        start_gap = math.hypot(points[0][0] - x, points[0][1] - y)
        if start_gap < 1e-6:
            points = points[1:]
        return Route(points=points, length=length + start_gap)

    def distance_to_facility(self, x: float, y: float, facility_id: str) -> float:
        """현재 위치에서 설비까지의 경로 거리 (도달 불가 시 inf).

        Args:
            x: 현재 X
            y: 현재 Y
            facility_id: 스테이션/충전소 ID

        Returns:
            거리 (px)
        """
        target = self.facility_nodes.get(facility_id)
        if target is None or target < 0 or not self.nodes:
            return math.inf
        start = self.nearest_node(x, y)
        dist, _ = self._dijkstra(start)
        if target not in dist:
            return math.inf
        sx, sy = self.nodes[start]
        return dist[target] + math.hypot(sx - x, sy - y)

    def facility_point(self, facility_id: str) -> Point | None:
        """설비 접근 노드 좌표.

        Args:
            facility_id: 설비 ID

        Returns:
            좌표 또는 None
        """
        node = self.facility_nodes.get(facility_id)
        return self.nodes.get(node) if node is not None else None


if __name__ == "__main__":
    from database.seed_data import build_default_config

    default = build_default_config()
    graph = PathGraph(default)
    blocked = sum(1 for e in graph.edges if e[2])
    print(f"nodes={len(graph.nodes)} edges={len(graph.edges)} blocked={blocked}")
    print("WS-01 → WS-05:", graph.route_to_facility(100, 150, "WS-05"))
