"""CorridorNetwork — 통로 차선 / 교차점 / 엘리베이터 공유자원.

PathGraph는 순수 그래프(최단경로 계산)만 담당하고, 이 모듈이 SimPy 공유자원(통로 차선
수용량, 교차점 상호배제, 엘리베이터)을 맡는다. 레이아웃(노드/엣지)이 바뀌면 엔진이
PathGraph와 함께 이 객체도 새로 만든다.

- 통로(엣지): 폭 ÷ 기준 로봇폭 → 차선 수(capacity). 큰 로봇은 차선을 더 점유한다.
  차선이 1개뿐인 통로는 Resource(capacity=1)이 되어 추월/반대편 동시진입이 자연히 막힌다.
- 교차점(node_type="intersection"): capacity=1 공유자원. 한 번에 로봇 1대만 통과한다.
- 엘리베이터(node_type="elevator"): 같은 linked_station_id(샤프트 그룹)를 가진 두 노드의
  floor가 다를 때만 그 사이 엣지가 엘리베이터 구간이 된다. 그룹 전체가 하나의 공유자원을 쓴다.
"""

from __future__ import annotations

import math

import simpy

import config
from simulation.resource_pool import CapacityPool
from utils.config_models import FactoryConfig, FactoryProfile, PathEdgeConfig

EdgeKind = str  # "corridor" | "elevator"


