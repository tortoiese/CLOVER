"""Elevator — 층간 이동용 SimPy 자원 래퍼.

로봇은 엘리베이터 자원을 점유한 뒤 (1) 자신이 있는 층으로 캐빈을 부르고 (2) 목적 층까지 함께 이동한다.
캐빈 위치는 시각화를 위해 소수 층 번호(1.0 = 1층, 1.5 = 1·2층 사이)로 보간해 제공한다.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import simpy

import config
from utils.config_models import ElevatorConfig

FLOOR_EPSILON: float = 1e-6


class Elevator:
    """엘리베이터 1대 (승강로 하나, 캐빈 하나)."""

    def __init__(self, env: simpy.Environment, cfg: ElevatorConfig) -> None:
        """엘리베이터를 생성한다. 캐빈은 1층에서 대기한다.

        Args:
            env: SimPy 환경
            cfg: 엘리베이터 설정
        """
        self.env = env
        self.cfg = cfg
        self.resource = simpy.Resource(env, capacity=max(1, int(cfg.capacity)))
        self.removed = False
        self.rides = 0
        self._floor = 1.0
        self._motion: tuple[float, float, float, float] | None = None  # (출발 층, 도착 층, 시작, 종료)

    @property
    def id(self) -> str:
        """엘리베이터 ID."""
        return self.cfg.id

    @property
    def active(self) -> bool:
        """가동 여부 (비활성/삭제 시 False)."""
        return bool(self.cfg.is_active) and not self.removed

    @property
    def waiting(self) -> int:
        """탑승 대기 로봇 수."""
        return len(self.resource.queue)

    @property
    def occupied(self) -> int:
        """탑승(점유) 중인 로봇 수."""
        return self.resource.count

    @property
    def moving(self) -> bool:
        """캐빈이 이동 중인지 여부."""
        return self._motion is not None

    def floor_position(self) -> float:
        """캐빈의 현재 층 (이동 중이면 보간된 소수 층).

        Returns:
            1.0 이상의 층 번호
        """
        if self._motion is None:
            return self._floor
        start, end, t0, t1 = self._motion
        fraction = 1.0 if t1 <= t0 else max(0.0, min(1.0, (self.env.now - t0) / (t1 - t0)))
        return start + (end - start) * fraction

    def update_config(self, cfg: ElevatorConfig) -> None:
        """설정을 갱신한다. 진행 중인 탑승은 이전 설정으로 마무리된다.

        Args:
            cfg: 새 설정
        """
        self.cfg = cfg

    def _move(self, target: float) -> Generator[Any, Any, None]:
        """캐빈을 target 층으로 옮긴다. 중단되면 그때의 보간 위치에 멈춘다."""
        start = self._floor
        distance = abs(target - start)
        if distance < FLOOR_EPSILON:
            return
        duration = distance * max(0.0, float(self.cfg.travel_time))
        self._motion = (start, target, self.env.now, self.env.now + duration)
        try:
            yield self.env.timeout(duration)
            self._floor = target
        finally:
            if self._floor != target:
                self._floor = self.floor_position()
            self._motion = None

    def arrive(self, floor: int) -> Generator[Any, Any, None]:
        """캐빈을 승차 층으로 부르고 승차한다 (자원을 점유한 로봇이 호출).

        Args:
            floor: 로봇이 기다리는 층
        """
        yield from self._move(float(floor))
        yield self.env.timeout(config.ELEVATOR_BOARD_TIME / 2.0)

    def carry(self, floor: int) -> Generator[Any, Any, None]:
        """목적 층까지 이동하고 하차한다.

        Args:
            floor: 목적 층
        """
        yield from self._move(float(floor))
        yield self.env.timeout(config.ELEVATOR_BOARD_TIME / 2.0)
        self.rides += 1
