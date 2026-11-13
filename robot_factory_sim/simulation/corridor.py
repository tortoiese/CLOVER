"""통로는 정수 mm, 교차점은 slot으로 계측하고 통제 구간 전체를 원자적으로 확보한다.

물리 좌표로 자원을 식별하여 재설정 중에도 기존 점유를 보존한다. 엘리베이터는 엔진의
실제 캐빈 자원을 참조하며 별도의 가상 캐빈을 만들지 않는다. 계약은 TRAFFIC_RULES.md 참고.
"""

from __future__ import annotations

import math
from dataclasses import replace

import simpy

import config
from simulation.path_graph import Route, edge_blocked
from simulation.resource_pool import SLOT_UNIT, WIDTH_UNIT, TrafficArbiter
from simulation.state import TrafficSnapshot
from simulation.traffic_rules import (
    LaneResource,
    TrafficBlocked,
    TrafficDemand,
    TrafficReservation,
    can_pass,
    capacity_mm,
    corridor_identity,
    demand_mm,
    physical_point,
    tail_clearance_seconds,
)
from utils.config_models import FactoryConfig, FactoryProfile, PathEdgeConfig

EdgeKind = str  # "corridor" | "elevator"


def lane_capacity(edge_width: float, profile: FactoryProfile) -> int:
    """표시 전용: 기준 폭 로봇이 들어갈 대수. 자원 확보에는 mm를 사용한다."""
    return capacity_mm(edge_width, profile) // demand_mm(config.DEFAULT_ROBOT_WIDTH, profile)


