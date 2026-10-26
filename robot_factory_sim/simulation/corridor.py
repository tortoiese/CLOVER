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
from utils.config_models import FactoryConfig, FactoryProfile, PathEdgeConfig
from simulation.traffic_rules import LaneResource, TrafficBlocked, can_pass
from simulation.state import TrafficSnapshot
from simulation.path_graph import edge_blocked

EdgeKind = str  # "corridor" | "elevator"


def lane_capacity(edge_width: float, profile: FactoryProfile) -> int:
    """통로 폭으로 차선 수(기준폭 로봇이 동시에 지나갈 수 있는 대수)를 계산한다.

    Args:
        edge_width: 통로 폭 (m)
        profile: 공장 프로필 (clearance 값)

    Returns:
        차선 수 (유효 폭이 없으면 0). 실제 로봇 통과 여부는 별도로 검사한다.
    """
    usable = edge_width - profile.edge_clearance
    per_lane = config.DEFAULT_ROBOT_WIDTH + profile.side_clearance
    return max(1, math.floor((usable + 1e-9) / per_lane)) if usable > 0 else 0


def lanes_consumed(robot_width: float, profile: FactoryProfile) -> int:
    """로봇이 통로에서 점유하는 차선 수 (기준폭보다 넓은 로봇은 더 점유한다).

    Args:
        robot_width: 로봇 폭 (m)
        profile: 공장 프로필

    Returns:
        점유 차선 수 (최소 1)
    """
    per_lane = config.DEFAULT_ROBOT_WIDTH + profile.side_clearance
    return max(1, math.ceil((robot_width + profile.side_clearance - 1e-9) / per_lane))


