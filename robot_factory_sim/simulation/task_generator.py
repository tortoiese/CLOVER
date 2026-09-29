"""작업 생성기 — 포아송 랜덤 작업 + 주문 기반 작업의 하이브리드."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

import config

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


@dataclass
class Task:
    """로봇이 수행할 작업 1건 (= 제품 1단위 공정)."""

    task_id: int
    station_id: str
    product: str
    priority: int
    created_at: float
    order_id: str | None = None
    allowed_stations: tuple[str, ...] = field(default_factory=tuple)
    deadline: float | None = None
    assigned_at: float | None = None
    started_at: float | None = None
    robot_id: str | None = None


class TaskGenerator:
    """작업 생성 SimPy 프로세스 모음."""

    def __init__(self, engine: SimulationEngine, rng: np.random.Generator) -> None:
        """생성기를 만든다.

        Args:
            engine: 소속 엔진
            rng: 도착 간격/스테이션 선택용 난수 생성기 (Baseline·최적화 엔진이 같은 시드를 공유)
        """
        self.engine = engine
        self.rng = rng

    def poisson_process(self) -> Generator[Any, Any, None]:
        """포아송 도착 과정으로 일반(백그라운드) 작업을 생성한다."""
        env = self.engine.env
        while True:
            rate = min(config.TASK_RATE_MAX, max(config.TASK_RATE_MIN, float(self.engine.params["task_rate"])))
            yield env.timeout(float(self.rng.exponential(1.0 / rate)))
            # 두 엔진의 난수 시퀀스를 맞추기 위해 조건과 무관하게 항상 뽑는다.
            pick = float(self.rng.random())
            stations = self.engine.background_station_ids()
            if not stations or self.engine.background_task_count() >= config.MAX_BACKGROUND_TASKS:
                continue
            station_id = stations[min(len(stations) - 1, int(pick * len(stations)))]
            self.engine.add_task(
                product=config.BACKGROUND_PRODUCT,
                station_id=station_id,
                priority=config.BACKGROUND_PRIORITY,
                allowed_stations=tuple(stations),
            )

    def order_release_process(self) -> Generator[Any, Any, None]:
        """주기적으로 주문 작업을 릴리즈한다."""
        env = self.engine.env
        while True:
            yield env.timeout(config.ORDER_RELEASE_INTERVAL)
            self.engine.release_order_tasks()
