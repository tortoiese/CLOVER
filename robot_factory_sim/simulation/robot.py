"""Robot — SimPy 로봇 프로세스.

루프: (방전 복구) → (충전 필요 시 충전) → (작업 없으면 대기) → 작업 수행.
외부 개입(선점, 긴급 복귀, 강제 충전, 퇴역, 설비 고장)은 simpy.Interrupt로 전달된다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Generator
from dataclasses import replace
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import simpy

import config
from simulation.path_graph import Route
from simulation.task_generator import TaskState
from simulation.telemetry import observe_charge, observe_wait
from simulation.traffic_rules import LaneRequest, TrafficBlocked, tail_clearance_seconds
from utils.config_models import RobotConfig
from utils.event_types import RobotState
from utils.metrics import RobotTimeUsage

if TYPE_CHECKING:
    from simulation.elevator import Elevator
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
    REPLAN = "replan"


class Robot:
    """AGV/AMR 로봇 1대."""

    def __init__(
        self,
        engine: SimulationEngine,
        cfg: RobotConfig,
        position: Point,
        color_index: int,
        floor: int = 1,
        *,
        soc_pct: float = 100,
    ) -> None:
        """로봇을 생성하고 프로세스를 등록한다.

        Args:
            engine: 소속 엔진
            cfg: 로봇 설정
            position: 초기 위치
            color_index: 고유 색상 인덱스
            floor: 시작 층
        """
        self.engine = engine
        self.env: simpy.Environment = engine.env
        self.cfg = cfg
        self.color_index = color_index
        self.x, self.y = position
        self.floor = floor
        self.battery = float(cfg.battery_capacity) * soc_pct / 100
        self.initial_energy_units = self.battery
        self.charged_energy_units = 0.0
        self.rescue_energy_units = 0.0
        self.capacity_adjustment_units = 0.0
        self.state = RobotState.IDLE
        self.state_since = self.env.now
        self.state_time: dict[str, float] = defaultdict(float)
        self.current_task: Task | None = None
        self.target_id: str | None = None
        self.route: list[Point] = []
        self.route_floors: list[int] = []
        self.route_lifts: list[str | None] = []
        self.emergency_order: str | None = None
        self.forced_charge = False
        self.alive = True
        self.retired_at: float | None = None
        self.charge_sessions = 0
        self.tasks_done = 0
        self.roam_count = 0
        self.distance_m = 0.0
        self.travel_energy_units = 0.0
        self.work_energy_units = 0.0
        self._work_started: float | None = None
        self._fault_holding = False
        self._depleted = False
        self._segment: tuple[float, float, float, float, float, float, float] | None = None
        self._charge_step: tuple[float, float] | None = None
        self._wake: simpy.Event | None = None
        self._riding: Elevator | None = None
        self.wait_reason = ""
        self.wait_resource = ""
        self.wait_since: float | None = None
        self._traffic_section_active = False
        self._traffic_granted = False
        self._pending_control: dict[str, Any] | None = None
        self._pending_dimensions: tuple[float, float] | None = None
        self.planned_action: Any = None
        self._traffic_request: Any = None
        self._traffic_release_at: float | None = None
        self._planned_charge_until: float | None = None
        self._completed_plan_route_key: Any = None
        self._route_progress_key: Any = None
        self._route_completed_segments = 0
        self._cabin_request: Any = None
        self._cabin_release_at: float | None = None
        self._cabin_destination: int | None = None
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
        if self._work_started is not None:
            return max(0.0, self.battery - self.engine.plant.work_drain_per_sec * (self.env.now - self._work_started))
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

    def remaining_route_floors(self) -> list[int]:
        """remaining_route()의 각 점이 있는 층.

        Returns:
            층 번호 리스트 (remaining_route와 같은 길이)
        """
        if not self.route:
            return []
        return [self.floor, *self.route_floors]

    def floor_position(self) -> float:
        """현재 층 (엘리베이터 탑승 중이면 캐빈 높이를 보간한 소수 층).

        Returns:
            층 번호
        """
        return self._riding.floor_position() if self._riding is not None else float(self.floor)

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
            and self.planned_action is None
        )

    def usage(self) -> RobotTimeUsage:
        """KPI 계산용 시간 사용량 (현재 상태 진행분 포함).

        Returns:
            RobotTimeUsage
        """
        times = dict(self.state_time)
        now = self.env.now if self.retired_at is None else self.retired_at
        times[self.state] = times.get(self.state, 0.0) + max(0, now - self.state_since)
        return RobotTimeUsage(times, self.charge_sessions, self.tasks_done)

    # ------------------------------------------------------------------
    # 외부 제어
    # ------------------------------------------------------------------
    def assign(self, task: Task, *, wake: bool = True) -> None:
        """작업을 배정하고 대기 중이면 깨운다.

        Args:
            task: 배정할 작업
        """
        self.current_task = task
        task.robot_id = self.robot_id
        if wake:
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
        if self._traffic_granted or self._riding is not None or self._fault_holding:
            # A control cancellation cannot make an occupied footprint disappear.
            return self._defer_control({"kind": kind, "task": task})
        self.process.interrupt({"kind": kind, "task": task})
        return True

    def update_config(self, cfg: RobotConfig) -> bool:
        """설정을 갱신한다. 배터리 잔량 비율은 유지한다.

        Args:
            cfg: 새 설정

        Returns:
            변경 여부
        """
        if cfg == self.cfg and self._pending_dimensions is None:
            return False
        if self._traffic_section_active:
            self._pending_dimensions = (cfg.width, cfg.length)
            cfg = replace(cfg, width=self.cfg.width, length=self.cfg.length)
        else:
            self._pending_dimensions = None
        ratio = self.battery / self.capacity
        previous_battery = self.battery
        self.cfg = cfg
        self.battery = ratio * self.capacity
        self.capacity_adjustment_units += self.battery - previous_battery
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

    def _plan_route_key(self) -> Any:
        if self.planned_action is None:
            return None
        result, action = self.planned_action
        snap = result.input.snapshot
        return snap.epoch, snap.token, snap.now, action.arc

    def _mark_planned_route_complete(self) -> None:
        self._completed_plan_route_key = self._plan_route_key()

    def planned_route_completed(self) -> bool:
        """Confirm actual route completion for this exact committed prefix."""
        key = self._plan_route_key()
        return key is not None and key == self._completed_plan_route_key

    def planned_route_progress(self) -> int:
        """Return fully released segments for this exact committed route only."""
        key = self._plan_route_key()
        return self._route_completed_segments if key is not None and key == self._route_progress_key else 0

    def _apply_segment(self, fraction: float) -> None:
        """이동 구간 진행분을 위치/배터리에 반영한다."""
        if self._segment is None:
            return
        x0, y0, x1, y1, _, _, drain = self._segment
        self.x = x0 + (x1 - x0) * fraction
        self.y = y0 + (y1 - y0) * fraction
        self.distance_m += math.hypot(x1 - x0, y1 - y0) * config.METERS_PER_PIXEL * fraction
        self.travel_energy_units += min(self.battery, drain * fraction)
        self.battery = max(0.0, self.battery - drain * fraction)
        self._segment = None

    def _settle(self) -> None:
        """인터럽트 시 진행 중이던 이동/충전을 정산한다."""
        if self._segment is not None:
            *_, t0, t1, _ = self._segment
            self._apply_segment(self._segment_fraction(t0, t1))
        self._settle_charge()
        self._settle_work()
        self._clear_route()

    def _settle_work(self) -> None:
        if self._work_started is not None:
            consumed = min(self.battery, self.engine.plant.work_drain_per_sec * (self.env.now - self._work_started))
            self.work_energy_units += consumed
            self.battery = max(0.0, self.battery - consumed)
            self._work_started = None

    def _settle_charge(self) -> None:
        if self._charge_step is not None:
            start, rate = self._charge_step
            gained = min(self.capacity - self.battery, max(0, rate * (self.env.now - start)))
            self.battery += gained
            self.charged_energy_units += gained
            self._charge_step = None

    def _clear_route(self) -> None:
        """남은 이동 경로를 비운다."""
        self.route = []
        self.route_floors = []
        self.route_lifts = []

    def _pop_route(self) -> None:
        """다음 경유점에 도착했을 때 경로의 첫 점을 지운다."""
        self.route.pop(0)
        self.route_floors.pop(0)
        self.route_lifts.pop(0)

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
                if self.planned_action is not None:
                    from simulation.planning.execution import execute

                    yield from execute(self)
                    continue
                if self._depleted:
                    yield from self._fault_process()
                    continue
                if hasattr(self.engine.scheduler, "idle_charge_process") and self.current_task is None:
                    self._set_state(RobotState.IDLE)
                    self._wake = self.env.event()
                    charging = yield from self.engine.scheduler.idle_charge_process(self)
                    self._wake = None
                    if self.planned_action is not None:
                        continue
                    if charging and self.current_task is None:
                        try:
                            yield from self._charge_process()
                        finally:
                            self.engine.scheduler.charge_finished(self)
                        continue
                    self.forced_charge = False
                elif self.forced_charge or (
                    self.current_task is None and self.engine.scheduler.should_charge(self, self.engine)
                ):
                    yield from self._charge_process()
                    continue
                if self.current_task is None:
                    self._set_state(RobotState.IDLE)
                    self._wake = self.env.event()
                    if self.engine.roam and self.engine.factory.floor_count > 1:
                        wake = self._wake
                        yield wake | self.env.timeout(config.ROAM_IDLE_SEC)
                        self._wake = None
                        if not wake.triggered:
                            yield from self._roam_process()
                        continue
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
            except TrafficBlocked:
                self._settle()
                if self.current_task is not None:
                    task = self.current_task
                    self.current_task = None
                    self.engine.report_unreachable(task, self)
                self._set_state(RobotState.IDLE)
                yield self.env.timeout(config.DISPATCH_INTERVAL)
        self._set_state(RobotState.IDLE)

    def _handle_interrupt(self, cause: Any) -> None:
        """인터럽트 원인별 처리."""
        self.planned_action = None
        kind = cause.get("kind") if isinstance(cause, dict) else str(cause)
        task = cause.get("task") if isinstance(cause, dict) else None
        if kind == InterruptKind.RETIRE:
            self.alive = False
            self.retired_at = self.env.now
            self._requeue_current()
            return
        if kind == InterruptKind.CHARGE:
            self._requeue_current()
            self.forced_charge = True
        elif kind == InterruptKind.RECALL:
            self.forced_charge = False
            self.target_id = None
        elif kind in (InterruptKind.PREEMPT, InterruptKind.STATION_DOWN, InterruptKind.REPLAN):
            self._requeue_current()
        if task is not None:
            self.engine.assign(task, self)

    def _move_point(self, tx: float, ty: float, forced_duration: float | None = None) -> Generator[Any, Any, None]:
        """현 위치에서 한 점까지 이동한다 (배터리 소모 포함). 배터리가 바닥나면 BatteryDepleted.

        Args:
            tx: 목표 X
            ty: 목표 Y
            forced_duration: 지정하면 거리와 무관하게 이 시간을 쓴다
        """
        dist_px = math.hypot(tx - self.x, ty - self.y)
        if dist_px < 1e-6 and forced_duration is None:
            return
        speed_px = max(0.05, float(self.cfg.move_speed)) / config.METERS_PER_PIXEL
        duration = forced_duration if forced_duration is not None else dist_px / speed_px
        drain = dist_px * config.METERS_PER_PIXEL * float(self.cfg.battery_drain_rate)
        fraction = 1.0 if drain <= self.battery else max(0.0, self.battery / drain)
        now = self.env.now
        self._segment = (self.x, self.y, tx, ty, now, now + duration, drain)
        yield self.env.timeout(duration * fraction)
        self._apply_segment(fraction)
        if fraction < 1.0:
            self._clear_route()
            raise BatteryDepleted()

    def _wait_for_traffic(self, request: LaneRequest, reason: str, identifier: str) -> Generator[Any, Any, None]:
        """자원 확보 전에는 진입점에서 대기하고 중단 시에도 대기 계측을 정리한다."""
        started = float(self.env.now)
        if not request.triggered:
            self.wait_reason, self.wait_resource, self.wait_since = reason, identifier, started
            self._set_state(RobotState.WAITING)
        try:
            channel = "corridor" if identifier.startswith("edge:") else "intersection"
            yield from observe_wait(self, request, channel, identifier)
        finally:
            elapsed = float(self.env.now) - started
            if identifier.startswith("edge:"):
                self.engine.metrics.record_corridor_wait(elapsed)
            else:
                self.engine.metrics.record_intersection_wait(elapsed)
            if elapsed > config.DEADLOCK_THRESHOLD_SEC:
                self.engine.metrics.record_long_wait()
            self.wait_reason, self.wait_resource, self.wait_since = "", "", None
        self._set_state(RobotState.MOVING)

    def _ride_process(
        self, lift_id: str, tx: float, ty: float, floor: int, expected_source: float | None = None
    ) -> Generator[Any, Any, None]:
        """실제 캐빈 도착·하차 후 점유를 해제하고 보류한 제어 명령을 처리한다."""
        elevator = self.engine.elevators.get(lift_id)
        if elevator is None or not elevator.active:
            raise TrafficBlocked("사용 가능한 실제 엘리베이터 자원이 없습니다.")
        wait_start = self.env.now
        with elevator.resource.request() as request:
            self._set_state(RobotState.WAITING)
            yield from observe_wait(self, request, "cabin", "cabin:" + lift_id)
            self.engine.metrics.record_ev_wait(self.env.now - wait_start)
            if expected_source is not None and abs(elevator.floor_position() - expected_source) > 1e-6:
                self.engine.ledger.emit(
                    "planning_cabin_source_changed",
                    robot=self.robot_id,
                    cabin=lift_id,
                    expected=expected_source,
                    actual=elevator.floor_position(),
                )
                raise simpy.Interrupt({"kind": InterruptKind.REPLAN})
            yield from elevator.arrive(self.floor)
            self._set_state(RobotState.MOVING)
            self._riding = elevator
            self._cabin_request = request
            self._cabin_destination = floor
            travel_seconds = max(0.0, float(elevator.cfg.travel_time))
            door_seconds = elevator.door_time / 2.0
            distance = abs(float(floor) - elevator.floor_position())
            self._cabin_release_at = float(self.env.now) + distance * travel_seconds + door_seconds
            try:
                self.engine.ledger.emit("cabin_boarded", robot=self.robot_id, cabin=lift_id, floor=self.floor)
                yield from elevator.carry(
                    floor,
                    travel_seconds=travel_seconds,
                    door_seconds=door_seconds,
                    defer=self._defer_control,
                )
            finally:
                self._riding = None
                self._cabin_request = None
                self._cabin_release_at = None
                self._cabin_destination = None
        self.x, self.y, self.floor = tx, ty, floor
        self.engine.ledger.emit("cabin_arrived", robot=self.robot_id, cabin=lift_id, floor=floor)
        self._raise_pending_control()

    def _travel_route(self, route: Route, not_before: tuple[float, ...] = ()) -> Generator[Any, Any, None]:
        """경로를 따라 이동한다. 층이 바뀌는 점은 엘리베이터 탑승, 그 외에는 통로 차선·교차점 제약을 지킨다."""
        gate = self.engine._traffic_ready
        if gate is not None and not gate.triggered:
            yield gate
            raise simpy.Interrupt({"kind": InterruptKind.STATION_DOWN})
        count = len(route.points)
        if not_before and (
            len(not_before) != count
            or not all(math.isfinite(t) for t in not_before)
            or any(a > b for a, b in pairwise(not_before))
        ):
            raise ValueError("one finite planned start is required per route segment")
        self.route = list(route.points)
        self.route_floors = list(route.floors) if len(route.floors) == count else [self.floor] * count
        self.route_lifts = list(route.lifts) if len(route.lifts) == count else [None] * count
        self._route_progress_key = self._plan_route_key() if not_before else None
        self._route_completed_segments = 0
        has_graph = len(route.nodes) == count and len(route.prev_nodes) == count and count > 0
        corridors = self.engine.corridors
        index = 0
        planned_cabins = iter(())
        if not_before and self.planned_action is not None:
            result, action = self.planned_action
            if action.arc.route is not None:
                planned_cabins = iter(result.input.routes[action.arc.route].cabins)
        while index < count:
            # A layout change may start while the previous section is occupied.
            # Wait outside it, then let the normal control path re-plan the task.
            gate = self.engine._traffic_ready
            if gate is not None and not gate.triggered:
                yield gate
                raise simpy.Interrupt({"kind": InterruptKind.STATION_DOWN})
            if not_before and self.env.now < not_before[index]:
                yield self.env.timeout(not_before[index] - self.env.now)
            tx, ty = self.route[0]
            lift, floor = self.route_lifts[0], self.route_floors[0]
            if lift is not None and floor != self.floor:
                planned = next(planned_cabins, None)
                yield from self._ride_process(lift, tx, ty, floor, planned.source_floor if planned else None)
                self._pop_route()
                index += 1
                self._route_completed_segments = index
                continue
            end = corridors.section_end(route, index) if has_graph else index
            yield from self._travel_section(route, index, end, has_graph, not_before)
            index = end + 1
            self._route_completed_segments = index

    def _travel_section(
        self, route: Route, start: int, end: int, has_graph: bool, not_before: tuple[float, ...] = ()
    ) -> Generator[Any, Any, None]:
        """Atomically acquire the whole section and release only after tail clearance."""
        corridors = self.engine.corridors
        reservations = {}
        labels = {}
        if has_graph:
            demands = corridors.section_demands(route, start, end, self.cfg.width)
            reservations = {corridors.demand_resource(demand): demand.amount for demand in demands}
            for index in range(start, end + 1):
                node, previous = route.nodes[index], route.prev_nodes[index]
                edge_id = corridors.edge_between(previous, node)
                if edge_id is not None:
                    resource = corridors.edge_resource(edge_id)
                    labels[resource] = (f"edge:{edge_id}", "통로 진입 대기")
                if corridors.is_intersection(node):
                    labels[corridors.intersection_resource(node)] = (f"node:{node}", "교차로 진입 대기")
        request = corridors.arbiter.request(reservations, self.robot_id) if reservations else None
        self._traffic_request = request
        self._traffic_release_at = None
        try:
            self._traffic_section_active = True
            if request is not None:
                blocker = corridors.arbiter.blocker(request)
                identifier, reason = labels.get(blocker, ("", "통로 진입 대기"))
                yield from self._wait_for_traffic(request, reason, identifier)
                self._traffic_granted = True
            for index in range(start, end + 1):
                if not_before and self.env.now < not_before[index]:
                    yield from self._protected_delay(not_before[index] - self.env.now)
                tx, ty = self.route[0]
                remaining = (list(self.route), list(self.route_floors), list(self.route_lifts))
                while True:
                    try:
                        yield from self._move_point(tx, ty)
                        break
                    except simpy.Interrupt as interruption:
                        if request is None or not request.granted:
                            raise
                        if self._segment is not None:
                            *_, t0, t1, _ = self._segment
                            self._apply_segment(self._segment_fraction(t0, t1))
                        self._defer_control(interruption.cause)
                    except BatteryDepleted:
                        if request is None or not request.granted:
                            raise
                        # The stalled robot still owns the physical corridor.
                        self.route, self.route_floors, self.route_lifts = map(list, remaining)
                        yield from self._fault_process(protected=True)
                        self._defer_control({"kind": InterruptKind.CHARGE})
                        self._set_state(RobotState.MOVING)
                self._pop_route()
                if has_graph and corridors.is_intersection(route.nodes[index]):
                    duration = self.cfg.length / max(0.05, self.cfg.move_speed) + config.INTERSECTION_MARGIN_SEC
                    yield from self._protected_delay(duration)
            if request is not None:
                tail = tail_clearance_seconds(self.cfg.length, self.cfg.move_speed)
                self._traffic_release_at = float(self.env.now) + tail
                yield from self._protected_delay(tail)
        finally:
            if request is not None:
                request.release()
            self._traffic_request = None
            self._traffic_release_at = None
            self._finish_traffic_section()
        self._raise_pending_control()

    def _protected_delay(self, seconds: float) -> Generator[Any, Any, None]:
        deadline = self.env.now + seconds
        while self.env.now < deadline:
            try:
                yield self.env.timeout(deadline - self.env.now)
            except simpy.Interrupt as interruption:
                self._defer_control(interruption.cause)

    def _defer_control(self, cause: Any) -> bool:
        new = cause if isinstance(cause, dict) else {"kind": str(cause)}
        old = self._pending_control
        if old is not None and old.get("kind") == InterruptKind.RETIRE:
            return new.get("task") is None
        if (
            old is not None
            and old.get("task") is not None
            and old.get("task") is not new.get("task")
            and old.get("task") is not self.current_task
        ):
            self.engine.requeue_task(old["task"])
        self._pending_control = new
        return True

    def _raise_pending_control(self) -> None:
        if self._pending_control is not None:
            cause, self._pending_control = self._pending_control, None
            raise simpy.Interrupt(cause)

    def _finish_traffic_section(self) -> None:
        """통로 점유 해제 후에만 새 로봇 크기를 적용한다."""
        self._traffic_section_active = False
        self._traffic_granted = False
        if self._pending_dimensions is not None:
            width, length = self._pending_dimensions
            self.cfg = replace(self.cfg, width=width, length=length)
            self._pending_dimensions = None

    def _task_process(
        self,
        planned_route: Route | None = None,
        not_before: tuple[float, ...] = (),
        work_not_before: float | None = None,
    ) -> Generator[Any, Any, None]:
        """배정된 작업 1건을 수행한다: 이동 → 대기열 → 작업."""
        task = self.current_task
        assert task is not None
        engine = self.engine
        station = engine.stations.get(task.station_id)
        if station is None or not station.active or station.cfg.task_type != task.operation:
            self._requeue_current()
            return
        route = planned_route or engine.path_graph.route_to_facility(
            self.x, self.y, station.id, self.floor, self.cfg.width
        )
        if route is None:
            self.current_task = None
            engine.report_unreachable(task, self)
            yield self.env.timeout(1.0)
            return
        self.target_id = station.id
        self.engine.task_state(task, TaskState.TRAVELING)
        self._set_state(RobotState.MOVING)
        yield from self._travel_route(route, not_before)
        self._mark_planned_route_complete()

        if work_not_before is not None and self.env.now < work_not_before:
            self._set_state(RobotState.WAITING)
            yield self.env.timeout(work_not_before - self.env.now)

        self._set_state(RobotState.WAITING)
        self.engine.task_state(task, TaskState.WAITING)
        station_wait = self.env.now
        with station.resource.request(priority=task.priority) as request:
            yield from observe_wait(self, request, "station", "work:" + station.id)
            engine.metrics.record_station_wait(self.env.now - station_wait)
            if (
                not station.active
                or engine.stations.get(station.id) is not station
                or station.cfg.task_type != task.operation
            ):
                self._requeue_current()
                return
            self._set_state(RobotState.WORKING)
            engine.on_task_started(task, self)
            station.begin(task, self.robot_id)
            started = self.env.now
            self._work_started = float(started)
            completed = False
            try:
                duration = (
                    max(0.5, station.cfg.avg_process_time + task.process_noise * station.cfg.process_time_std)
                    if task.process_noise is not None
                    else station.sample_process_time()
                )
                rate = engine.plant.work_drain_per_sec
                available_time = self.battery / rate if rate > 0 else math.inf
                yield self.env.timeout(min(duration, available_time))
                self._settle_work()
                if available_time + 1e-9 < duration:
                    station.end(False)
                    self._depleted = True
                    self._fault_holding = True
                    try:
                        yield from self._fault_process(protected=True)
                    finally:
                        self._fault_holding = False
                else:
                    completed = True
            finally:
                self._settle_work()
                if station.current_task is task:
                    station.end(completed)
        self._raise_pending_control()
        if not completed:
            return
        self.current_task = None
        self.target_id = None
        self.tasks_done += 1
        engine.complete_task(task, self)

    def _roam_process(self) -> Generator[Any, Any, None]:
        """일이 없는 로봇이 다음 층의 설비로 순회 이동한다 (엘리베이터 포함)."""
        engine = self.engine
        facility_id = engine.roam_target(self)
        route = engine.path_graph.route_to_facility(self.x, self.y, facility_id, self.floor) if facility_id else None
        if route is None:
            return
        self._set_state(RobotState.MOVING)
        yield from self._travel_route(route)
        self._clear_route()

    def _charge_process(self) -> Generator[Any, Any, None]:
        """충전소로 이동하여 목표 잔량까지 충전한다."""
        engine = self.engine
        charger = engine.scheduler.choose_charger(self, engine)
        route = (
            engine.path_graph.route_to_facility(self.x, self.y, charger.id, self.floor, self.cfg.width)
            if charger
            else None
        )
        if charger is None or route is None:
            self.forced_charge = False
            self._set_state(RobotState.IDLE)
            yield self.env.timeout(2.0)
            return
        self.target_id = charger.id
        charger.incoming.add(self.robot_id)
        try:
            self._set_state(RobotState.MOVING)
            yield from self._travel_route(route)
        finally:
            charger.incoming.discard(self.robot_id)

        self._set_state(RobotState.CHARGE_WAIT)
        wait_start = self.env.now
        with charger.resource.request() as request:
            yield from observe_wait(self, request, "charge", "charge:" + charger.id)
            engine.metrics.record_charge_wait(self.env.now - wait_start)
            self._set_state(RobotState.CHARGING)
            self.charge_sessions += 1
            charge_started, charge_completed = self.env.now, False
            observe_charge(self, charger.id, charge_started)
            charger.occupants.add(self.robot_id)
            try:
                while self.alive and charger.active:
                    if hasattr(engine.scheduler, "target_process"):
                        target_pct = yield from engine.scheduler.target_process(self)
                    else:
                        target_pct = engine.scheduler.charge_target(self, engine)
                    target = target_pct / 100.0 * self.capacity
                    if self.battery_pct >= target_pct - 1e-6:
                        charge_completed = True
                        break
                    rate = max(1e-9, float(charger.cfg.charge_rate))
                    step = min(config.CHARGE_STEP, (target - self.battery) / rate)
                    self._charge_step = (self.env.now, rate)
                    yield self.env.timeout(step)
                    self._settle_charge()
            finally:
                observe_charge(self, charger.id, charge_started, completed=charge_completed)
                charger.occupants.discard(self.robot_id)
        self.forced_charge = False
        self.target_id = None

    def _fault_process(self, protected: bool = False) -> Generator[Any, Any, None]:
        """방전 → 구조 대기 → 최소 잔량으로 복구 후 충전소행."""
        self._set_state(RobotState.FAULT)
        self._requeue_current()
        self.engine.on_robot_fault(self)
        if protected:
            yield from self._protected_delay(self.engine.plant.fault_recovery_time)
        else:
            yield self.env.timeout(self.engine.plant.fault_recovery_time)
        target = self.capacity * self.engine.plant.rescue_battery_pct / 100.0
        injected = max(0.0, target - self.battery)
        self.rescue_energy_units += injected
        self.battery += injected
        self.engine.ledger.emit("robot_rescued", robot=self.robot_id, injected=injected)
        self._depleted = False
        self.forced_charge = True
