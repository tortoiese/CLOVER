"""Robot — SimPy 로봇 프로세스.

루프: (방전 복구) → (충전 필요 시 충전) → (작업 없으면 대기) → 작업 수행.
외부 개입(선점, 긴급 복귀, 강제 충전, 퇴역, 설비 고장)은 simpy.Interrupt로 전달된다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import simpy

import config
from utils.config_models import RobotConfig
from utils.event_types import RobotState
from utils.metrics import RobotTimeUsage

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.task_generator import Task

Point = tuple[float, float]


class BatteryDepleted(Exception):
    """이동 중 배터리가 0이 되었음을 알린다."""


class InterruptKind:
    """Robot.interrupt()의 cause 종류."""

    PREEMPT = "preempt"
    RECALL = "recall"
    CHARGE = "charge"
    RETIRE = "retire"
    STATION_DOWN = "station_down"


class Robot:
    """AGV/AMR 로봇 1대."""

    def __init__(self, engine: SimulationEngine, cfg: RobotConfig, position: Point, color_index: int) -> None:
        """로봇을 생성하고 프로세스를 등록한다.

        Args:
            engine: 소속 엔진
            cfg: 로봇 설정
            position: 초기 위치
            color_index: 고유 색상 인덱스
        """
        self.engine = engine
        self.env: simpy.Environment = engine.env
        self.cfg = cfg
        self.color_index = color_index
        self.x, self.y = position
        self.battery = float(cfg.battery_capacity)
        self.state = RobotState.IDLE
        self.state_since = self.env.now
        self.state_time: dict[str, float] = defaultdict(float)
        self.current_task: Task | None = None
        self.target_id: str | None = None
        self.route: list[Point] = []
        self.emergency_order: str | None = None
        self.forced_charge = False
        self.alive = True
        self.charge_sessions = 0
        self.tasks_done = 0
        self._depleted = False
        self._segment: tuple[float, float, float, float, float, float, float] | None = None
        self._charge_step: tuple[float, float] | None = None
        self._wake: simpy.Event | None = None
        self.process = self.env.process(self.run_process())

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    @property
    def robot_id(self) -> str:
        """로봇 ID."""
        return self.cfg.id

    @property
    def capacity(self) -> float:
        """배터리 용량."""
        return max(1e-6, float(self.cfg.battery_capacity))

    @property
    def battery_pct(self) -> float:
        """배터리 잔량 (%)."""
        return max(0.0, min(100.0, self.live_battery() / self.capacity * 100.0))

    def live_battery(self) -> float:
        """이동/충전 진행분까지 반영한 현재 배터리량.

        Returns:
            배터리량
        """
        if self._segment is not None:
            *_, t0, t1, drain = self._segment
            return max(0.0, self.battery - drain * self._segment_fraction(t0, t1))
        if self._charge_step is not None:
            start, rate = self._charge_step
            return min(self.capacity, self.battery + rate * (self.env.now - start))
        return self.battery

    def position(self) -> Point:
        """현재 위치 (이동 중이면 보간).

        Returns:
            (x, y)
        """
        if self._segment is None:
            return self.x, self.y
        x0, y0, x1, y1, t0, t1, _ = self._segment
        f = self._segment_fraction(t0, t1)
        return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f

    def remaining_route(self) -> list[Point]:
        """남은 이동 경로 (현재 위치 포함).

        Returns:
            좌표 리스트
        """
        if not self.route:
            return []
        return [self.position(), *self.route]

    def is_available(self) -> bool:
        """새 작업을 배정받을 수 있는지 여부.

        Returns:
            배정 가능 여부
        """
        return (
            self.alive
            and self.state == RobotState.IDLE
            and self._wake is not None
            and self.current_task is None
            and not self.forced_charge
            and not self._depleted
        )

    def usage(self) -> RobotTimeUsage:
        """KPI 계산용 시간 사용량 (현재 상태 진행분 포함).

        Returns:
            RobotTimeUsage
        """
        times = dict(self.state_time)
        times[self.state] = times.get(self.state, 0.0) + (self.env.now - self.state_since)
        return RobotTimeUsage(times, self.charge_sessions, self.tasks_done)

    # ------------------------------------------------------------------
    # 외부 제어
    # ------------------------------------------------------------------
    def assign(self, task: Task) -> None:
        """작업을 배정하고 대기 중이면 깨운다.

        Args:
            task: 배정할 작업
        """
        self.current_task = task
        task.robot_id = self.robot_id
        self.wake()

    def wake(self) -> None:
        """대기(IDLE) 중인 프로세스를 깨운다."""
        if self._wake is not None and not self._wake.triggered:
            self._wake.succeed()

    def interrupt(self, kind: str, task: Task | None = None) -> bool:
        """프로세스에 인터럽트를 보낸다.

        Args:
            kind: InterruptKind.*
            task: 인터럽트 처리 후 바로 수행할 작업 (선택)

        Returns:
            전달 여부
        """
        if not self.process.is_alive or self.env.active_process is self.process:
            return False
        self.process.interrupt({"kind": kind, "task": task})
        return True

    def update_config(self, cfg: RobotConfig) -> bool:
        """설정을 갱신한다. 배터리 잔량 비율은 유지한다.

        Args:
            cfg: 새 설정

        Returns:
            변경 여부
        """
        if cfg == self.cfg:
            return False
        ratio = self.battery / self.capacity
        self.cfg = cfg
        self.battery = ratio * self.capacity
        return True

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------
    def _segment_fraction(self, t0: float, t1: float) -> float:
        """현재 구간 진행률 (0~1)."""
        if t1 <= t0:
            return 1.0
        return max(0.0, min(1.0, (self.env.now - t0) / (t1 - t0)))

    def _set_state(self, state: str) -> None:
        """상태를 바꾸고 이전 상태의 체류 시간을 누적한다."""
        now = self.env.now
        self.state_time[self.state] += now - self.state_since
        self.state = state
        self.state_since = now

    def _apply_segment(self, fraction: float) -> None:
        """이동 구간 진행분을 위치/배터리에 반영한다."""
        if self._segment is None:
            return
        x0, y0, x1, y1, _, _, drain = self._segment
        self.x = x0 + (x1 - x0) * fraction
        self.y = y0 + (y1 - y0) * fraction
        self.battery = max(0.0, self.battery - drain * fraction)
        self._segment = None

    def _settle(self) -> None:
        """인터럽트 시 진행 중이던 이동/충전을 정산한다."""
        if self._segment is not None:
            *_, t0, t1, _ = self._segment
            self._apply_segment(self._segment_fraction(t0, t1))
        if self._charge_step is not None:
            start, rate = self._charge_step
            self.battery = min(self.capacity, self.battery + rate * (self.env.now - start))
            self._charge_step = None
        self.route = []

    def _requeue_current(self) -> None:
        """현재 작업을 엔진 대기열로 돌려보낸다."""
        if self.current_task is not None:
            self.engine.requeue_task(self.current_task)
            self.current_task = None
        self.target_id = None

    # ------------------------------------------------------------------
    # SimPy 프로세스
    # ------------------------------------------------------------------
    def run_process(self) -> Generator[Any, Any, None]:
        """로봇 메인 프로세스."""
        while self.alive:
            try:
                if self._depleted:
                    yield from self._fault_process()
                    continue
                if self.forced_charge or (
                    self.current_task is None and self.engine.scheduler.should_charge(self, self.engine)
                ):
                    yield from self._charge_process()
                    continue
                if self.current_task is None:
                    self._set_state(RobotState.IDLE)
                    self._wake = self.env.event()
                    yield self._wake
                    self._wake = None
                    continue
                yield from self._task_process()
            except simpy.Interrupt as interrupt:
                self._wake = None
                self._settle()
                self._handle_interrupt(interrupt.cause)
            except BatteryDepleted:
                self._depleted = True
        self._set_state(RobotState.IDLE)

    def _handle_interrupt(self, cause: Any) -> None:
        """인터럽트 원인별 처리."""
        kind = cause.get("kind") if isinstance(cause, dict) else str(cause)
        task = cause.get("task") if isinstance(cause, dict) else None
        if kind == InterruptKind.RETIRE:
            self.alive = False
            self._requeue_current()
            return
        if kind == InterruptKind.CHARGE:
            self._requeue_current()
            self.forced_charge = True
        elif kind == InterruptKind.RECALL:
            self.forced_charge = False
            self.target_id = None
        elif kind in (InterruptKind.PREEMPT, InterruptKind.STATION_DOWN):
            self._requeue_current()
        if task is not None:
            self.engine.assign(task, self)

    def _travel_process(self, points: list[Point]) -> Generator[Any, Any, None]:
        """경유점을 따라 이동한다. 배터리가 바닥나면 BatteryDepleted."""
        self.route = list(points)
        while self.route:
            tx, ty = self.route[0]
            dist_px = math.hypot(tx - self.x, ty - self.y)
            if dist_px < 1e-6:
                self.route.pop(0)
                continue
            speed_px = max(0.05, float(self.cfg.move_speed)) / config.METERS_PER_PIXEL
            duration = dist_px / speed_px
            drain = dist_px * config.METERS_PER_PIXEL * float(self.cfg.battery_drain_rate)
            fraction = 1.0 if drain <= self.battery else max(0.0, self.battery / drain)
            now = self.env.now
            self._segment = (self.x, self.y, tx, ty, now, now + duration, drain)
            yield self.env.timeout(duration * fraction)
            self._apply_segment(fraction)
            if fraction < 1.0:
                self.route = []
                raise BatteryDepleted()
            self.route.pop(0)

    def _task_process(self) -> Generator[Any, Any, None]:
        """배정된 작업 1건을 수행한다: 이동 → 대기열 → 작업."""
        task = self.current_task
        assert task is not None
        engine = self.engine
        station = engine.stations.get(task.station_id)
        if station is None or not station.active:
            self._requeue_current()
            return
        route = engine.path_graph.route_to_facility(self.x, self.y, station.id)
        if route is None:
            self.current_task = None
            engine.report_unreachable(task, self)
            yield self.env.timeout(1.0)
            return
        self.target_id = station.id
        self._set_state(RobotState.MOVING)
        yield from self._travel_process(route.points)

        self._set_state(RobotState.WAITING)
        with station.resource.request(priority=task.priority) as request:
            yield request
            if not station.active or engine.stations.get(station.id) is not station:
                self._requeue_current()
                return
            self._set_state(RobotState.WORKING)
            engine.on_task_started(task, self)
            station.begin(task, self.robot_id)
            started = self.env.now
            completed = False
            try:
                yield self.env.timeout(station.sample_process_time())
                completed = True
            finally:
                station.end(completed)
                self.battery = max(0.0, self.battery - config.WORK_DRAIN_PER_SEC * (self.env.now - started))
        self.current_task = None
        self.target_id = None
        self.tasks_done += 1
        engine.complete_task(task, self)

    def _charge_process(self) -> Generator[Any, Any, None]:
        """충전소로 이동하여 목표 잔량까지 충전한다."""
        engine = self.engine
        charger = engine.scheduler.choose_charger(self, engine)
        route = engine.path_graph.route_to_facility(self.x, self.y, charger.id) if charger else None
        if charger is None or route is None:
            self.forced_charge = False
            self._set_state(RobotState.IDLE)
            yield self.env.timeout(2.0)
            return
        self.target_id = charger.id
        charger.incoming.add(self.robot_id)
        try:
            self._set_state(RobotState.MOVING)
            yield from self._travel_process(route.points)
        finally:
            charger.incoming.discard(self.robot_id)

        self._set_state(RobotState.CHARGE_WAIT)
        wait_start = self.env.now
        with charger.resource.request() as request:
            yield request
            engine.metrics.record_charge_wait(self.env.now - wait_start)
            self._set_state(RobotState.CHARGING)
            self.charge_sessions += 1
            charger.occupants.add(self.robot_id)
            try:
                while self.alive and charger.active:
                    target = engine.scheduler.charge_target(self, engine) / 100.0 * self.capacity
                    if self.battery >= target - 1e-6:
                        break
                    rate = max(0.01, float(charger.cfg.charge_rate))
                    step = min(config.CHARGE_STEP, (target - self.battery) / rate)
                    self._charge_step = (self.env.now, rate)
                    yield self.env.timeout(step)
                    self._charge_step = None
                    self.battery = min(self.capacity, self.battery + rate * step)
            finally:
                charger.occupants.discard(self.robot_id)
        self.forced_charge = False
        self.target_id = None

    def _fault_process(self) -> Generator[Any, Any, None]:
        """방전 → 구조 대기 → 최소 잔량으로 복구 후 충전소행."""
        self._set_state(RobotState.FAULT)
        self._requeue_current()
        self.engine.on_robot_fault(self)
        yield self.env.timeout(config.FAULT_RECOVERY_TIME)
        self.battery = self.capacity * config.RESCUE_BATTERY_LEVEL / 100.0
        self._depleted = False
        self.forced_charge = True