def lane_capacity(edge_width: float, profile: FactoryProfile) -> int:
    """통로 폭으로 차선 수(기준폭 로봇이 동시에 지나갈 수 있는 대수)를 계산한다.

    Args:
        edge_width: 통로 폭 (m)
        profile: 공장 프로필 (clearance 값)

    Returns:
        차선 수 (최소 1)
    """
    usable = edge_width - profile.edge_clearance
    per_lane = config.DEFAULT_ROBOT_WIDTH + profile.side_clearance
    return max(0, int((usable + profile.side_clearance + 1e-9) // per_lane))


def lanes_consumed(robot_width: float, profile: FactoryProfile) -> int:
    """로봇이 통로에서 점유하는 차선 수 (기준폭보다 넓은 로봇은 더 점유한다).

    Args:
        robot_width: 로봇 폭 (m)
        profile: 공장 프로필

    Returns:
        점유 차선 수 (최소 1)
    """
    per_lane = config.DEFAULT_ROBOT_WIDTH + profile.side_clearance
    return max(1, math.ceil((robot_width + profile.side_clearance) / per_lane))


class CorridorNetwork:
    """통로/교차점/엘리베이터 공유자원 모음."""

    def __init__(self, env: simpy.Environment, factory: FactoryConfig) -> None:
        """공장 구성으로부터 공유자원을 만든다.

        Args:
            env: SimPy 환경
            factory: 공장 구성 (노드/엣지)
        """
        self.env = env
        self._factory = factory
        self.profile = factory.profile
        self._node_type: dict[int, str] = {n.id: n.node_type for n in factory.nodes}
        self._node_floor: dict[int, int] = {n.id: n.floor for n in factory.nodes}
        self._node_group: dict[int, str | None] = {n.id: n.linked_station_id for n in factory.nodes}
        self._node_capacity: dict[int, int | None] = {n.id: n.capacity for n in factory.nodes}

        self._by_pair: dict[tuple[int, int], int] = {}
        self._edge_kind: dict[int, EdgeKind] = {}
        self._edge_capacity: dict[int, int] = {}
        self._edge_floor_change: dict[int, int] = {}
        self._edge_group: dict[int, str] = {}
        self._corridor_resource: dict[int, CapacityPool] = {}
        self._physical: dict[tuple, CapacityPool] = {}
        self._usable: dict[int, float] = {}
        self._elevator_resource: dict[str, simpy.Resource] = {}

        for edge in factory.edges:
            self._register_edge(edge)

        self._intersection_resource: dict[int, simpy.Resource] = {
            node_id: simpy.Resource(env, capacity=1)
            for node_id, node_type in self._node_type.items() if node_type == "intersection"
        }

    def _register_edge(self, edge: PathEdgeConfig) -> None:
        """엣지 1개를 통로/엘리베이터로 분류하고 필요한 자원을 만든다."""
        if edge.from_node not in self._node_type or edge.to_node not in self._node_type:
            return
        self._by_pair[(edge.from_node, edge.to_node)] = edge.id
        if edge.is_bidirectional:
            self._by_pair[(edge.to_node, edge.from_node)] = edge.id

        a_type, b_type = self._node_type[edge.from_node], self._node_type[edge.to_node]
        a_floor, b_floor = self._node_floor[edge.from_node], self._node_floor[edge.to_node]
        a_group, b_group = self._node_group[edge.from_node], self._node_group[edge.to_node]
        is_elevator_edge = (
            a_type == "elevator" and b_type == "elevator" and a_group and a_group == b_group
            and a_floor != b_floor
        )
        if is_elevator_edge:
            assert a_group is not None
            self._edge_kind[edge.id] = "elevator"
            self._edge_floor_change[edge.id] = abs(a_floor - b_floor)
            self._edge_group[edge.id] = a_group
            if a_group not in self._elevator_resource:
                self._elevator_resource[a_group] = simpy.Resource(self.env, capacity=self._elevator_capacity(a_group))
            return
        capacity = lane_capacity(edge.width, self.profile)
        self._edge_kind[edge.id] = "corridor"
        self._edge_capacity[edge.id] = capacity
        nodes = {n.id: (n.floor, n.x, n.y) for n in self._factory.nodes}
        key = tuple(sorted((nodes[edge.from_node], nodes[edge.to_node])))
        usable = max(0.0, edge.width - self.profile.edge_clearance)
        self._usable[edge.id] = usable
        units = math.floor((usable + self.profile.side_clearance) * 1000 + 1e-6)
        resource = self._physical.setdefault(key, CapacityPool(self.env, units))
        resource.resize(min(units, resource.capacity))
        self._corridor_resource[edge.id] = resource

    def update(self, factory: FactoryConfig) -> None:
        """Preserve occupied physical resources across metadata and ID changes."""
        fresh = CorridorNetwork(self.env, factory)
        for key, candidate in list(fresh._physical.items()):
            old = self._physical.get(key)
            if old is not None:
                old.resize(candidate.capacity)
                fresh._physical[key] = old
                for edge_id, resource in list(fresh._corridor_resource.items()):
                    if resource is candidate:
                        fresh._corridor_resource[edge_id] = old
        old_points = {n.id: (n.floor, n.x, n.y) for n in self._factory.nodes}
        intersections = {old_points[i]: r for i, r in self._intersection_resource.items()}
        for node in factory.nodes:
            key = (node.floor, node.x, node.y)
            if node.id in fresh._intersection_resource and key in intersections:
                fresh._intersection_resource[node.id] = intersections[key]
        # Keep removed resources reachable until their owners have drained.
        for key, resource in self._physical.items():
            if key not in fresh._physical:
                resource.resize(0)
                fresh._physical[key] = resource
        self.__dict__.update(fresh.__dict__)

    def can_pass(self, edge_id: int, robot_width: float) -> bool:
        """Check physical clearance without clamping an oversized robot."""
        return robot_width <= self._usable.get(edge_id, 0.0) + 1e-9

    def width_units(self, robot_width: float) -> int:
        """Convert width plus inter-robot separation to conservative millimetres."""
        return max(1, math.ceil((robot_width + self.profile.side_clearance) * 1000 - 1e-6))

    def resource_id(self, edge_id: int) -> str:
        """Identify a physical footprint independently of database edge IDs."""
        resource = self._corridor_resource[edge_id]
        key = next(key for key, value in self._physical.items() if value is resource)
        return "corridor:" + repr(key)

    def intersection_id(self, node_id: int) -> str:
        """Identify an intersection by floor and position."""
        node = next(n for n in self._factory.nodes if n.id == node_id)
        return "intersection:" + repr((node.floor, node.x, node.y))

    def _elevator_capacity(self, group: str) -> int:
        """같은 엘리베이터 그룹의 노드 중 지정된 capacity가 있으면 그 값을, 없으면 fallback을 쓴다."""
        for node_id, node_group in self._node_group.items():
            if node_group == group and self._node_type.get(node_id) == "elevator":
                node_capacity = self._node_capacity.get(node_id)
                if node_capacity:
                    return max(1, int(node_capacity))
        return config.DEFAULT_ELEVATOR_CAPACITY

    # ------------------------------------------------------------------
    # 조회 (Robot이 이동 중 사용)
    # ------------------------------------------------------------------
    def edge_between(self, a: int | None, b: int) -> int | None:
        """두 노드를 잇는 엣지 ID.

        Args:
            a: 출발 노드 (그래프 밖 연결 구간이면 None)
            b: 도착 노드

        Returns:
            엣지 ID 또는 None
        """
        if a is None:
            return None
        return self._by_pair.get((a, b))

    def edge_kind(self, edge_id: int) -> EdgeKind:
        """엣지 종류.

        Args:
            edge_id: 엣지 ID

        Returns:
            "corridor" | "elevator"
        """
        return self._edge_kind.get(edge_id, "corridor")

    def edge_resource(self, edge_id: int) -> simpy.Resource | None:
        """통로 차선 자원.

        Args:
            edge_id: 엣지 ID

        Returns:
            Resource 또는 None (엘리베이터 엣지거나 미등록)
        """
        return self._corridor_resource.get(edge_id)

    def lanes_needed(self, edge_id: int, robot_width: float) -> int:
        """이 로봇이 이 통로에서 점유해야 할 차선 수 (통로 전체 차선 수로 clamp).

        Args:
            edge_id: 엣지 ID
            robot_width: 로봇 폭 (m)

        Returns:
            점유 차선 수
        """
        return lanes_consumed(robot_width, self.profile)

    def elevator_resource(self, edge_id: int) -> simpy.Resource | None:
        """이 엘리베이터 엣지가 속한 샤프트(그룹)의 공유자원.

        Args:
            edge_id: 엣지 ID

        Returns:
            Resource 또는 None (엘리베이터 엣지가 아님)
        """
        group = self._edge_group.get(edge_id)
        return self._elevator_resource.get(group) if group else None

    def floor_change(self, edge_id: int) -> int:
        """엘리베이터 엣지의 층수 차이 (일반 통로면 0).

        Args:
            edge_id: 엣지 ID

        Returns:
            층수 차이
        """
        return self._edge_floor_change.get(edge_id, 0)

    def is_intersection(self, node_id: int | None) -> bool:
        """노드가 교차점인지 여부.

        Args:
            node_id: 노드 ID

        Returns:
            교차점 여부
        """
        return node_id is not None and self._node_type.get(node_id) == "intersection"

    def intersection_resource(self, node_id: int) -> simpy.Resource:
        """교차점 공유자원(capacity=1).

        Args:
            node_id: 노드 ID

        Returns:
            Resource
        """
        return self._intersection_resource[node_id]


if __name__ == "__main__":
    from utils.config_models import PathNodeConfig

    env = simpy.Environment()
    nodes = [
        PathNodeConfig(1, 0, 0, "station", "WS-01"),
        PathNodeConfig(2, 100, 0),
        PathNodeConfig(3, 200, 0, "station", "WS-02"),
    ]
    for width in (5.0, 1.2):
        edges = [PathEdgeConfig(1, 1, 2, width=width), PathEdgeConfig(2, 2, 3, width=width)]
        net = CorridorNetwork(env, FactoryConfig(FactoryProfile(), [], [], [], [], nodes, edges))
        print(f"width={width}m lanes={net._edge_capacity[1]}, "
              f"소형(0.6m) 점유={net.lanes_needed(1, 0.6)}, 팔레트(1.1m) 점유={net.lanes_needed(1, 1.1)}")
