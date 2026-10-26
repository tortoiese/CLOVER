"""경로 그래프 + Dijkstra 최단경로.

- 노드/엣지는 DB(path_nodes, path_edges) 기반.
- 통과 불가 장애물(벽/장애물)과 교차하는 엣지는 차단된다. 단, 교차 지점을 문(is_passable=1)이 덮으면 통과 가능.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from itertools import pairwise

import config
from utils.config_models import (
    ElevatorConfig,
    FactoryConfig,
    ObstacleConfig,
    PathEdgeConfig,
)

WALL_HALF_THICKNESS: float = 4.0
DOOR_HALF_THICKNESS: float = 8.0

Point = tuple[float, float]


@dataclass(frozen=True)
class Route:
    """이동 경로.

    points[i]는 이동할 좌표, floors[i] / lifts[i]는 그 점에 도착했을 때의 층과 그 점으로 올 때 탄 엘리베이터 ID이다.
    nodes[i]는 points[i]에 해당하는 그래프 노드 ID(현재 위치에서 그래프에 올라타기 전의 연결 구간이면 그 시작점은
    그래프 노드가 아닐 수 있다), prev_nodes[i]는 points[i]로 들어오기 직전 노드 ID(그래프 엣지를 타지 않은 연결 구간이면
    None)이다. Robot은 이 둘로 (prev_nodes[i], nodes[i]) 간 엣지를 찾아 통로/교차점 제약을 적용한다.
    """

    points: list[Point]
    length: float  # px
    floors: list[int] = field(default_factory=list)
    lifts: list[str | None] = field(default_factory=list)
    nodes: list[int] = field(default_factory=list)
    prev_nodes: list[int | None] = field(default_factory=list)


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
    """층별 경로망 그래프. 층 사이는 엘리베이터 노드를 잇는 수직 엣지로만 이동한다."""

    def __init__(self, factory: FactoryConfig, robot_width: float | None = None) -> None:
        """구성으로부터 그래프를 만든다.

        Args:
            factory: 공장 구성
        """
        self.factory = factory
        self.robot_width = robot_width
        self._width_graphs: dict[float, PathGraph] = {}
        self._entry_cache: dict[tuple[float, float, int, int], int | None] = {}
        self.nodes: dict[int, Point] = {n.id: (n.x, n.y) for n in factory.nodes}
        self.node_floor: dict[int, int] = {n.id: n.floor for n in factory.nodes}
        self.floor_nodes: dict[int, list[int]] = {}
        for node_id, floor in self.node_floor.items():
            self.floor_nodes.setdefault(floor, []).append(node_id)
        self.adjacency: dict[int, list[tuple[int, float]]] = {n: [] for n in self.nodes}
        self.edges: list[tuple[int, int, bool]] = []  # (a, b, blocked) — 같은 층 안의 엣지만
        self.lift_edges: dict[tuple[int, int], str] = {}  # (노드, 노드) → 엘리베이터 ID (양방향)
        self.implicit_elevators: list[ElevatorConfig] = []  # ElevatorConfig 없이 노드 그룹으로만 정의된 엘리베이터
        self.facility_nodes: dict[str, int] = {}  # 설비 ID → 접근 노드 (노드 ID는 저장 전 음수 임시 ID일 수 있음)
        self._dist_cache: dict[int, tuple[dict[int, float], dict[int, int]]] = {}
        self.obstacles = list(factory.obstacles)
        self.floor_obstacles: dict[int, list[ObstacleConfig]] = {}
        for obstacle in factory.obstacles:
            self.floor_obstacles.setdefault(obstacle.floor, []).append(obstacle)

        for edge in factory.edges:
            if edge.from_node not in self.nodes or edge.to_node not in self.nodes:
                continue
            if self.node_floor[edge.from_node] != self.node_floor[edge.to_node]:
                self._register_node_group_lift(edge, factory)
                continue
            a, b = self.nodes[edge.from_node], self.nodes[edge.to_node]
            blocked = edge_blocked(a, b, self.floor_obstacles.get(self.node_floor[edge.from_node], []))
            blocked |= robot_width is not None and edge.width - factory.profile.edge_clearance < robot_width - 1e-9
            self.edges.append((edge.from_node, edge.to_node, blocked))
            if blocked:
                continue
            length = math.hypot(a[0] - b[0], a[1] - b[1])
            self.adjacency[edge.from_node].append((edge.to_node, length))
            if edge.is_bidirectional:
                self.adjacency[edge.to_node].append((edge.from_node, length))

        self._link_elevators(factory)

        linked = {n.linked_station_id: n.id for n in factory.nodes
                  if n.linked_station_id and n.node_type != "elevator"}
        facilities = [(s.id, s.x, s.y, s.floor) for s in factory.stations] + \
            [(c.id, c.x, c.y, c.floor) for c in factory.chargers]
        for facility_id, fx, fy, floor in facilities:
            node = linked.get(facility_id)
            nearest = node if node is not None else self.nearest_node(fx, fy, floor)
            if nearest is not None:
                self.facility_nodes[facility_id] = nearest

    def _register_node_group_lift(self, edge: PathEdgeConfig, factory: FactoryConfig) -> None:
        """서로 다른 층의 같은 엘리베이터 그룹 노드를 잇는 DB 엣지를 엘리베이터 구간으로 등록한다 (이전 방식 호환).

        ElevatorConfig가 있는 그룹은 _link_elevators가 처리하므로 여기서는 없는 그룹만 다룬다.
        """
        a = next((n for n in factory.nodes if n.id == edge.from_node), None)
        b = next((n for n in factory.nodes if n.id == edge.to_node), None)
        if a is None or b is None or a.node_type != "elevator" or b.node_type != "elevator":
            return
        group = a.linked_station_id
        if not group or group != b.linked_station_id or any(e.id == group for e in factory.elevators):
            return
        profile = factory.profile
        floors = abs(a.floor - b.floor)
        cost = (floors * profile.floor_travel_sec + profile.door_time_sec) * config.ELEVATOR_PX_PER_SEC
        self.adjacency[a.id].append((b.id, cost))
        self.adjacency[b.id].append((a.id, cost))
        self.lift_edges[(a.id, b.id)] = group
        self.lift_edges[(b.id, a.id)] = group
        if all(e.id != group for e in self.implicit_elevators):
            capacity = a.capacity or b.capacity or config.DEFAULT_ELEVATOR_CAPACITY
            self.implicit_elevators.append(ElevatorConfig(group, group, a.x, a.y, travel_time=profile.floor_travel_sec,
                                                          capacity=int(capacity)))

    def _link_elevators(self, factory: FactoryConfig) -> None:
        """엘리베이터 접근 노드를 인접한 층끼리 수직 엣지로 잇는다 (가동 중인 엘리베이터만)."""
        for elevator in factory.elevators:
            if not elevator.is_active:
                continue
            by_floor = {n.floor: n.id for n in factory.nodes
                        if n.node_type == "elevator" and n.linked_station_id == elevator.id}
            floors = sorted(by_floor)
            hop_cost = (max(0.0, float(elevator.travel_time)) + factory.profile.door_time_sec) * config.ELEVATOR_PX_PER_SEC
            for lower, upper in pairwise(floors):
                if upper - lower != 1:
                    continue
                a, b = by_floor[lower], by_floor[upper]
                self.adjacency[a].append((b, hop_cost))
                self.adjacency[b].append((a, hop_cost))
                self.lift_edges[(a, b)] = elevator.id
                self.lift_edges[(b, a)] = elevator.id

    def nearest_node(self, x: float, y: float, floor: int | None = None) -> int | None:
        """좌표에서 가장 가까운 노드 (그래프가 비면 None).

        Args:
            x: X 좌표
            y: Y 좌표
            floor: 이 층의 노드만 찾는다 (None이면 전체)

        Returns:
            노드 ID 또는 None
        """
        pool = list(self.nodes) if floor is None else self.floor_nodes.get(floor, [])
        if not pool:
            return None
        return min(pool, key=lambda n: math.hypot(self.nodes[n][0] - x, self.nodes[n][1] - y))

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

    def route_to_facility(self, x: float, y: float, facility_id: str, floor: int = 1) -> Route | None:
        """현재 위치에서 설비 접근 노드까지 경로. 다른 층이면 엘리베이터를 경유한다.

        연속된 같은 엘리베이터 구간은 한 번의 탑승(마지막 층 도착점)으로 합쳐진다.

        Args:
            x: 현재 X
            y: 현재 Y
            facility_id: 스테이션/충전소 ID
            floor: 현재 층

        Returns:
            Route 또는 도달 불가 시 None
        """
        target = self.facility_nodes.get(facility_id)
        if target not in self.nodes:
            return None
        start = self._accessible_start(x, y, floor, target)
        if start is None:
            return None
        result = self.shortest_path(start, target)
        if result is None:
            return None
        node_path, length = result
        points: list[Point] = []
        floors: list[int] = []
        lifts: list[str | None] = []
        nodes_out: list[int] = []
        prev_nodes: list[int | None] = []
        for prev_node, node in zip([None, *node_path[:-1]], node_path):
            lift = self.lift_edges.get((prev_node, node)) if prev_node is not None else None
            if lift is not None and lifts and lifts[-1] == lift:
                points[-1], floors[-1], nodes_out[-1] = self.nodes[node], self.node_floor[node], node
                continue
            points.append(self.nodes[node])
            floors.append(self.node_floor[node])
            lifts.append(lift)
            nodes_out.append(node)
            prev_nodes.append(prev_node)
        start_gap = math.hypot(points[0][0] - x, points[0][1] - y)
        if start_gap < 1e-6 and lifts[0] is None:
            # 로봇이 이미 그래프 노드 위에 있다 — 그 노드는 이동할 점이 아니라 출발점이다.
            points, floors, lifts = points[1:], floors[1:], lifts[1:]
            nodes_out, prev_nodes = nodes_out[1:], prev_nodes[1:]
        return Route(points=points, length=length + start_gap, floors=floors, lifts=lifts, nodes=nodes_out,
                     prev_nodes=prev_nodes)

    def distance_to_facility(self, x: float, y: float, facility_id: str, floor: int = 1) -> float:
        """현재 위치에서 설비까지의 경로 거리 (도달 불가 시 inf). 엘리베이터 이동은 환산 거리로 더해진다.

        Args:
            x: 현재 X
            y: 현재 Y
            facility_id: 스테이션/충전소 ID
            floor: 현재 층

        Returns:
            거리 (px)
        """
        target = self.facility_nodes.get(facility_id)
        if target not in self.nodes:
            return math.inf
        start = self._accessible_start(x, y, floor, target)
        if start is None:
            return math.inf
        dist, _ = self._dijkstra(start)
        if target not in dist:
            return math.inf
        sx, sy = self.nodes[start]
        return dist[target] + math.hypot(sx - x, sy - y)

    def _accessible_start(self, x: float, y: float, floor: int, target: int) -> int | None:
        """같은 층에서 장애물 없이 연결할 수 있고 목적지에 도달 가능한 최근접 노드를 반환한다."""
        key = (x, y, floor, target)
        if key not in self._entry_cache:
            if len(self._entry_cache) >= 1024:
                self._entry_cache.pop(next(iter(self._entry_cache)))
            self._entry_cache[key] = self._find_accessible_start(x, y, floor, target)
        return self._entry_cache[key]

    def _find_accessible_start(self, x: float, y: float, floor: int, target: int) -> int | None:
        """Scan one immutable width-filtered layout; hot reload creates a new cache."""
        pool = self.floor_nodes.get(floor, [])
        obstacles = self.floor_obstacles.get(floor, [])
        candidates = sorted(pool, key=lambda n: math.hypot(self.nodes[n][0] - x, self.nodes[n][1] - y))
        if self.robot_width is not None and candidates:
            closest = candidates[0]
            if math.dist((x, y), self.nodes[closest]) < 1e-6:
                return closest if self.shortest_path(closest, target) is not None else None
            # Off-graph spawn connectors may enter the nearest visible graph point,
            # but may not jump across an inaccessible narrow edge to a distant node.
            candidates = [n for n in candidates if not edge_blocked((x, y), self.nodes[n], obstacles)][:1]
        for node in candidates:
            if edge_blocked((x, y), self.nodes[node], obstacles):
                continue
            distances, _ = self._dijkstra(node)
            if target in distances:
                return node
        return None

    def for_robot(self, width: float) -> PathGraph:
        """Return a cached width-filtered graph for this immutable layout."""
        if width not in self._width_graphs:
            self._width_graphs[width] = PathGraph(self.factory, width)
        return self._width_graphs[width]

    def physical_distance_to_facility(self, x: float, y: float, facility_id: str, floor: int = 1) -> float:
        """Return horizontal metres, excluding the elevator's Dijkstra time cost."""
        route = self.route_to_facility(x, y, facility_id, floor)
        if route is None:
            return math.inf
        distance = 0.0
        previous, previous_floor = (x, y), floor
        for point, next_floor, lift in zip(route.points, route.floors, route.lifts, strict=True):
            if lift is None and previous_floor == next_floor:
                distance += math.dist(previous, point) * config.METERS_PER_PIXEL
            previous, previous_floor = point, next_floor
        return distance

    def facility_point(self, facility_id: str) -> Point | None:
        """설비 접근 노드 좌표.

        Args:
            facility_id: 설비 ID

        Returns:
            좌표 또는 None
        """
        node = self.facility_nodes.get(facility_id)
        return self.nodes.get(node) if node is not None else None

    def facility_floor(self, facility_id: str) -> int | None:
        """설비 접근 노드가 있는 층.

        Args:
            facility_id: 설비 ID

        Returns:
            층 번호 또는 None
        """
        node = self.facility_nodes.get(facility_id)
        return self.node_floor.get(node) if node is not None else None


if __name__ == "__main__":
    from database.seed_data import build_default_config

    default = build_default_config()
    graph = PathGraph(default)
    blocked = sum(1 for e in graph.edges if e[2])
    print(f"nodes={len(graph.nodes)} edges={len(graph.edges)} blocked={blocked}")
    print("WS-01 → WS-05:", graph.route_to_facility(100, 150, "WS-05", 1))
