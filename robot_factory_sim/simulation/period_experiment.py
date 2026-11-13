"""운영 DB를 변경하지 않고 지정 기간 동안 두 운영 정책을 평가한다."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

import config
from simulation.engine import SimulationEngine
from utils.config_models import FactoryConfig, OrderRecord

SECONDS_PER_DAY = 86_400
MAX_EXPERIMENT_DAYS = 30
EXPERIMENT_SAMPLE_COUNT = 360
EXPERIMENT_STEP_SECONDS = 120
PROGRESS_INTERVAL = 0.5


@dataclass(frozen=True)
class PeriodResult:
    """전체 기간 집계와 그래프에 사용하는 처리량 표본."""

    days: int
    remaining_orders: int
    task_rate: float
    elapsed_seconds: float
    times: list[float]
    throughput: dict[str, list[float]]
    metrics: dict[str, dict[str, float]]


def run_period(
    factory: FactoryConfig,
    orders: list[OrderRecord],
    epoch: datetime,
    task_rate: float,
    days: int,
    *,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> PeriodResult:
    """같은 공장과 입력 시드로 라운드로빈·가중합을 지정 기간 동안 실행한다."""
    if not 1 <= days <= MAX_EXPERIMENT_DAYS:
        raise ValueError(f"실험 기간은 1~{MAX_EXPERIMENT_DAYS}일이어야 합니다.")
    if not math.isfinite(task_rate) or not config.TASK_RATE_MIN <= task_rate <= config.TASK_RATE_MAX:
        raise ValueError("작업 발생 빈도가 허용 범위를 벗어났습니다.")
    remaining = [replace(order, quantity=order.remaining, completed=0, created_at=epoch)
                 for order in orders if order.is_active and order.remaining]
    duration = days * SECONDS_PER_DAY
    interval = math.ceil(duration / EXPERIMENT_SAMPLE_COUNT)
    times = [0.0]
    throughputs: dict[str, list[float]] = {}
    results: dict[str, dict[str, float]] = {}
    started = time.monotonic()
    last_update = started

    for index, mode in enumerate(config.SCHEDULER_MODES):
        if cancelled and cancelled():
            raise InterruptedError
        engine = SimulationEngine(factory, mode, seed=config.RANDOM_SEED, epoch=epoch,
                                  params={"task_rate": task_rate}, comparison_seed=config.RANDOM_SEED)
        engine.sync_orders(remaining)
        engine.env.process(engine.generator.poisson_process(shared_input=True))
        series = throughputs[mode] = [0.0]
        last_completed = 0
        for sample_start in range(0, duration, interval):
            target = min(duration, sample_start + interval)
            while engine.now < target:
                if cancelled and cancelled():
                    raise InterruptedError
                engine.env.run(until=min(target, engine.now + EXPERIMENT_STEP_SECONDS))
                engine.drain_alerts()
                engine.drain_notices()
                now = time.monotonic()
                if progress and now - last_update >= PROGRESS_INTERVAL:
                    progress(int((index + engine.now / duration) / len(config.SCHEDULER_MODES) * 100))
                    last_update = now
            completed = engine.metrics.total_completed
            series.append((completed - last_completed) * 60 / (target - sample_start))
            last_completed = completed
            if index == 0:
                times.append(target / SECONDS_PER_DAY)
        metrics = engine.kpis()
        metrics["throughput"] = last_completed * 60 / duration
        metrics["avg_wait"] = engine.metrics.mean_wait
        metrics["charge_wait"] = engine.metrics.mean_charge_wait
        metrics["unfinished_orders"] = sum(order.is_active for order in engine.orders.values())
        metrics["background_arrivals"] = engine.metrics.background_arrivals
        metrics["background_rejected"] = engine.metrics.background_rejected
        results[mode] = metrics
    if progress:
        progress(100)
    return PeriodResult(days, len(remaining), task_rate, time.monotonic() - started, times, throughputs, results)
