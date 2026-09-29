"""SimulationEngine — SimPy 공장 모델 1개.

SimWorker는 Baseline / 최적화 엔진 2개를 같은 시드·같은 작업 부하로 나란히 실행하여 KPI를 비교한다.
엔진은 Qt에 의존하지 않으므로 GUI 없이 단독 실행/테스트할 수 있다 (``python -m simulation.engine``).
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from collections.abc import Callable, Generator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import simpy

import config
from simulation.path_graph import PathGraph
from simulation.robot import InterruptKind, Robot
from simulation.scheduler import BaseScheduler, create_scheduler
from simulation.state import ChargerSnapshot, OrderSnapshot, RobotSnapshot, StationSnapshot, TimelineEvent
from simulation.station import ChargingStation, WorkStation
from simulation.task_generator import Task, TaskGenerator
from utils.config_models import ChargingStationConfig, FactoryConfig, OrderRecord, RobotConfig, WorkStationConfig
from utils.event_types import OrderStatus, ProductionEventType, RobotState, Severity, TimelineKind
from utils.metrics import MetricsCollector

Point = tuple[float, float]

TIMELINE_LIMIT: int = 300
ORDER_SNAPSHOT_LIMIT: int = 12
SPAWN_SPACING: float = 14.0
SURGE_RESET_RATIO: float = 0.8
TUNABLE_PARAMS: frozenset[str] = frozenset({"task_rate", "min_charge_mode"})


@dataclass
class OrderProgress:
    """엔진 내부 주문 진행 상태 (DB 주문과 별개로 엔진마다 독립적으로 추적)."""

    order_id: str
    product: str
    quantity: int
    completed: int
    priority: str
    deadline: float | None      # sim-sec
    created_at: float           # sim-sec
    required_stations: list[str]
    db_priority: str
    status: str = OrderStatus.PENDING
    in_flight: int = 0
    finished_at: float | None = None
    detected_at: float | None = None
    responded: bool = False
    risk_flagged: bool = False

    @property
    def level(self) -> int:
        """숫자 우선순위 (작을수록 긴급)."""
        return config.PRIORITY_LEVELS.get(self.priority, config.BACKGROUND_PRIORITY)

    @property
    def remaining(self) -> int:
        """남은 수량."""
        return max(0, self.quantity - self.completed)

    @property
    def is_active(self) -> bool:
        """진행 중 여부."""
        return self.status in OrderStatus.ACTIVE


@dataclass
class EngineAlert:
    """엔진이 감지한 생산 이벤트 (SimWorker가 production_events에 기록)."""

    event_type: str
    order_id: str | None
    payload: dict[str, Any]


class SimulationEngine:
    """SimPy 환경 + 공장 엔티티 + 스케줄러."""

    def __init__(
        self,
        factory: FactoryConfig,
        mode: str = config.DEFAULT_SCHEDULER_MODE,
        seed: int = config.RANDOM_SEED,
        epoch: datetime | None = None,
        params: dict[str, float] | None = None,
    ) -> None:
        """엔진을 생성하고 SimPy 프로세스를 등록한다.

        Args:
            factory: 공장 구성 (DB 스냅샷)
            mode: 스케줄러 모드 ("baseline" | "optimized")
            seed: 난수 시드 (두 엔진이 같은 값을 쓰면 같은 작업 부하가 생성된다)
            epoch: sim-time 0에 해당하는 실제 시각 (주문 납기 환산용)
            params: 초기 파라미터 (task_rate, min_charge_mode)
        """
        self.env = simpy.Environment()
        self.mode = mode
        self.scheduler: BaseScheduler = create_scheduler(mode)
        self.epoch = epoch or datetime.now()
        self.params: dict[str, float] = {"task_rate": config.DEFAULT_TASK_RATE, "min_charge_mode": 0.0}
        if params:
            self.params.update(params)
        self.metrics = MetricsCollector()
        self.factory = factory
        self.path_graph = PathGraph(factory)
        self.layout_version = 0
        self.stations: dict[str, WorkStation] = {}
        self.chargers: dict[str, ChargingStation] = {}
        self.robots: dict[str, Robot] = {}
        self.retired: list[Robot] = []
        self.pending_tasks: list[Task] = []
        self.orders: dict[str, OrderProgress] = {}
        self.timeline: deque[TimelineEvent] = deque(maxlen=TIMELINE_LIMIT)
        self._alerts: list[EngineAlert] = []
        self._notices: list[tuple[str, str]] = []
        self._task_ids = itertools.count(1)
        self._color_ids = itertools.count()
        self._surge_flagged = False
        self._unreachable_warned: set[tuple[int, str]] = set()
        self._process_rng = np.random.default_rng(seed + 1)

        for station_cfg in factory.stations:
            self._create_station(station_cfg)
        for charger_cfg in factory.chargers:
            self._create_charger(charger_cfg)
        for robot_cfg in factory.robots:
            if robot_cfg.is_active:
                self._create_robot(robot_cfg)

        self.generator = TaskGenerator(self, np.random.default_rng(seed))
        self.env.process(self.generator.poisson_process())
        self.env.process(self.generator.order_release_process())
        self.env.process(self._dispatch_process())
        self.env.process(self._monitor_process())

    # ------------------------------------------------------------------
    # 팩토리 메서드
    # ------------------------------------------------------------------
    def _create_station(self, cfg: WorkStationConfig) -> WorkStation:
        """작업 스테이션을 생성·등록한다."""
        station = WorkStation(self.env, cfg, self._process_rng)
        self.stations[cfg.id] = station
        return station

    def _create_charger(self, cfg: ChargingStationConfig) -> ChargingStation:
        """충전소를 생성·등록한다."""
        charger = ChargingStation(self.env, cfg)
        self.chargers[cfg.id] = charger
        return charger

    def _create_robot(self, cfg: RobotConfig) -> Robot:
        """로봇을 생성·등록한다 (충전소 근처에 배치)."""
        index = next(self._color_ids)
        robot = Robot(self, cfg, self._spawn_point(index), index)
        self.robots[cfg.id] = robot
        return robot

    def _spawn_point(self, index: int) -> Point:
        """신규 로봇 배치 위치."""
        chargers = [c for c in self.chargers.values() if c.active]
        if chargers:
            charger = chargers[index % len(chargers)]
            base = self.path_graph.facility_point(charger.id) or (charger.cfg.x, charger.cfg.y)
        elif self.path_graph.nodes:
            base = next(iter(self.path_graph.nodes.values()))
        else:
            profile = self.factory.profile
            base = (profile.floor_width / 2.0, profile.floor_height / 2.0)
        offset = ((index % 5) - 2) * SPAWN_SPACING
        return base[0] + offset, base[1]

    # ------------------------------------------------------------------
    # 조회 (스케줄러 / 로봇 / 생성기가 사용)
    # ------------------------------------------------------------------
    @property
    def now(self) -> float:
        """현재 sim-time."""
        return float(self.env.now)

    def sim_datetime(self) -> datetime:
        """현재 sim-time에 해당하는 시각.

        Returns:
            epoch + sim-time
        """
        return self.epoch + timedelta(seconds=self.now)

    def robot_list(self) -> list[Robot]:
        """가동 중인 로봇 목록.

        Returns:
            Robot 리스트
        """
        return list(self.robots.values())

    def robot_ids(self) -> list[str]:
        """가동 중인 로봇 ID 목록 (등록 순).

        Returns:
            ID 리스트
        """
        return list(self.robots)

    def station_available(self, station_id: str) -> bool:
        """스테이션이 가동 중이고 경로망에 연결돼 있는지.

        Args:
            station_id: 스테이션 ID

        Returns:
            사용 가능 여부
        """
        station = self.stations.get(station_id)
        return station is not None and station.active and station_id in self.path_graph.facility_nodes

    def pending_count_by_station(self) -> dict[str, int]:
        """스테이션별 미배정 작업 수.

        Returns:
            station_id → 개수
        """
        counts: dict[str, int] = {}
        for task in self.pending_tasks:
            counts[task.station_id] = counts.get(task.station_id, 0) + 1
        return counts

    def background_station_ids(self) -> list[str]:
        """일반(백그라운드) 작업을 받을 수 있는 스테이션 (전용 지정 스테이션 제외).

        Returns:
            ID 리스트
        """
        return [sid for sid, s in self.stations.items() if self.station_available(sid) and s.dedicated_order is None]

    def background_task_count(self) -> int:
        """대기 중인 일반 작업 수.

        Returns:
            개수
        """
        return sum(1 for t in self.pending_tasks if t.order_id is None)

    def _usable_stations(self, task: Task) -> list[str]:
        """작업이 지금 처리될 수 있는 스테이션."""
        return [s for s in (task.allowed_stations or (task.station_id,)) if self.station_available(s)]

    def _remove_pending(self, task: Task) -> bool:
        """대기열에서 작업을 제거한다 (동일성 비교)."""
        for index, pending in enumerate(self.pending_tasks):
            if pending is task:
                del self.pending_tasks[index]
                return True
        return False

    def take_task(self, predicate: Callable[[Task], bool]) -> Task | None:
        """조건에 맞는 가장 긴급한 대기 작업을 꺼낸다.

        Args:
            predicate: 작업 필터

        Returns:
            작업 또는 None
        """
        candidates = [t for t in self.pending_tasks if predicate(t) and self._usable_stations(t)]
        if not candidates:
            return None
        task = min(candidates, key=lambda t: (t.priority, t.created_at))
        self._remove_pending(task)
        return task

    # ------------------------------------------------------------------
    # 작업 생명주기
    # ------------------------------------------------------------------
    def add_task(
        self,
        product: str,
        station_id: str,
        priority: int,
        allowed_stations: tuple[str, ...] = (),
        order_id: str | None = None,
        deadline: float | None = None,
    ) -> Task:
        """작업을 대기열에 추가한다.

        Args:
            product: 제품명
            station_id: 기본 배정 스테이션
            priority: 숫자 우선순위 (작을수록 긴급)
            allowed_stations: 처리 가능한 스테이션 목록
            order_id: 주문 ID (일반 작업이면 None)
            deadline: 납기 (sim-sec)

        Returns:
            생성된 작업
        """
        task = Task(
            task_id=next(self._task_ids),
            station_id=station_id,
            product=product,
            priority=priority,
            created_at=self.now,
            order_id=order_id,
            allowed_stations=tuple(allowed_stations),
            deadline=deadline,
        )
        self.pending_tasks.append(task)
        return task

    def assign(self, task: Task, robot: Robot) -> None:
        """작업을 로봇에 배정한다.

        Args:
            task: 작업
            robot: 로봇
        """
        self._remove_pending(task)
        if not self.station_available(task.station_id):
            usable = self._usable_stations(task)
            if usable:
                task.station_id = min(usable, key=lambda s: self.path_graph.distance_to_facility(robot.x, robot.y, s))
        task.assigned_at = self.now
        order = self.orders.get(task.order_id) if task.order_id else None
        if order is not None and order.detected_at is not None and not order.responded:
            order.responded = True
            self.metrics.record_response(self.now - order.detected_at)
        robot.assign(task)

    def requeue_task(self, task: Task) -> None:
        """중단된 작업을 대기열로 되돌린다.

        Args:
            task: 작업
        """
        task.robot_id = None
        task.assigned_at = None
        order = self.orders.get(task.order_id) if task.order_id else None
        if order is not None and not order.is_active:
            order.in_flight = max(0, order.in_flight - 1)
            return
        if not self.station_available(task.station_id):
            self._rehome_task(task)
        self.pending_tasks.append(task)

    def on_task_started(self, task: Task, robot: Robot) -> None:
        """스테이션에서 작업이 시작됐을 때 호출된다.

        Args:
            task: 작업
            robot: 로봇
        """
        task.started_at = self.now
        self.metrics.record_wait(self.now - task.created_at)

    def complete_task(self, task: Task, robot: Robot) -> None:
        """작업 완료를 처리한다.

        Args:
            task: 작업
            robot: 로봇
        """
        started = task.assigned_at if task.assigned_at is not None else task.created_at
        self.metrics.record_task_completed(self.now, self.now - started)
        order = self.orders.get(task.order_id) if task.order_id else None
        if order is None:
            return
        order.in_flight = max(0, order.in_flight - 1)
        if not order.is_active:
            return
        order.completed += 1
        order.status = OrderStatus.IN_PROGRESS
        if order.completed >= order.quantity:
            self._finish_order(order)

    def report_unreachable(self, task: Task, robot: Robot) -> None:
        """로봇이 스테이션까지 경로를 찾지 못했을 때 호출된다.

        Args:
            task: 작업
            robot: 로봇
        """
        self.requeue_task(task)
        key = (self.layout_version, task.station_id)
        if key not in self._unreachable_warned:
            self._unreachable_warned.add(key)
            self._notify(Severity.WARNING, f"{robot.robot_id} → {task.station_id} 경로 없음 (벽/장애물 차단). 경로망을 확인하세요.")

    def on_robot_fault(self, robot: Robot) -> None:
        """로봇 방전 시 호출된다.

        Args:
            robot: 로봇
        """
        message = f"{robot.robot_id} 배터리 방전 — 구조 대기 ({config.FAULT_RECOVERY_TIME:.0f}s)"
        self._timeline(TimelineKind.FAILURE, message)
        self._notify(Severity.WARNING, f"⚫ {message}")

    def _rehome_task(self, task: Task) -> None:
        """처리 불가 스테이션의 작업을 대체 스테이션으로 옮긴다."""
        usable = self._usable_stations(task)
        if usable:
            task.station_id = usable[0]
            return
        original = self.stations.get(task.station_id)
        task_type = original.cfg.task_type if original else None
        alternatives = [
            sid for sid, s in self.stations.items()
            if self.station_available(sid) and s.dedicated_order in (None, task.order_id)
        ]
        same_type = [sid for sid in alternatives if self.stations[sid].cfg.task_type == task_type]
        choices = same_type or alternatives
        if not choices:
            return
        loads = self.pending_count_by_station()
        task.station_id = min(choices, key=lambda s: loads.get(s, 0) + self.stations[s].waiting)
        task.allowed_stations = tuple(choices)

    # ------------------------------------------------------------------
    # 로봇 제어
    # ------------------------------------------------------------------
    def send_to_charge(self, robot: Robot) -> bool:
        """로봇을 충전소로 보낸다.

        Args:
            robot: 로봇

        Returns:
            요청 전달 여부
        """
        if robot.state in (RobotState.CHARGING, RobotState.CHARGE_WAIT) or robot.target_id in self.chargers:
            return False
        if robot.state == RobotState.IDLE and robot.current_task is None:
            robot.forced_charge = True
            robot.wake()
            return True
        return robot.interrupt(InterruptKind.CHARGE)

    def preempt(self, robot: Robot, task: Task) -> bool:
        """로봇의 현재 작업을 선점하고 새 작업을 준다.

        Args:
            robot: 대상 로봇
            task: 새 작업 (대기열에서 예약된다)

        Returns:
            선점 여부
        """
        self._remove_pending(task)
        if not robot.interrupt(InterruptKind.PREEMPT, task):
            self.pending_tasks.append(task)
            return False
        task.robot_id = robot.robot_id
        return True

    def recall(self, robot: Robot, task: Task | None = None) -> bool:
        """충전 중(또는 충전하러 가는) 로봇을 긴급 복귀시킨다.

        Args:
            robot: 대상 로봇
            task: 복귀 후 바로 수행할 작업

        Returns:
            복귀 여부
        """
        charging = robot.state in (RobotState.CHARGING, RobotState.CHARGE_WAIT)
        heading = robot.state == RobotState.MOVING and robot.target_id in self.chargers
        if not (charging or heading):
            return False
        return robot.interrupt(InterruptKind.RECALL, task)

    def precharge(self) -> list[str]:
        """유휴 로봇 중 잔량이 낮은 로봇을 선충전 보낸다.

        Returns:
            충전 보낸 로봇 ID
        """
        sent = []
        for robot in self.robot_list():
            if robot.is_available() and robot.battery_pct < config.PRECHARGE_LEVEL and self.send_to_charge(robot):
                sent.append(robot.robot_id)
        return sent

    def update_param(self, key: str, value: float) -> float:
        """시뮬레이션 파라미터를 변경한다 (파라미터 변경의 유일한 진입점).

        Args:
            key: task_rate | min_charge_mode
            value: 새 값

        Returns:
            적용된 값
        """
        if key not in TUNABLE_PARAMS:
            raise KeyError(f"변경할 수 없는 파라미터: {key}")
        if key == "task_rate":
            value = min(config.TASK_RATE_MAX, max(config.TASK_RATE_MIN, float(value)))
        else:
            value = 1.0 if value else 0.0
        self.params[key] = value
        return value

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def _to_sim(self, value: datetime | None) -> float | None:
        """datetime → sim-sec."""
        return (value - self.epoch).total_seconds() if value is not None else None

    def sync_orders(self, records: list[OrderRecord]) -> None:
        """DB 주문 목록을 엔진 상태에 반영한다 (신규·취소·수량/우선순위/납기 변경).

        Args:
            records: DB 주문 전체
        """
        for record in records:
            order = self.orders.get(record.order_id)
            if order is None:
                if record.is_active:
                    self._add_order(record)
                continue
            if record.status == OrderStatus.CANCELLED and order.is_active:
                self._cancel_order(order)
            if not order.is_active:
                continue
            if record.quantity != order.quantity:
                order.quantity = record.quantity
                self._timeline(TimelineKind.INFO, f"{order.order_id} 수량 변경 → {record.quantity}")
                if order.completed >= order.quantity:
                    self._finish_order(order)
                    continue
            if record.priority != order.db_priority:
                order.db_priority = record.priority
                self._set_order_priority(order, record.priority)
            deadline = self._to_sim(record.deadline)
            if deadline != order.deadline:
                order.deadline = deadline
                order.risk_flagged = False
            if record.required_stations != order.required_stations:
                order.required_stations = list(record.required_stations)

    def _add_order(self, record: OrderRecord) -> None:
        """신규 주문 등록."""
        created = self._to_sim(record.created_at)
        order = OrderProgress(
            order_id=record.order_id,
            product=record.product_type,
            quantity=record.quantity,
            completed=record.completed,
            priority=record.priority,
            deadline=self._to_sim(record.deadline),
            created_at=created if created is not None and created >= 0 else self.now,
            required_stations=list(record.required_stations),
            db_priority=record.priority,
            status=OrderStatus.IN_PROGRESS if record.completed else OrderStatus.PENDING,
        )
        self.orders[order.order_id] = order
        if order.priority == "critical":
            order.detected_at = self.now
            self._timeline(TimelineKind.EMERGENCY, f"긴급 주문 {order.order_id} ({order.product} {order.quantity}개)")
            self._alerts.append(
                EngineAlert(ProductionEventType.CRITICAL_ORDER, order.order_id, self._order_payload(order)))
        else:
            self._timeline(TimelineKind.INFO, f"주문 등록 {order.order_id} ({order.product} {order.quantity}개)")

    def _set_order_priority(self, order: OrderProgress, priority: str) -> None:
        """주문 우선순위를 바꾸고 대기 작업에도 반영한다."""
        order.priority = priority
        for task in self.pending_tasks:
            if task.order_id == order.order_id:
                task.priority = order.level

    def _cancel_order(self, order: OrderProgress) -> None:
        """주문 취소 처리."""
        order.status = OrderStatus.CANCELLED
        order.finished_at = self.now
        before = len(self.pending_tasks)
        self.pending_tasks = [t for t in self.pending_tasks if t.order_id != order.order_id]
        order.in_flight = max(0, order.in_flight - (before - len(self.pending_tasks)))
        self._release_emergency(order.order_id)
        self._timeline(TimelineKind.INFO, f"주문 취소 {order.order_id} (잔량 {order.remaining}개 해제)")

    def _finish_order(self, order: OrderProgress) -> None:
        """주문 완료 처리."""
        order.status = OrderStatus.COMPLETED
        order.finished_at = self.now
        on_time = order.deadline is None or self.now <= order.deadline
        self.metrics.record_order_finished(on_time)
        self.pending_tasks = [t for t in self.pending_tasks if t.order_id != order.order_id]
        order.in_flight = 0
        self._release_emergency(order.order_id)
        verdict = "납기 내" if on_time else "납기 초과"
        self._timeline(TimelineKind.SUCCESS, f"주문 완료 {order.order_id} ({verdict})")
        payload = self._order_payload(order)
        payload["on_time"] = on_time
        self._alerts.append(EngineAlert(ProductionEventType.ORDER_COMPLETED, order.order_id, payload))

    def _release_emergency(self, order_id: str) -> None:
        """주문 종료 시 긴급 배정 로봇·전용 스테이션을 해제한다."""
        for robot in self.robots.values():
            if robot.emergency_order == order_id:
                robot.emergency_order = None
        for station in self.stations.values():
            if station.dedicated_order == order_id:
                station.dedicated_order = None

    def order_stations(self, order: OrderProgress) -> list[str]:
        """주문 작업을 처리할 스테이션 (전용 지정 반영, 필수 스테이션 전부 고장 시 대체).

        Args:
            order: 주문

        Returns:
            스테이션 ID 리스트
        """
        available = [sid for sid in self.stations if self.station_available(sid)]
        dedicated_here = [sid for sid in available if self.stations[sid].dedicated_order == order.order_id]
        free = [sid for sid in available if self.stations[sid].dedicated_order is None]
        if order.required_stations:
            chosen = [sid for sid in order.required_stations if sid in free or sid in dedicated_here]
            if not chosen and not any(self.station_available(s) for s in order.required_stations):
                types = {self.stations[s].cfg.task_type for s in order.required_stations if s in self.stations}
                chosen = [sid for sid in free if self.stations[sid].cfg.task_type in types] or free
        else:
            chosen = free
        return list(dict.fromkeys([*dedicated_here, *chosen]))

    def release_order_tasks(self) -> None:
        """주문별로 파이프라인 한도까지 작업을 릴리즈한다."""
        limit = config.ORDER_PIPELINE_PER_ROBOT * max(1, len(self.robots))
        active = sorted((o for o in self.orders.values() if o.is_active), key=lambda o: (o.level, o.created_at))
        for order in active:
            need = order.quantity - order.completed - order.in_flight
            if need < 0:
                self._trim_order_tasks(order, -need)
                continue
            stations = self.order_stations(order)
            if not stations:
                continue
            for task in self.pending_tasks:
                if task.order_id == order.order_id:
                    task.allowed_stations = tuple(stations)
            loads = self.pending_count_by_station()
            for _ in range(max(0, min(need, limit - order.in_flight))):
                station_id = min(stations, key=lambda s: loads.get(s, 0) + self.stations[s].waiting)
                loads[station_id] = loads.get(station_id, 0) + 1
                self.add_task(order.product, station_id, order.level, tuple(stations), order.order_id, order.deadline)
                order.in_flight += 1
            if order.in_flight and order.status == OrderStatus.PENDING:
                order.status = OrderStatus.IN_PROGRESS

    def _trim_order_tasks(self, order: OrderProgress, excess: int) -> None:
        """수량 감소로 초과 릴리즈된 대기 작업을 제거한다."""
        kept: list[Task] = []
        for task in reversed(self.pending_tasks):
            if excess > 0 and task.order_id == order.order_id:
                excess -= 1
                order.in_flight -= 1
                continue
            kept.append(task)
        kept.reverse()
        self.pending_tasks = kept

    def order_eta(self, order: OrderProgress) -> float:
        """주문 잔량 예상 소요시간 (sim-sec).

        로봇 처리율(로봇 수 / 평균 사이클)과 스테이션 처리율(스테이션 수 / 평균 처리시간) 중 작은 값을 쓰며,
        같은 우선순위 이상의 다른 주문과 로봇을 나눠 쓴다고 가정한다.

        Args:
            order: 주문

        Returns:
            예상 소요시간 (처리 불가 시 inf)
        """
        if order.remaining == 0:
            return 0.0
        stations = self.order_stations(order)
        robots = [r for r in self.robots.values() if r.state != RobotState.FAULT]
        if not stations or not robots:
            return math.inf
        competing = sum(1 for o in self.orders.values() if o.is_active and o.level <= order.level)
        cycle = self.metrics.avg_cycle_time()
        robot_rate = len(robots) / cycle / max(1, competing)
        dedicated = sum(1 for r in robots if r.emergency_order == order.order_id)
        robot_rate = max(robot_rate, dedicated / cycle)
        avg_process = sum(self.stations[s].cfg.avg_process_time for s in stations) / len(stations)
        station_rate = len(stations) / max(0.1, avg_process)
        return order.remaining / max(1e-6, min(robot_rate, station_rate))

    def _order_payload(self, order: OrderProgress) -> dict[str, Any]:
        """이벤트 payload용 주문 요약."""
        left = order.deadline - self.now if order.deadline is not None else None
        return {
            "order_id": order.order_id,
            "product_type": order.product,
            "quantity": order.quantity,
            "completed": order.completed,
            "remaining": order.remaining,
            "priority": order.priority,
            "deadline_left": left,
            "eta_seconds": self.order_eta(order) if order.is_active else 0.0,
            "required_stations": list(order.required_stations),
        }

    def apply_emergency_plan(self, plan: dict[str, Any]) -> list[str]:
        """AI 긴급 대응 계획을 실행한다.

        Args:
            plan: order_id, boost_priority, dedicate, assign, recall, preempt, min_charge_mode

        Returns:
            실제 수행된 조치 설명
        """
        order_id: str | None = plan.get("order_id")
        order = self.orders.get(order_id) if order_id else None
        if order_id and order is None:
            return [f"주문 {order_id}을(를) 시뮬레이션에서 찾을 수 없습니다."]
        results: list[str] = []
        if order is not None:
            boost = plan.get("boost_priority")
            if boost in config.PRIORITY_LEVELS and config.PRIORITY_LEVELS[boost] < order.level:
                self._set_order_priority(order, boost)
                results.append(f"{order_id} 우선순위 → {boost}")
            dedicated = []
            for station_id in plan.get("dedicate", []):
                station = self.stations.get(station_id)
                if station is not None and station.active:
                    station.dedicated_order = order_id
                    dedicated.append(station_id)
            if dedicated:
                results.append(f"{', '.join(dedicated)} → {order.product} 전용 전환")
            self.release_order_tasks()

        def order_task() -> Task | None:
            return self.take_task(lambda t: t.order_id == order_id) if order_id else None

        for robot_id in plan.get("assign", []):
            robot = self.robots.get(robot_id)
            if robot is not None:
                robot.emergency_order = order_id
                results.append(f"{robot_id} 유휴 → 긴급 배정")
        for robot_id in plan.get("recall", []):
            robot = self.robots.get(robot_id)
            if robot is None:
                continue
            robot.emergency_order = order_id
            task = order_task()
            if self.recall(robot, task):
                results.append(f"{robot_id} 충전 중단 → 긴급 복귀")
            elif task is not None:
                self.pending_tasks.append(task)
        for robot_id in plan.get("preempt", []):
            robot = self.robots.get(robot_id)
            if robot is None:
                continue
            robot.emergency_order = order_id
            task = order_task()
            if task is not None and self.preempt(robot, task):
                results.append(f"{robot_id} 작업 선점 → 긴급 작업 전환")
            elif task is None and robot.interrupt(InterruptKind.PREEMPT):
                results.append(f"{robot_id} 작업 선점 → 긴급 대기")
        if "min_charge_mode" in plan:
            self.update_param("min_charge_mode", float(bool(plan["min_charge_mode"])))
            results.append("최소충전 복귀 모드 " + ("ON" if plan["min_charge_mode"] else "OFF"))
        if results:
            self._timeline(TimelineKind.EMERGENCY, "AI 대응: " + " / ".join(results))
        return results

    # ------------------------------------------------------------------
    # 설정 핫리로드
    # ------------------------------------------------------------------
    def apply_config(self, factory: FactoryConfig) -> list[str]:
        """새 공장 구성을 무중단 반영한다.

        - 설비 추가/삭제: 엔티티 생성/제거 (삭제·고장 스테이션의 작업은 대체 스테이션으로 재배정)
        - 위치/경로 변경: 경로 그래프 재계산 후 이동 중 로봇 재경로
        - 로봇 추가/삭제: Robot 프로세스 생성/종료
        - 파라미터 변경: 즉시 반영

        Args:
            factory: 새 구성

        Returns:
            변경 요약 문장 리스트
        """
        old = self.factory
        summary: list[str] = []
        geometry_changed = (
            old.obstacles != factory.obstacles or old.nodes != factory.nodes or old.edges != factory.edges
            or old.profile != factory.profile
            or [(s.id, s.x, s.y) for s in old.stations] != [(s.id, s.x, s.y) for s in factory.stations]
            or [(c.id, c.x, c.y) for c in old.chargers] != [(c.id, c.x, c.y) for c in factory.chargers]
        )
        self.factory = factory
        self.path_graph = PathGraph(factory)
        self.layout_version += 1

        summary += self._apply_station_configs(factory.stations)
        summary += self._apply_charger_configs(factory.chargers)
        summary += self._apply_robot_configs(factory.robots)
        if geometry_changed:
            self._reroute_moving_robots()
            summary.append("경로 그래프 재계산")
        if summary:
            self._timeline(TimelineKind.CONFIG, "설정 반영: " + ", ".join(summary))
        return summary

    def _apply_station_configs(self, configs: list[WorkStationConfig]) -> list[str]:
        """스테이션 diff 반영."""
        summary: list[str] = []
        new_ids = {c.id for c in configs}
        for station_id in [sid for sid in self.stations if sid not in new_ids]:
            station = self.stations.pop(station_id)
            station.removed = True
            self._disable_station(station)
            summary.append(f"{station_id} 제거")
        for cfg in configs:
            station = self.stations.get(cfg.id)
            if station is None:
                self._create_station(cfg)
                summary.append(f"{cfg.id} 추가")
                continue
            if station.cfg == cfg:
                continue
            was_active = station.active
            station.cfg = cfg
            if was_active and not station.active:
                self._disable_station(station)
                summary.append(f"{cfg.id} 비활성(고장)")
                self._timeline(TimelineKind.FAILURE, f"설비 고장 {cfg.id} ({cfg.label})")
                self._alerts.append(
                    EngineAlert(ProductionEventType.EQUIPMENT_FAILURE, None, self._station_payload(cfg)))
            elif not was_active and station.active:
                summary.append(f"{cfg.id} 재가동")
                self._timeline(TimelineKind.SUCCESS, f"설비 복구 {cfg.id} ({cfg.label})")
                self._alerts.append(
                    EngineAlert(ProductionEventType.EQUIPMENT_RESTORED, None, self._station_payload(cfg)))
            else:
                summary.append(f"{cfg.id} 수정")
        return summary

    def _station_payload(self, cfg: WorkStationConfig) -> dict[str, Any]:
        """설비 이벤트 payload."""
        affected = [
            o.order_id for o in self.orders.values()
            if o.is_active and (not o.required_stations or cfg.id in o.required_stations)
        ]
        alternatives = [
            sid for sid, s in self.stations.items()
            if sid != cfg.id and self.station_available(sid) and s.cfg.task_type == cfg.task_type
        ]
        return {
            "station_id": cfg.id,
            "label": cfg.label,
            "task_type": cfg.task_type,
            "affected_orders": affected,
            "alternatives": alternatives,
        }

    def _disable_station(self, station: WorkStation) -> None:
        """고장/삭제된 스테이션의 작업을 정리한다."""
        station.dedicated_order = None
        for robot in self.robots.values():
            task = robot.current_task
            if task is not None and task.station_id == station.id:
                robot.interrupt(InterruptKind.STATION_DOWN)
        for task in self.pending_tasks:
            if task.station_id == station.id:
                self._rehome_task(task)

    def _apply_charger_configs(self, configs: list[ChargingStationConfig]) -> list[str]:
        """충전소 diff 반영."""
        summary: list[str] = []
        new_ids = {c.id for c in configs}
        for charger_id in [cid for cid in self.chargers if cid not in new_ids]:
            charger = self.chargers.pop(charger_id)
            charger.removed = True
            self._evacuate_charger(charger_id)
            summary.append(f"{charger_id} 제거")
        for cfg in configs:
            charger = self.chargers.get(cfg.id)
            if charger is None:
                self._create_charger(cfg)
                summary.append(f"{cfg.id} 추가")
            elif charger.cfg != cfg:
                was_active = charger.active
                charger.update_config(cfg)
                if was_active and not charger.active:
                    self._evacuate_charger(cfg.id)
                summary.append(f"{cfg.id} 수정")
        return summary

    def _evacuate_charger(self, charger_id: str) -> None:
        """사용 불가 충전소로 가거나 충전 중인 로봇을 다른 충전소로 보낸다."""
        for robot in self.robots.values():
            if robot.target_id == charger_id:
                robot.interrupt(InterruptKind.CHARGE)

    def _apply_robot_configs(self, configs: list[RobotConfig]) -> list[str]:
        """로봇 diff 반영."""
        summary: list[str] = []
        wanted = {c.id: c for c in configs if c.is_active}
        for robot_id in [rid for rid in self.robots if rid not in wanted]:
            robot = self.robots.pop(robot_id)
            robot.interrupt(InterruptKind.RETIRE)
            self.retired.append(robot)
            summary.append(f"{robot_id} 퇴역")
        for robot_id, cfg in wanted.items():
            robot = self.robots.get(robot_id)
            if robot is None:
                self._create_robot(cfg)
                summary.append(f"{robot_id} 투입")
            elif robot.update_config(cfg):
                summary.append(f"{robot_id} 파라미터 변경")
        return summary

    def _reroute_moving_robots(self) -> None:
        """경로망 변경 후 이동 중인 로봇의 경로를 다시 계산한다."""
        for robot in self.robots.values():
            if robot.state != RobotState.MOVING:
                continue
            if robot.current_task is not None:
                task = robot.current_task
                robot.interrupt(InterruptKind.PREEMPT, task)
            elif robot.target_id in self.chargers:
                robot.interrupt(InterruptKind.CHARGE)

    # ------------------------------------------------------------------
    # SimPy 프로세스
    # ------------------------------------------------------------------
    def _dispatch_process(self) -> Generator[Any, Any, None]:
        """주기적으로 스케줄러를 호출한다."""
        while True:
            self.scheduler.dispatch(self)
            yield self.env.timeout(config.DISPATCH_INTERVAL)

    def _monitor_process(self) -> Generator[Any, Any, None]:
        """납기 임박 / 생산량 급증을 감시한다."""
        while True:
            yield self.env.timeout(config.MONITOR_INTERVAL)
            self._check_deadlines()
            self._check_surge()

    def _check_deadlines(self) -> None:
        """납기 임박 주문을 감지한다 (남은시간 < 예상소요 × 1.3)."""
        for order in self.orders.values():
            if not order.is_active or order.deadline is None or order.risk_flagged:
                continue
            left = order.deadline - self.now
            eta = self.order_eta(order)
            if left < eta * config.DEADLINE_RISK_FACTOR:
                order.risk_flagged = True
                self._timeline(TimelineKind.EMERGENCY, f"납기 임박 {order.order_id} (남은 {left / 60:.1f}분)")
                self._alerts.append(EngineAlert(ProductionEventType.DEADLINE_RISK, order.order_id,
                                                self._order_payload(order)))

    def _check_surge(self) -> None:
        """활성 주문 잔량 합계가 임계치를 넘으면 생산량 급증으로 판단한다."""
        total = sum(o.remaining for o in self.orders.values() if o.is_active)
        threshold = config.SURGE_UNITS_PER_ROBOT * max(1, len(self.robots))
        if total > threshold and not self._surge_flagged:
            self._surge_flagged = True
            self._timeline(TimelineKind.EMERGENCY, f"생산량 급증 (잔량 {total}개 > 임계 {threshold}개)")
            self._alerts.append(EngineAlert(ProductionEventType.DEMAND_SURGE, None, {
                "total_units": total,
                "threshold": threshold,
                "robots": len(self.robots),
                "active_orders": [o.order_id for o in self.orders.values() if o.is_active],
            }))
        elif self._surge_flagged and total < threshold * SURGE_RESET_RATIO:
            self._surge_flagged = False

    # ------------------------------------------------------------------
    # 알림 / 타임라인
    # ------------------------------------------------------------------
    def _timeline(self, kind: str, message: str) -> None:
        """이벤트 타임라인에 항목을 추가한다."""
        self.timeline.append(TimelineEvent(self.now, kind, message))

    def _notify(self, severity: str, message: str) -> None:
        """관제 콘솔 시스템 알림을 쌓는다."""
        self._notices.append((severity, message))

    def drain_alerts(self) -> list[EngineAlert]:
        """감지된 생산 이벤트를 꺼낸다.

        Returns:
            EngineAlert 리스트
        """
        alerts, self._alerts = self._alerts, []
        return alerts

    def drain_notices(self) -> list[tuple[str, str]]:
        """쌓인 시스템 알림을 꺼낸다.

        Returns:
            (severity, message) 리스트
        """
        notices, self._notices = self._notices, []
        return notices

    # ------------------------------------------------------------------
    # 스냅샷
    # ------------------------------------------------------------------
    def kpis(self) -> dict[str, float]:
        """현재 KPI.

        Returns:
            KPI key → 값
        """
        usages = [r.usage() for r in (*self.robots.values(), *self.retired)]
        return self.metrics.compute(self.now, usages)

    def robot_snapshots(self) -> list[RobotSnapshot]:
        """로봇 스냅샷.

        Returns:
            RobotSnapshot 리스트
        """
        snapshots = []
        for robot in self.robots.values():
            x, y = robot.position()
            task = robot.current_task
            snapshots.append(RobotSnapshot(
                id=robot.robot_id,
                x=x,
                y=y,
                state=robot.state,
                battery_pct=round(robot.battery_pct, 1),
                color_index=robot.color_index,
                task_id=task.task_id if task else None,
                task_priority=task.priority if task else None,
                order_id=task.order_id if task else None,
                target_id=robot.target_id,
                emergency_order=robot.emergency_order,
                route=robot.remaining_route(),
                tasks_done=robot.tasks_done,
                robot_type=robot.cfg.robot_type,
            ))
        return snapshots

    def station_snapshots(self) -> list[StationSnapshot]:
        """스테이션 스냅샷.

        Returns:
            StationSnapshot 리스트
        """
        pending = self.pending_count_by_station()
        snapshots = []
        for station in self.stations.values():
            cfg = station.cfg
            task = station.current_task
            snapshots.append(StationSnapshot(
                id=cfg.id,
                label=cfg.label,
                x=cfg.x,
                y=cfg.y,
                width=cfg.width,
                height=cfg.height,
                task_type=cfg.task_type,
                active=station.active,
                queue_length=station.waiting,
                pending_tasks=pending.get(cfg.id, 0),
                busy=task is not None,
                current_order=task.order_id if task else None,
                current_robot=station.current_robot,
                processed=station.processed,
                utilization=round(station.utilization(), 1),
                dedicated_order=station.dedicated_order,
            ))
        return snapshots

    def charger_snapshots(self) -> list[ChargerSnapshot]:
        """충전소 스냅샷.

        Returns:
            ChargerSnapshot 리스트
        """
        return [
            ChargerSnapshot(
                id=c.id, x=c.cfg.x, y=c.cfg.y, capacity=c.capacity, occupied=len(c.occupants),
                queue_length=c.waiting, active=c.active, charge_rate=float(c.cfg.charge_rate),
            )
            for c in self.chargers.values()
        ]

    def order_snapshots(self) -> list[OrderSnapshot]:
        """주문 스냅샷 (진행 중 + 최근 종료).

        Returns:
            OrderSnapshot 리스트
        """
        active = sorted((o for o in self.orders.values() if o.is_active), key=lambda o: (o.level, o.created_at))
        finished = sorted(
            (o for o in self.orders.values() if not o.is_active),
            key=lambda o: o.finished_at or 0.0, reverse=True,
        )
        snapshots = []
        for order in [*active, *finished][:ORDER_SNAPSHOT_LIMIT]:
            left = order.deadline - self.now if order.deadline is not None else None
            eta = self.order_eta(order) if order.is_active else 0.0
            end = order.finished_at if order.finished_at is not None else self.now
            snapshots.append(OrderSnapshot(
                order_id=order.order_id,
                product_type=order.product,
                quantity=order.quantity,
                completed=order.completed,
                priority=order.priority,
                status=order.status,
                deadline_remaining=left,
                eta_seconds=eta,
                at_risk=order.is_active and left is not None and left < eta * config.DEADLINE_RISK_FACTOR,
                elapsed=max(0.0, end - order.created_at),
                dedicated_stations=[s.id for s in self.stations.values() if s.dedicated_order == order.order_id],
            ))
        return snapshots


def run_headless(duration: float = 1800.0) -> dict[str, dict[str, float]]:
    """GUI 없이 기본 공장 구성으로 두 스케줄러를 실행해 KPI를 비교한다.

    Args:
        duration: 시뮬레이션 시간 (sim-sec)

    Returns:
        mode → KPI
    """
    from database.seed_data import build_default_config

    factory = build_default_config()
    epoch = datetime.now()
    critical = OrderRecord(
        "ORD-TEST-0001", "제품B", 60, priority="critical", required_stations=["WS-02", "WS-05"],
        deadline=epoch + timedelta(seconds=900), created_at=epoch + timedelta(seconds=300),
    )
    results: dict[str, dict[str, float]] = {}
    for mode in config.SCHEDULER_MODES:
        engine = SimulationEngine(factory, mode, epoch=epoch)
        engine.env.run(until=300)
        engine.sync_orders([critical])
        engine.env.run(until=duration)
        results[mode] = engine.kpis()
        order = engine.orders[critical.order_id]
        print(f"[{mode}] done={engine.metrics.total_completed} order={order.completed}/{order.quantity} "
              f"status={order.status} timeline={len(engine.timeline)}")
    return results


if __name__ == "__main__":
    for engine_mode, values in run_headless().items():
        print(engine_mode, {k: round(v, 2) for k, v in values.items()})