class CorridorNetwork:
    """통로/교차점/엘리베이터 공유자원 모음."""

    def __init__(self, env: simpy.Environment, factory: FactoryConfig) -> None:
        """공장 구성으로부터 공유자원을 만든다.

        Args:
            env: SimPy 환경
            factory: 공장 구성 (노드/엣지)
        """
        self.env = env
        self.profile = factory.profile
        self._edges = {edge.id: edge for edge in factory.edges}
        self._nodes = {node.id: node for node in factory.nodes}
        self._robot_widths = [robot.width for robot in factory.robots if robot.is_active]
        self._blocked_edges = set()
        for edge in factory.edges:
            start, end = self._nodes.get(edge.from_node), self._nodes.get(edge.to_node)
            if start and end and start.floor == end.floor and edge_blocked(
                    (start.x, start.y), (end.x, end.y), [o for o in factory.obstacles if o.floor == start.floor]):
                self._blocked_edges.add(edge.id)
        self._node_type: dict[int, str] = {n.id: n.node_type for n in factory.nodes}
        self._node_floor: dict[int, int] = {n.id: n.floor for n in factory.nodes}
        self._node_group: dict[int, str | None] = {n.id: n.linked_station_id for n in factory.nodes}
        self._node_capacity: dict[int, int | None] = {n.id: n.capacity for n in factory.nodes}
        self._neighbors: dict[int, set[int]] = {node.id: set() for node in factory.nodes}
        for edge in factory.edges:
            if edge.from_node in self._neighbors and edge.to_node in self._neighbors:
                self._neighbors[edge.from_node].add(edge.to_node)
                self._neighbors[edge.to_node].add(edge.from_node)

        self._by_pair: dict[tuple[int, int], int] = {}
        self._edge_kind: dict[int, EdgeKind] = {}
        self._edge_capacity: dict[int, int] = {}
        self._edge_floor_change: dict[int, int] = {}
        self._edge_group: dict[int, str] = {}
        self._corridor_resource: dict[int, LaneResource] = {}
        self._elevator_resource: dict[str, simpy.Resource] = {}

        for edge in factory.edges:
            self._register_edge(edge)

        self._intersection_resource: dict[int, LaneResource] = {
            node_id: LaneResource(env, capacity=1)
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
        if capacity:
            self._corridor_resource[edge.id] = LaneResource(self.env, capacity=capacity)

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

    def edge_resource(self, edge_id: int) -> LaneResource | None:
        """통로 차선 자원.

        Args:
            edge_id: 엣지 ID

        Returns:
            Resource 또는 None (엘리베이터 엣지거나 미등록)
        """
        return self._corridor_resource.get(edge_id)

    def lanes_needed(self, edge_id: int, robot_width: float) -> int:
        """필요 차선 수를 반환한다. 초과 로봇은 clamp해서 통과시키지 않는다.

        Args:
            edge_id: 엣지 ID
            robot_width: 로봇 폭 (m)

        Returns:
            점유 차선 수
        """
        capacity = self._edge_capacity.get(edge_id, 1)
        need = lanes_consumed(robot_width, self.profile)
        edge = self._edges[edge_id]
        if not capacity or not can_pass(edge.width, robot_width, self.profile):
            raise TrafficBlocked(f"통로 {edge_id}: 로봇 폭 {robot_width:g}m 통행 불가")
        return min(need, capacity)

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

    def intersection_resource(self, node_id: int) -> LaneResource:
        """교차점 공유자원(capacity=1).

        Args:
            node_id: 노드 ID

        Returns:
            Resource
        """
        return self._intersection_resource[node_id]

    def update_robot_widths(self, widths: list[float]) -> None:
        """통로 자원은 유지하면서 표시용 활성 로봇 폭 목록을 갱신한다.

        Args:
            widths: 활성 로봇 폭(m) 목록.
        """
        self._robot_widths = list(widths)

    def continues_controlled_section(self, node_id: int, incoming: int | None, outgoing: int | None) -> bool:
        """교차로와 분기 없는 좁은 통로는 빠져나갈 때까지 한 구간으로 확보한다.

        Args:
            node_id: 현재 경유 노드.
            incoming: 진입 엣지 ID.
            outgoing: 진출 엣지 ID.

        Returns:
            다음 엣지까지 함께 확보할지 여부.
        """
        if outgoing not in self._corridor_resource:
            return False
        if self.is_intersection(node_id):
            return True
        if self._node_type.get(node_id) != "waypoint" or len(self._neighbors.get(node_id, [])) != 2:
            return False

        def single_file(edge_id: int | None) -> bool:
            capacity = self._edge_capacity.get(edge_id, 0)
            edge = self._edges.get(edge_id)
            if not edge or not capacity:
                return False
            needed = [min(lanes_consumed(width, self.profile), capacity) for width in self._robot_widths
                      if can_pass(edge.width, width, self.profile)]
            return bool(needed) and max(needed) == capacity

        return single_file(incoming) and single_file(outgoing)

    def snapshots(self) -> list[TrafficSnapshot]:
        """통로·교차로 점유와 실제 요청 대기열의 스냅샷을 만든다.

        Returns:
            스레드 간 전달 가능한 통행 상태 목록.
        """
        snapshots = []
        for edge_id, capacity in self._edge_capacity.items():
            edge = self._edges[edge_id]
            start, end = self._nodes[edge.from_node], self._nodes[edge.to_node]
            resource = self._corridor_resource.get(edge_id)
            passable = (edge_id not in self._blocked_edges
                        and any(can_pass(edge.width, width, self.profile) for width in self._robot_widths))
            snapshots.append(self._snapshot(f"edge:{edge_id}", "corridor", start.floor,
                                            start.x, start.y, end.x, end.y, capacity, resource,
                                            edge.width, edge.is_bidirectional, passable))
        for node_id, resource in self._intersection_resource.items():
            node = self._nodes[node_id]
            snapshots.append(self._snapshot(f"node:{node_id}", "intersection", node.floor,
                                            node.x, node.y, node.x, node.y, 1, resource))
        return snapshots

    def _snapshot(self, identifier: str, kind: str, floor: int, x: float, y: float, end_x: float, end_y: float,
                  capacity: int, resource: LaneResource | None, width: float = 0,
                  bidirectional: bool = True, passable: bool = True) -> TrafficSnapshot:
        users = resource.users if resource else []
        queue = resource.queue if resource else []
        return TrafficSnapshot(identifier, kind, floor, x, y, end_x, end_y, capacity,
                               resource.count if resource else 0, len(queue),
                               [request.robot_id for request in users], [request.robot_id for request in queue],
                               max((float(self.env.now) - request.created_at for request in queue), default=0.0),
                               width, bidirectional, passable)


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
