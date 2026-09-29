"""SimState — 시뮬레이션 상태 스냅샷.

SimWorker가 매 틱 생성하여 Qt 시그널로 GUI 스레드에 전달한다. 전달 후에는 수정하지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from utils.config_models import FactoryConfig

Point = tuple[float, float]


@dataclass
class RobotSnapshot:
    """로봇 1대 상태."""

    id: str
    x: float
    y: float
    state: str
    battery_pct: float
    color_index: int
    task_id: int | None = None
    task_priority: int | None = None
    order_id: str | None = None
    target_id: str | None = None
    emergency_order: str | None = None
    route: list[Point] = field(default_factory=list)
    tasks_done: int = 0
    robot_type: str = "AGV"


@dataclass
class StationSnapshot:
    """작업 스테이션 상태."""

    id: str
    label: str
    x: float
    y: float
    width: float
    height: float
    task_type: str
    active: bool
    queue_length: int
    pending_tasks: int
    busy: bool
    current_order: str | None
    current_robot: str | None
    processed: int
    utilization: float
    dedicated_order: str | None = None


@dataclass
class ChargerSnapshot:
    """충전소 상태."""

    id: str
    x: float
    y: float
    capacity: int
    occupied: int
    queue_length: int
    active: bool
    charge_rate: float


@dataclass
class OrderSnapshot:
    """주문 진행 상태."""

    order_id: str
    product_type: str
    quantity: int
    completed: int
    priority: str
    status: str
    deadline_remaining: float | None  # sim-sec, 없으면 None
    eta_seconds: float
    at_risk: bool
    elapsed: float
    dedicated_stations: list[str] = field(default_factory=list)


@dataclass
class TimelineEvent:
    """이벤트 타임라인 항목."""

    sim_time: float
    kind: str
    message: str


@dataclass
class SimState:
    """시뮬레이션 전체 스냅샷."""

    sim_time: float
    sim_datetime: datetime
    wall_elapsed: float
    speed: int
    running: bool
    mode: str
    layout_version: int
    layout: FactoryConfig
    robots: list[RobotSnapshot]
    stations: list[StationSnapshot]
    chargers: list[ChargerSnapshot]
    orders: list[OrderSnapshot]
    pending_tasks: int
    avg_task_cycle: float
    kpis: dict[str, float]
    engine_kpis: dict[str, dict[str, float]]
    params: dict[str, float]
    events: list[TimelineEvent] = field(default_factory=list)


def state_to_context(state: SimState | None) -> dict[str, Any]:
    """SimState를 에이전트용 평범한 dict(sim_context)로 변환한다.

    agent 패키지가 simulation 패키지에 의존하지 않도록 dict로 넘긴다.

    Args:
        state: 최신 스냅샷 (없으면 빈 dict)

    Returns:
        sim_context dict
    """
    if state is None:
        return {}
    process_times = {s.id: s.avg_process_time for s in state.layout.stations}
    robots = []
    for robot in state.robots:
        item = asdict(robot)
        item.pop("route", None)
        robots.append(item)
    return {
        "factory_name": state.layout.profile.factory_name,
        "floor": {"width": state.layout.profile.floor_width, "height": state.layout.profile.floor_height},
        "sim_time": state.sim_time,
        "sim_datetime": state.sim_datetime.isoformat(sep=" ", timespec="seconds"),
        "speed": state.speed,
        "running": state.running,
        "mode": state.mode,
        "pending_tasks": state.pending_tasks,
        "avg_task_cycle": state.avg_task_cycle,
        "kpis": dict(state.kpis),
        "engine_kpis": {mode: dict(values) for mode, values in state.engine_kpis.items()},
        "params": dict(state.params),
        "robots": robots,
        "stations": [{**asdict(s), "avg_process_time": process_times.get(s.id, 0.0)} for s in state.stations],
        "chargers": [asdict(c) for c in state.chargers],
        "orders": [asdict(o) for o in state.orders],
        "obstacles": [asdict(o) for o in state.layout.obstacles],
    }