class CorridorNetwork:
    """통로/교차점/엘리베이터 공유자원 모음."""

    def __init__(self, env: simpy.Environment, factory: FactoryConfig, *,
                 pools: dict[str, LaneResource] | None = None, arbiter: TrafficArbiter | None = None,
                 templates: dict[str, TrafficSnapshot] | None = None,
                 elevator_resources: dict[str, simpy.Resource] | None = None) -> None:
        """공장 구성으로부터 공유자원을 만든다.

        Args:
            env: SimPy 환경
            factory: 공장 구성 (노드/엣지)
        """
        self.env = env
        self._factory = factory
        self._paused = False
        self.arbiter = arbiter or TrafficArbiter(env)
        self._pools = {} if pools is None else pools
        self._templates = {} if templates is None else templates
        self._limits: dict[LaneResource, int] = {}
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
        self._elevator_resource = {} if elevator_resources is None else elevator_resources

        for edge in factory.edges:
            self._register_edge(edge)

        self._intersection_resource: dict[int, LaneResource] = {}
        for node in factory.nodes:
            if node.node_type != "intersection":
                continue
            key = "intersection:" + repr(physical_point(node.floor, node.x, node.y))
            resource = self._pool(key, 1, SLOT_UNIT)
            self._intersection_resource[node.id] = resource
            self._templates[key] = TrafficSnapshot(f"node:{node.id}", "intersection", node.floor,
                                                   node.x, node.y, node.x, node.y, 1, 0, 0,
                                                   resource_id=key)
        self.arbiter.resize({pool: self._limits.get(pool, 0) for pool in self._pools.values()})

    def _pool(self, key: str, capacity: int, unit: str) -> LaneResource:
        resource = self._pools.get(key)
        if resource is None:
            resource = LaneResource(self.env, capacity, resource_id=key, unit=unit, arbiter=self.arbiter)
            self._pools[key] = resource
        if resource.unit != unit:
            raise ValueError("a physical resource cannot change its capacity unit")
        self._limits[resource] = min(capacity, self._limits.get(resource, capacity))
        return resource

    def reconfigure(self, factory: FactoryConfig) -> None:
        """Preserve occupied footprints and drain removals before admitting new motion."""
        fresh = CorridorNetwork(self.env, factory, pools=self._pools, arbiter=self.arbiter,
                                templates=self._templates, elevator_resources=self._elevator_resource)
        self.__dict__.update(fresh.__dict__)

    def pause_admission(self) -> None:
        """Close new entries while existing motion drains to a topology boundary."""
        self._paused = True
        self.arbiter.resize({pool: 0 for pool in self._pools.values()})

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
            return
        capacity = 0 if edge.id in self._blocked_edges else capacity_mm(edge.width, self.profile)
        self._edge_kind[edge.id] = "corridor"
        self._edge_capacity[edge.id] = capacity
        key = corridor_identity(self._point_key(edge.from_node), self._point_key(edge.to_node))
        resource = self._pool(key, capacity, WIDTH_UNIT)
        self._corridor_resource[edge.id] = resource
        start, end = self._nodes[edge.from_node], self._nodes[edge.to_node]
        self._templates[key] = TrafficSnapshot(f"edge:{edge.id}", "corridor", start.floor,
                                               start.x, start.y, end.x, end.y, capacity, 0, 0,
                                               width=edge.width, bidirectional=edge.is_bidirectional,
                                               unit=WIDTH_UNIT, resource_id=key)

    def _point_key(self, node_id: int) -> tuple[int, float, float]:
        node = self._nodes[node_id]
        return physical_point(node.floor, node.x, node.y)

    def resource_id(self, edge_id: int) -> str:
        """Return a physical footprint identity independent of database edge numbering."""
        return self._corridor_resource[edge_id].resource_id

    def node_resource_id(self, node_id: int) -> str:
        """Return the stable physical identity of a crossing slot."""
        return self._intersection_resource[node_id].resource_id

    def width_units(self, robot_width: float) -> int:
        """Return the common conservative mm demand used by runtime and planners."""
        return demand_mm(robot_width, self.profile)

    def can_pass(self, edge_id: int, robot_width: float) -> bool:
        """Check demand against the actual shared footprint capacity."""
        resource = self._corridor_resource.get(edge_id)
        return bool(resource and self.width_units(robot_width) <= resource.capacity)

    def section_demands(self, route: Route, start: int, end: int, robot_width: float) -> tuple[TrafficDemand, ...]:
        """Describe the full entry-to-tail-clearance hold without any SimPy/Qt objects."""
        demands = {}
        for index in range(start, end + 1):
            node, previous = route.nodes[index], route.prev_nodes[index]
            edge = self.edge_between(previous, node)
            if previous is not None and edge is None:
                raise TrafficBlocked("통행 방향에 맞는 경로 엣지가 없습니다.")
            if edge is not None:
                if edge not in self._corridor_resource:
                    raise TrafficBlocked("사용 가능한 통로 자원이 없습니다.")
                amount = self.demand_units(edge, robot_width)
                key = self.resource_id(edge)
                demands[key] = TrafficDemand(key, amount, WIDTH_UNIT)
            if self.is_intersection(node):
                key = self.node_resource_id(node)
                demands[key] = TrafficDemand(key, 1, SLOT_UNIT)
        return tuple(demands.values())

    def section_end(self, route: Route, start: int) -> int:
        """Use one controlled-section grouping for physical execution and planning."""
        end = start
        if len(route.nodes) != len(route.points) or len(route.prev_nodes) != len(route.points):
            return end
        while end + 1 < len(route.points) and not (route.lifts and route.lifts[end + 1]):
            incoming = self.edge_between(route.prev_nodes[end], route.nodes[end])
            outgoing = self.edge_between(route.prev_nodes[end + 1], route.nodes[end + 1])
            if not self.continues_controlled_section(route.nodes[end], incoming, outgoing):
                break
            end += 1
        return end

    def planned_reservations(self, route: Route, robot_width: float, length: float, speed: float,
                             starts: tuple[float, ...], ends: tuple[float, ...]) -> tuple[TrafficReservation, ...]:
        """Reserve every grouped resource until exit plus tail clearance.

        Segment end times include intersection crossing dwell. A planner must
        represent each returned interval, including any waiting inside a section.
        This method consumes only detached route data and does not acquire resources.
        """
        count = len(route.points)
        if (len(starts) != count or len(ends) != count or
                any(not math.isfinite(t) for t in (*starts, *ends)) or
                any(end < start for start, end in zip(starts, ends, strict=True)) or
                any(starts[i] < ends[i - 1] for i in range(1, count))):
            raise ValueError("invalid absolute route interval timestamps")
        clearance = tail_clearance_seconds(length, speed)
        reservations = []
        index = 0
        ready = -math.inf
        while index < count:
            if starts[index] < ready:
                raise ValueError("route schedule must include section tail clearance")
            if route.lifts and route.lifts[index]:
                ready = ends[index]
                index += 1
                continue
            end = self.section_end(route, index)
            demands = self.section_demands(route, index, end, robot_width)
            ready = ends[end] + (clearance if demands else 0)
            for demand in demands:
                reservations.append(TrafficReservation(demand.resource, demand.amount, demand.unit,
                                                       starts[index], ready))
            index = end + 1
        return tuple(reservations)

    def demand_resource(self, demand: TrafficDemand) -> LaneResource:
        """Resolve a detached demand only when identity, version and unit agree."""
        from simulation.resource_pool import TRAFFIC_CONTRACT
        resource = self._pools.get(demand.resource)
        if (resource is None or demand.contract != TRAFFIC_CONTRACT or resource.unit != demand.unit
                or isinstance(demand.amount, bool) or not isinstance(demand.amount, int)
                or not 1 <= demand.amount <= resource.capacity):
            raise TrafficBlocked("traffic demand identity/unit/version mismatch")
        return resource

    def bind_elevators(self, resources: dict[str, simpy.Resource]) -> None:
        """Reference the actual cabin resources used by Robot._ride_process."""
        self._elevator_resource.clear()
        self._elevator_resource.update(resources)

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

    def demand_units(self, edge_id: int, robot_width: float) -> int:
        """통과 가능한 로봇의 점유 폭(mm)을 반환한다. 초과 요구는 거부한다."""
        need = self.width_units(robot_width)
        if not self.can_pass(edge_id, robot_width):
            raise TrafficBlocked(f"통로 {edge_id}: 로봇 폭 {robot_width:g}m 통행 불가")
        return need

    def lanes_needed(self, edge_id: int, robot_width: float) -> int:
        """이전 호출부 호환용 이름. 반환값은 차선 수가 아니라 mm이다."""
        return self.demand_units(edge_id, robot_width)

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
            resource = self._corridor_resource.get(edge_id)
            capacity = resource.capacity if resource else 0
            edge = self._edges.get(edge_id)
            if not edge or not capacity:
                return False
            needed = [self.width_units(width) for width in self._robot_widths
                      if can_pass(edge.width, width, self.profile)]
            return bool(needed) and max(needed) * 2 > capacity

        return single_file(incoming) and single_file(outgoing)

    def snapshots(self) -> list[TrafficSnapshot]:
        """통로·교차로 점유와 실제 요청 대기열의 스냅샷을 만든다.

        Returns:
            스레드 간 전달 가능한 통행 상태 목록.
        """
        snapshots = []
        for edge_id in self._edge_capacity:
            edge = self._edges[edge_id]
            start, end = self._nodes[edge.from_node], self._nodes[edge.to_node]
            resource = self._corridor_resource.get(edge_id)
            passable = (edge_id not in self._blocked_edges
                        and any(self.can_pass(edge_id, width) for width in self._robot_widths))
            snapshots.append(self._snapshot(f"edge:{edge_id}", "corridor", start.floor,
                                            start.x, start.y, end.x, end.y, resource.capacity, resource,
                                            edge.width, edge.is_bidirectional, passable))
        for node_id, resource in self._intersection_resource.items():
            node = self._nodes[node_id]
            snapshots.append(self._snapshot(f"node:{node_id}", "intersection", node.floor,
                                            node.x, node.y, node.x, node.y, 1, resource))
        active = set(self._limits.keys())
        for key, resource in list(self._pools.items()):
            if resource in active:
                continue
            if resource.users or resource.queue:
                old = self._templates[key]
                snapshots.append(replace(self._snapshot("retired:" + key, old.kind, old.floor,
                    old.x, old.y, old.end_x, old.end_y, resource.capacity, resource,
                    old.width, old.bidirectional, False), draining=True))
            else:
                del self._pools[key]
                self._templates.pop(key, None)
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
                               width, bidirectional, passable,
                               resource.unit if resource else WIDTH_UNIT,
                               resource.resource_id if resource else identifier,
                               draining=self._paused or bool(resource and resource.count > resource.capacity))


if __name__ == "__main__":
    from utils.config_models import PathNodeConfig

    env = simpy.Environment()
    nodes = [
        PathNodeConfig(1, 0, 0, "station", "WS-01"),
        PathNodeConfig(2, 100, 0),
        PathNodeConfig(3, 200, 0, "station", "WS-02"),
    ]
    for width in (5.0, 1.8):
        edges = [PathEdgeConfig(1, 1, 2, width=width), PathEdgeConfig(2, 2, 3, width=width)]
        net = CorridorNetwork(env, FactoryConfig(FactoryProfile(), [], [], [], [], nodes, edges))
        print(f"width={width}m capacity={net._edge_capacity[1]}mm, "
              f"소형(0.6m)={net.demand_units(1, 0.6)}mm, 팔레트(1.1m)={net.demand_units(1, 1.1)}mm")
