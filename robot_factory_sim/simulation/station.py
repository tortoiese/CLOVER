"""WorkStation, ChargingStation — SimPy 자원 래퍼."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import simpy

from utils.config_models import ChargingStationConfig, WorkStationConfig

if TYPE_CHECKING:
    from simulation.task_generator import Task

MIN_PROCESS_TIME: float = 0.5


class WorkStation:
    """작업 스테이션. 한 번에 로봇 1대가 작업하며 대기열은 우선순위 순."""

    def __init__(self, env: simpy.Environment, cfg: WorkStationConfig, rng: np.random.Generator) -> None:
        """스테이션을 생성한다.

        Args:
            env: SimPy 환경
            cfg: 스테이션 설정
            rng: 처리시간 샘플링용 난수 생성기
        """
        self.env = env
        self.cfg = cfg
        self.rng = rng
        self.resource = simpy.PriorityResource(env, capacity=1)
        self.removed = False
        self.current_task: Task | None = None
        self.current_robot: str | None = None
        self.busy_since: float | None = None
        self.busy_time = 0.0
        self.processed = 0
        self.dedicated_order: str | None = None

    @property
    def id(self) -> str:
        """스테이션 ID."""
        return self.cfg.id

    @property
    def active(self) -> bool:
        """가동 여부 (비활성/삭제 시 False)."""
        return bool(self.cfg.is_active) and not self.removed

    @property
    def waiting(self) -> int:
        """스테이션 앞에서 대기 중인 로봇 수."""
        return len(self.resource.queue)

    def sample_process_time(self) -> float:
        """정규분포 처리시간을 샘플링한다.

        Returns:
            처리시간 (sim-sec)
        """
        value = float(self.rng.normal(self.cfg.avg_process_time, max(0.0, self.cfg.process_time_std)))
        return max(MIN_PROCESS_TIME, value)

    def begin(self, task: Task, robot_id: str) -> None:
        """작업 시작을 기록한다.

        Args:
            task: 처리할 작업
            robot_id: 작업 로봇 ID
        """
        self.current_task = task
        self.current_robot = robot_id
        self.busy_since = self.env.now

    def end(self, completed: bool) -> None:
        """작업 종료를 기록한다.

        Args:
            completed: 정상 완료 여부 (중단 시 False)
        """
        if self.busy_since is not None:
            self.busy_time += self.env.now - self.busy_since
        self.busy_since = None
        self.current_task = None
        self.current_robot = None
        if completed:
            self.processed += 1

    def utilization(self) -> float:
        """가동률 (%).

        Returns:
            0~100
        """
        now = self.env.now
        if now <= 0:
            return 0.0
        busy = self.busy_time + (now - self.busy_since if self.busy_since is not None else 0.0)
        return min(100.0, busy / now * 100.0)


class ChargingStation:
    """충전 스테이션. capacity 만큼 동시 충전."""

    def __init__(self, env: simpy.Environment, cfg: ChargingStationConfig) -> None:
        """충전소를 생성한다.

        Args:
            env: SimPy 환경
            cfg: 충전소 설정
        """
        self.env = env
        self.cfg = cfg
        self.resource = simpy.Resource(env, capacity=max(1, int(cfg.capacity)))
        self.removed = False
        self.occupants: set[str] = set()
        self.incoming: set[str] = set()

    @property
    def id(self) -> str:
        """충전소 ID."""
        return self.cfg.id

    @property
    def active(self) -> bool:
        """가동 여부."""
        return bool(self.cfg.is_active) and not self.removed

    @property
    def capacity(self) -> int:
        """동시 충전 슬롯 수."""
        return max(1, int(self.cfg.capacity))

    @property
    def waiting(self) -> int:
        """충전 대기 로봇 수."""
        return len(self.resource.queue)

    @property
    def free_slots(self) -> int:
        """(이동 중 로봇까지 고려한) 빈 슬롯 수."""
        return self.capacity - len(self.occupants) - self.waiting - len(self.incoming)

    def update_config(self, cfg: ChargingStationConfig) -> None:
        """설정을 갱신한다. 슬롯 수가 바뀌면 자원을 새로 만든다 (기존 점유자는 이전 자원에서 해제).

        Args:
            cfg: 새 설정
        """
        if max(1, int(cfg.capacity)) != self.capacity:
            self.resource = simpy.Resource(self.env, capacity=max(1, int(cfg.capacity)))
        self.cfg = cfg
