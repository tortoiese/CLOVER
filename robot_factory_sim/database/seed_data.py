"""기본 공장 구성 시드 데이터 (스테이션 5개, 충전소 2개, 로봇 5대) + 물리 제약 시나리오 프리셋."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

import config
from database.db_manager import DBManager
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    FactoryProfile,
    ObstacleConfig,
    OrderRecord,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)
from utils.layout_tools import build_grid_network

SEED_NODE_SPACING: float = 100.0

# ---------------------------------------------------------------------------
# 물리 제약 시나리오 프리셋
#
# 레이아웃(중앙복도형/격자형/루프형)은 통로 폭·교차점·엘리베이터 같은 '맵' 변수를 담당하고,
# 난이도(S1~S4)는 작업량·설비 고장·로봇 수 같은 '운영' 변수만 조정한다 — 물리 레이아웃은
# 난이도와 무관하게 그대로 유지된다.
# ---------------------------------------------------------------------------
LAYOUT_PRESETS: tuple[str, ...] = ("corridor", "grid", "loop")
DIFFICULTY_PRESETS: tuple[str, ...] = ("S1", "S2", "S3", "S4")
DIFFICULTY_LABELS: dict[str, str] = {
    "S1": "평시", "S2": "증속", "S3": "교란", "S4": "증설",
}
_DEFAULT_CORRIDOR_WIDTH: float = 2.0  # m (레이아웃 기본 병목 통로 폭)

DIFFICULTY_TASK_RATE: dict[str, float] = {
    "S1": config.DEFAULT_TASK_RATE,
    "S2": min(config.TASK_RATE_MAX, config.DEFAULT_TASK_RATE * 1.8),
    "S3": config.DEFAULT_TASK_RATE,
    "S4": config.DEFAULT_TASK_RATE,
}


def _node(
    nodes: list[PathNodeConfig], x: float, y: float, node_type: str = "waypoint",
    link: str | None = None, floor: int = 1, capacity: int | None = None,
) -> int:
    """노드를 추가하고 ID를 반환한다 (임시 음수 ID)."""
    node_id = -(len(nodes) + 1)
    nodes.append(PathNodeConfig(node_id, x, y, node_type, link, floor, capacity))
    return node_id


def _edge(edges: list[PathEdgeConfig], a: int, b: int, width: float | None = None,
          bidirectional: bool = True) -> int:
    """엣지를 추가하고 ID를 반환한다 (임시 음수 ID)."""
    edge_id = -(len(edges) + 1)
    edges.append(PathEdgeConfig(edge_id, a, b, None, bidirectional, width or config.DEFAULT_EDGE_WIDTH))
    return edge_id


def _corridor_layout(main_width: float = _DEFAULT_CORRIDOR_WIDTH) -> FactoryConfig:
    """중앙복도형: 좌/우 스테이션 블록을 폭 main_width(m)의 중앙 통로 하나로 연결한다.

    충전소로 가는 분기점을 교차점(capacity=1)으로 둬서 물리 제약을 함께 보여준다.

    Args:
        main_width: 중앙 통로(병목 구간) 폭 (m)

    Returns:
        FactoryConfig
    """
    profile = FactoryProfile(id=1, factory_name="중앙복도형 공장", floor_width=900.0, floor_height=400.0,
                              grid_cell_size=50.0)
    stations = [
        WorkStationConfig("WS-L1", "조립(좌)", 100, 100, task_type="assembly", avg_process_time=6.0),
        WorkStationConfig("WS-L2", "용접(좌)", 100, 300, task_type="welding", avg_process_time=8.0),
        WorkStationConfig("WS-R1", "검사(우)", 800, 100, task_type="inspection", avg_process_time=4.0),
        WorkStationConfig("WS-R2", "포장(우)", 800, 300, task_type="packaging", avg_process_time=5.0),
    ]
    chargers = [ChargingStationConfig("CS-01", 450, 350, capacity=2, charge_rate=2.0)]
    robots = [RobotConfig(f"R-{i:02d}") for i in range(1, 5)]

    nodes: list[PathNodeConfig] = []
    edges: list[PathEdgeConfig] = []
    n_l1 = _node(nodes, 150, 100, "station", "WS-L1")
    n_l2 = _node(nodes, 150, 300, "station", "WS-L2")
    n_join_l = _node(nodes, 200, 200)
    n_mid1 = _node(nodes, 350, 200)
    n_x = _node(nodes, 450, 200, "intersection")
    n_cs = _node(nodes, 450, 350, "charger", "CS-01")
    n_mid2 = _node(nodes, 550, 200)
    n_join_r = _node(nodes, 700, 200)
    n_r1 = _node(nodes, 750, 100, "station", "WS-R1")
    n_r2 = _node(nodes, 750, 300, "station", "WS-R2")

    _edge(edges, n_l1, n_join_l)
    _edge(edges, n_l2, n_join_l)
    _edge(edges, n_join_l, n_mid1, main_width)
    _edge(edges, n_mid1, n_x, main_width)
    _edge(edges, n_x, n_cs)
    _edge(edges, n_x, n_mid2, main_width)
    _edge(edges, n_mid2, n_join_r, main_width)
    _edge(edges, n_join_r, n_r1)
    _edge(edges, n_join_r, n_r2)

    return FactoryConfig(profile, stations, chargers, robots, [], nodes, edges)


def _grid_layout(main_width: float = _DEFAULT_CORRIDOR_WIDTH) -> FactoryConfig:
    """격자형: 기본 그리드 경로망 + 중앙 교차점 1개 + 교차점 인접 통로 폭 조절.

    Args:
        main_width: 교차점 인접 통로(병목 구간) 폭 (m)

    Returns:
        FactoryConfig
    """
    factory = build_default_config()
    profile = factory.profile
    center = min(factory.nodes, key=lambda n: math.hypot(n.x - profile.floor_width / 2, n.y - profile.floor_height / 2))
    if center.node_type == "waypoint":
        center.node_type = "intersection"
    for edge in factory.edges:
        if edge.from_node == center.id or edge.to_node == center.id:
            edge.width = main_width
    return factory


def _loop_layout(main_width: float = _DEFAULT_CORRIDOR_WIDTH) -> FactoryConfig:
    """루프형: 1층 루프 통로(교차점 1개) + 엘리베이터로 연결된 2층 포장 구역.

    2층은 1층과 겹치지 않도록 같은 평면 좌표계의 오른쪽 영역에 배치한다
    (최단경로 계산이 좌표 기반이라, 층을 겹쳐 그리면 다른 층 노드와 혼동될 수 있다).

    Args:
        main_width: 루프 통로(병목 구간) 폭 (m)

    Returns:
        FactoryConfig
    """
    profile = FactoryProfile(id=1, factory_name="루프형 공장(2층)", floor_width=1000.0, floor_height=600.0,
                              grid_cell_size=50.0)
    stations = [
        WorkStationConfig("WS-A1", "조립", 150, 150, task_type="assembly", avg_process_time=6.0),
        WorkStationConfig("WS-A2", "용접", 450, 150, task_type="welding", avg_process_time=8.0),
        WorkStationConfig("WS-A3", "검사", 450, 450, task_type="inspection", avg_process_time=4.0),
        WorkStationConfig("WS-B1", "포장(2F)", 900, 300, task_type="packaging", avg_process_time=5.0),
    ]
    chargers = [ChargingStationConfig("CS-01", 300, 300, capacity=2, charge_rate=2.0)]
    robots = [RobotConfig(f"R-{i:02d}") for i in range(1, 5)]

    nodes: list[PathNodeConfig] = []
    edges: list[PathEdgeConfig] = []
    n_a1 = _node(nodes, 150, 150, "station", "WS-A1")
    n_a2 = _node(nodes, 450, 150, "station", "WS-A2")
    n_a3 = _node(nodes, 450, 450, "station", "WS-A3")
    n_cs = _node(nodes, 300, 300, "charger", "CS-01")
    n_x = _node(nodes, 300, 150, "intersection")
    n_ev1 = _node(nodes, 300, 450, "elevator", "EV-01", floor=1)
    n_ev2 = _node(nodes, 900, 450, "elevator", "EV-01", floor=2)
    n_b1 = _node(nodes, 900, 300, "station", "WS-B1", floor=2)

    _edge(edges, n_a1, n_x, main_width)
    _edge(edges, n_x, n_a2, main_width)
    _edge(edges, n_a2, n_a3, main_width)
    _edge(edges, n_a3, n_cs)
    _edge(edges, n_cs, n_x)
    _edge(edges, n_x, n_ev1, main_width)
    _edge(edges, n_ev1, n_ev2)      # 엘리베이터 샤프트 (층간 — 거리는 이동시간 계산에 쓰이지 않는다)
    _edge(edges, n_ev2, n_b1)

    return FactoryConfig(profile, stations, chargers, robots, [], nodes, edges)


LAYOUT_BUILDERS: dict[str, Callable[[float], FactoryConfig]] = {
    "corridor": _corridor_layout,
    "grid": _grid_layout,
    "loop": _loop_layout,
}


def apply_difficulty(factory: FactoryConfig, difficulty: str) -> tuple[FactoryConfig, dict[str, float]]:
    """난이도(운영 변수: 작업량/설비 고장/로봇 수)를 레이아웃에 적용한다.

    통로 폭·교차점·엘리베이터 같은 물리(맵) 변수는 건드리지 않는다.

    Args:
        factory: 레이아웃(맵) 구성
        difficulty: "S1"(평시) | "S2"(증속) | "S3"(교란) | "S4"(증설)

    Returns:
        (조정된 FactoryConfig, SimulationEngine(params=...)에 바로 넘길 파라미터)
    """
    if difficulty not in DIFFICULTY_PRESETS:
        raise ValueError(f"알 수 없는 난이도: {difficulty}")
    result = FactoryConfig.from_dict(factory.to_dict())
    if difficulty == "S3" and result.chargers:
        result.chargers[0].is_active = False
    elif difficulty == "S4":
        base = result.robots[0] if result.robots else RobotConfig("R-00")
        start = len(result.robots) + 1
        for i in range(start, start + 2):
            result.robots.append(RobotConfig(f"R-{i:02d}", base.robot_type, base.battery_capacity,
                                              base.move_speed, base.battery_drain_rate, base.charge_threshold))
    return result, {"task_rate": DIFFICULTY_TASK_RATE[difficulty]}


def build_scenario_config(
    layout: str = "corridor", difficulty: str = "S1", corridor_width: float | None = None,
) -> tuple[FactoryConfig, dict[str, float]]:
    """물리 제약 시나리오 구성을 만든다.

    Args:
        layout: "corridor"(중앙복도형) | "grid"(격자형) | "loop"(루프형, 2층+EV)
        difficulty: "S1" | "S2" | "S3" | "S4"
        corridor_width: 병목 구간 통로 폭 (m). None이면 레이아웃 기본값(2.0m)을 쓴다.

    Returns:
        (FactoryConfig, SimulationEngine params)
    """
    builder = LAYOUT_BUILDERS.get(layout)
    if builder is None:
        raise ValueError(f"알 수 없는 레이아웃: {layout}")
    base = builder(corridor_width if corridor_width is not None else _DEFAULT_CORRIDOR_WIDTH)
    return apply_difficulty(base, difficulty)


def seed_scenario_presets(db: DBManager) -> list[str]:
    """레이아웃 프리셋을 config_presets에 저장한다 (기존 공장 설정 편집기의 '불러오기'에서 선택 가능).

    난이도(S1~S4)는 운영 변수라 프리셋에 담지 않고, 불러온 뒤 SimWorker의 작업률 조정으로 적용한다.

    Args:
        db: 초기화된 DBManager

    Returns:
        저장된 프리셋 이름 목록
    """
    names = []
    for layout in LAYOUT_PRESETS:
        name = f"physical-{layout}"
        db.save_preset(name, LAYOUT_BUILDERS[layout](_DEFAULT_CORRIDOR_WIDTH), changed_by="seed")
        names.append(name)
    return names


def build_default_config() -> FactoryConfig:
    """기본 공장 구성을 만든다.

    Returns:
        FactoryConfig (노드/엣지/장애물 ID는 음수 임시 ID)
    """
    profile = FactoryProfile(
        id=1,
        factory_name="클로봇 데모 공장",
        floor_width=config.DEFAULT_FLOOR_WIDTH,
        floor_height=config.DEFAULT_FLOOR_HEIGHT,
        grid_cell_size=config.DEFAULT_GRID_CELL_SIZE,
    )
    stations = [
        WorkStationConfig("WS-01", "조립 A", 100, 100, task_type="assembly", avg_process_time=6.0),
        WorkStationConfig("WS-02", "용접", 300, 100, task_type="welding", avg_process_time=8.0, process_time_std=1.5),
        WorkStationConfig("WS-03", "도장", 100, 500, task_type="painting", avg_process_time=7.0),
        WorkStationConfig("WS-04", "검사", 300, 500, task_type="inspection", avg_process_time=4.0,
                          process_time_std=0.5),
        WorkStationConfig("WS-05", "포장", 700, 100, task_type="packaging", avg_process_time=5.0),
    ]
    chargers = [
        ChargingStationConfig("CS-01", 700, 500, capacity=2, charge_rate=2.0),
        ChargingStationConfig("CS-02", 300, 300, capacity=1, charge_rate=2.5),
    ]
    robots = [
        RobotConfig(f"R-{i:02d}", move_speed=2.0, battery_drain_rate=0.35, charge_threshold=20.0)
        for i in range(1, 6)
    ]
    obstacles = [
        ObstacleConfig(-1, "wall", 500, 0, 500, 600, False, "구획 벽"),
        ObstacleConfig(-2, "door", 500, 250, 500, 350, True, "출입문"),
        ObstacleConfig(-3, "obstacle", 190, 330, 210, 470, False, "팔레트 랙"),
    ]
    nodes, edges = build_grid_network(profile.floor_width, profile.floor_height, SEED_NODE_SPACING, stations,
                                      chargers)
    return FactoryConfig(profile, stations, chargers, robots, obstacles, nodes, edges)


def build_default_orders() -> list[OrderRecord]:
    """기본 생산 주문을 만든다.

    Returns:
        OrderRecord 리스트 (order_id는 삽입 시 부여)
    """
    now = datetime.now()
    return [
        OrderRecord("", "제품A", 120, priority="normal", required_stations=["WS-01", "WS-02"], created_at=now),
        OrderRecord("", "제품C", 80, priority="high", required_stations=["WS-03", "WS-04"], created_at=now),
    ]


def seed_default_factory(db: DBManager) -> bool:
    """DB가 비어 있으면 기본 구성을 삽입한다.

    Args:
        db: 초기화된 DBManager

    Returns:
        시드 삽입 여부
    """
    if db.is_seeded():
        return False
    db.save_factory_config(build_default_config(), changed_by="seed")
    for order in build_default_orders():
        order.order_id = db.next_order_id()
        db.insert_order(order, changed_by="seed")
    return True


if __name__ == "__main__":
    seed_db = DBManager(":memory:")
    seed_db.initialize()
    print("seeded:", seed_default_factory(seed_db))
    factory = seed_db.load_factory_config()
    print(len(factory.stations), "stations /", len(factory.nodes), "nodes /", len(factory.edges), "edges")

    # 물리 제약 데모: 같은 레이아웃(중앙복도형)에서 통로 폭만 줄이면 통로 대기·교착이 늘어난다.
    # 병목을 눈에 보이게 하려고 로봇 수/작업률을 키운 '증설+증속' 부하로 돌린다 (운영 변수).
    from simulation.corridor import lane_capacity
    from simulation.engine import SimulationEngine

    print("\n[물리 제약 데모] 중앙복도형 — 통로 폭에 따른 통로 대기/교착/처리량")
    for width in (3.0, 1.8, 1.2):
        scenario = _corridor_layout(width)
        scenario.robots = [RobotConfig(f"R-{i:02d}") for i in range(1, 13)]
        lanes = lane_capacity(width, scenario.profile)
        engine = SimulationEngine(scenario, "baseline", params={"task_rate": 2.0})
        engine.env.run(until=3600)
        kpis = engine.kpis()
        print(
            f"  통로폭 {width:>4.1f}m (차선 {lanes}개) → completed={engine.metrics.total_completed:>3d}  "
            f"corridor_wait={kpis['corridor_wait']:.1f}s  deadlock={kpis['deadlock_count']:.0f}건"
        )
