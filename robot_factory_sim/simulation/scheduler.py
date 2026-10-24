"""작업 배정 / 충전 스케줄러.

- BaselineScheduler: 라운드로빈(FIFO) 배정 + 배터리 소진(임계치) 시 만충전.
- OptimizedScheduler: 배터리·거리·큐·우선순위·납기 가중합 배정 + 예측 기반 선제 충전 + 긴급 시 최소충전 복귀.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TYPE_CHECKING

import config
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot
    from simulation.station import ChargingStation
    from simulation.task_generator import Task

CRITICAL_PREEMPT_DELAY: float = 2.0      # critical 작업이 이 시간 이상 대기하면 자동 선점 (sim-sec)
BACKLOG_CHARGE_TARGET: float = 75.0      # 대기열 적체 시 충전 목표 (%)
EXPECTED_CHARGE_AMOUNT: float = 50.0     # 충전 대기 예상치 계산용 평균 충전량 (%)


class BaseScheduler(ABC):
    """스케줄러 확장 포인트."""

    name: str = "base"

    def dispatch(self, engine: SimulationEngine) -> None:
        """유휴 로봇에 작업을 배정한다. (긴급 배정 로봇 우선)

        Args:
            engine: 시뮬레이션 엔진
        """
        idle = [r for r in engine.robot_list() if r.is_available()]
        idle = self._dispatch_emergency(engine, idle)
        self._dispatch(engine, idle)

    @staticmethod
    def _dispatch_emergency(engine: SimulationEngine, idle: list[Robot]) -> list[Robot]:
        """긴급 주문에 지정된 로봇에게 해당 주문 작업을 먼저 배정한다."""
        remaining: list[Robot] = []
        for robot in idle:
            order_id = robot.emergency_order
            task = engine.take_task(lambda t, oid=order_id: t.order_id == oid) if order_id else None
            if task is not None:
                engine.assign(task, robot)
            else:
                remaining.append(robot)
        return remaining

    @abstractmethod
    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        """스케줄러별 배정 로직."""

    @abstractmethod
    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """작업이 없을 때 충전하러 가야 하는지.

        Args:
            robot: 대상 로봇
            engine: 엔진

        Returns:
            충전 필요 여부
        """

    @abstractmethod
    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """충전 목표 잔량 (%).

        Args:
            robot: 대상 로봇
            engine: 엔진

        Returns:
            목표 %
        """

    @abstractmethod
    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """사용할 충전소를 고른다.

        Args:
            robot: 대상 로봇
            engine: 엔진

        Returns:
            충전소 또는 None
        """


class BaselineScheduler(BaseScheduler):
    """라운드로빈 배정 + 반응형 충전 (비교 기준)."""

    name = "baseline"

    def __init__(self) -> None:
        """스케줄러를 생성한다."""
        self._pointer = 0

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        """생성 순서(FIFO)대로 작업을 꺼내 로봇 순번대로 배정한다."""
        if not idle or not engine.pending_tasks:
            return
        order = engine.robot_ids()
        idle_ids = {r.robot_id for r in idle}
        for task in sorted(engine.pending_tasks, key=lambda t: t.task_id):
            if not idle_ids:
                break
            if not engine.station_available(task.station_id):
                continue
            for step in range(len(order)):
                robot_id = order[(self._pointer + step) % len(order)]
                if robot_id in idle_ids:
                    engine.assign(task, engine.robots[robot_id])
                    idle_ids.discard(robot_id)
                    self._pointer = (self._pointer + step + 1) % len(order)
                    break

    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """임계치 미만일 때만 충전 (반응형)."""
        return robot.battery_pct < min(100.0, robot.cfg.charge_threshold) - 1e-6

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """항상 만충전."""
        return config.FULL_CHARGE_LEVEL

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """직선 거리 기준 가장 가까운 충전소 (대기열 무시)."""
        chargers = [c for c in engine.chargers.values() if c.active]
        if not chargers:
            return None
        return min(chargers, key=lambda c: math.hypot(c.cfg.x - robot.x, c.cfg.y - robot.y))


class OptimizedScheduler(BaseScheduler):
    """가중합 최적 배정 + 예측 충전."""

    name = "optimized"

    def __init__(self, weights: dict[str, float] | None = None) -> None:
        """스케줄러를 생성한다.

        Args:
            weights: 비용 가중치 (distance, battery, queue, priority, deadline)
        """
        self.weights = dict(config.SCHEDULER_WEIGHTS)
        if weights:
            self.weights.update(weights)

    # ------------------------------------------------------------------
    # 배정
    # ------------------------------------------------------------------
    def _task_key(self, engine: SimulationEngine) -> Callable[[Task], tuple[float, float, float]]:
        """작업 정렬 키: 우선순위 → 납기 여유 → 생성 순."""
        now = engine.env.now

        def key(task: Task) -> tuple[float, float, float]:
            slack = (task.deadline - now) if task.deadline is not None else math.inf
            return float(task.priority), slack, task.created_at

        return key

    def _urgency(self, task: Task, now: float) -> float:
        """우선순위·납기 긴급도 (0 이상). 긴급할수록 가까운 로봇을 더 강하게 선호한다."""
        max_level = max(config.PRIORITY_LEVELS.values())
        urgency = self.weights["priority"] * (max_level - task.priority) / max_level
        if task.deadline is not None:
            slack = max(0.0, task.deadline - now)
            urgency += self.weights["deadline"] * config.DEFAULT_TASK_CYCLE / (config.DEFAULT_TASK_CYCLE + slack)
        return urgency

    def _energy_needed(self, robot: Robot, engine: SimulationEngine, station_id: str, distance: float) -> float:
        """작업 수행 + 가장 가까운 충전소 복귀까지 필요한 배터리량."""
        station = engine.stations[station_id]
        sx, sy = station.cfg.x, station.cfg.y
        back = min(
            (engine.path_graph.distance_to_facility(sx, sy, c.id) for c in engine.chargers.values() if c.active),
            default=0.0,
        )
        back = 0.0 if math.isinf(back) else back
        travel = (distance + back) * config.METERS_PER_PIXEL * float(robot.cfg.battery_drain_rate)
        work = station.cfg.avg_process_time * config.WORK_DRAIN_PER_SEC
        return travel + work

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        """비용이 가장 낮은 (로봇, 스테이션) 조합에 작업을 배정한다."""
        free = list(idle)
        low_battery: set[str] = set()
        profile = engine.factory.profile
        diagonal = max(1.0, math.hypot(profile.floor_width, profile.floor_height))
        pending_by_station = engine.pending_count_by_station()

        for task in sorted(engine.pending_tasks, key=self._task_key(engine)):
            if not free:
                break
            candidates = [s for s in (task.allowed_stations or (task.station_id,)) if engine.station_available(s)]
            if not candidates:
                continue
            best: tuple[float, Robot, str] | None = None
            urgency = self._urgency(task, engine.env.now)
            for robot in free:
                reserve = robot.capacity * config.BATTERY_RESERVE / 100.0
                for station_id in candidates:
                    distance = engine.path_graph.distance_to_facility(robot.x, robot.y, station_id)
                    if math.isinf(distance):
                        continue
                    if robot.battery - self._energy_needed(robot, engine, station_id, distance) < reserve:
                        low_battery.add(robot.robot_id)
                        continue
                    station = engine.stations[station_id]
                    load = station.waiting + pending_by_station.get(station_id, 0) + (1 if station.current_task else 0)
                    cost = (
                        self.weights["distance"] * (1.0 + urgency) * distance / diagonal
                        + self.weights["battery"] * (1.0 - robot.battery_pct / 100.0)
                        + self.weights["queue"] * load / 3.0
                    )
                    if best is None or cost < best[0]:
                        best = (cost, robot, station_id)
            if best is None:
                continue
            _, robot, station_id = best
            if station_id != task.station_id:
                pending_by_station[task.station_id] = max(0, pending_by_station.get(task.station_id, 0) - 1)
                pending_by_station[station_id] = pending_by_station.get(station_id, 0) + 1
                task.station_id = station_id
            engine.assign(task, robot)
            free.remove(robot)
            low_battery.discard(robot.robot_id)

        # 예측 충전: 다음 작업을 끝내고 복귀할 배터리가 부족한 로봇은 미리 충전
        for robot in free:
            if robot.robot_id in low_battery:
                engine.send_to_charge(robot)
        free = [r for r in free if r.robot_id not in low_battery]

        # 기회 충전: 대기 작업이 없으면 잔량 낮은 유휴 로봇부터 빈 슬롯에 충전
        if not engine.pending_tasks:
            slots = sum(max(0, c.free_slots) for c in engine.chargers.values() if c.active)
            for robot in sorted(free, key=lambda r: r.battery_pct):
                if slots <= 0 or robot.battery_pct >= config.OPPORTUNISTIC_CHARGE_LEVEL:
                    break
                engine.send_to_charge(robot)
                slots -= 1

        self._preempt_for_critical(engine)

    def _preempt_for_critical(self, engine: SimulationEngine) -> None:
        """critical 작업이 오래 대기하면 저우선순위 작업 중인 로봇 1대를 선점한다."""
        now = engine.env.now
        waiting = [
            t for t in engine.pending_tasks
            if t.priority == config.PRIORITY_LEVELS["critical"] and now - t.created_at >= CRITICAL_PREEMPT_DELAY
        ]
        if not waiting:
            return
        task = min(waiting, key=lambda t: t.created_at)
        victims = [
            r for r in engine.robot_list()
            if r.state == RobotState.MOVING and r.current_task is not None
            and r.current_task.priority >= config.PRIORITY_LEVELS["normal"]
            and r.battery_pct > config.RECALL_MIN_BATTERY
        ]
        if victims:
            victim = min(victims, key=lambda r: engine.path_graph.distance_to_facility(r.x, r.y, task.station_id))
            engine.preempt(victim, task)

    # ------------------------------------------------------------------
    # 충전
    # ------------------------------------------------------------------
    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """임계치 미만이면 충전."""
        return robot.battery_pct < min(100.0, robot.cfg.charge_threshold) - 1e-6

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """상황별 충전 목표: 긴급/최소충전 모드 → 적체 → 평상시."""
        critical_pending = any(t.priority == config.PRIORITY_LEVELS["critical"] for t in engine.pending_tasks)
        if engine.params.get("min_charge_mode") or critical_pending:
            target = config.MIN_CHARGE_TARGET
        elif len(engine.pending_tasks) > 2 * max(1, len(engine.robots)):
            target = BACKLOG_CHARGE_TARGET
        else:
            target = config.OPTIMIZED_CHARGE_TARGET
        # 충전 종료 직후 다시 충전에 진입하지 않도록 시작 임계값 이상을 보장한다.
        return min(100.0, max(target, robot.cfg.charge_threshold))

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """이동시간 + 예상 대기시간이 가장 짧은 충전소."""
        best: tuple[float, ChargingStation] | None = None
        speed_px = max(0.05, float(robot.cfg.move_speed)) / config.METERS_PER_PIXEL
        for charger in engine.chargers.values():
            if not charger.active:
                continue
            distance = engine.path_graph.distance_to_facility(robot.x, robot.y, charger.id)
            if math.isinf(distance):
                continue
            ahead = len(charger.occupants) + charger.waiting + len(charger.incoming)
            queued = max(0, ahead - charger.capacity + 1)
            per_session = EXPECTED_CHARGE_AMOUNT / max(0.01, float(charger.cfg.charge_rate))
            cost = distance / speed_px + queued * per_session / charger.capacity
            if best is None or cost < best[0]:
                best = (cost, charger)
        return best[1] if best else None


def create_scheduler(mode: str) -> BaseScheduler:
    """모드 이름으로 스케줄러를 생성한다 (팩토리).

    Args:
        mode: "baseline" | "optimized"

    Returns:
        스케줄러 인스턴스
    """
    match mode:
        case "baseline":
            return BaselineScheduler()
        case "optimized":
            return OptimizedScheduler()
        case _:
            raise ValueError(f"알 수 없는 스케줄러 모드: {mode}")
