"""KPI 계산.

SimulationEngine마다 MetricsCollector 하나를 두고, 스냅샷 시점에 compute()로 KPI dict를 만든다.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

import config
from utils.event_types import RobotState


@dataclass(frozen=True)
class KpiDefinition:
    """KPI 표시 정의."""

    key: str
    label: str
    unit: str
    higher_is_better: bool
    decimals: int = 1


KPI_DEFINITIONS: tuple[KpiDefinition, ...] = (
    KpiDefinition("throughput", "처리량", "건/분", True),
    KpiDefinition("avg_wait", "평균 대기시간", "s", False),
    KpiDefinition("utilization", "로봇 가동률", "%", True),
    KpiDefinition("charge_wait", "충전 대기시간", "s", False),
    KpiDefinition("idle_rate", "유휴율", "%", False),
    KpiDefinition("battery_eff", "배터리 효율", "건/회", True),
    KpiDefinition("order_fulfillment", "주문 달성률", "%", True),
    KpiDefinition("response_time", "긴급 대응시간", "s", False),
)

KPI_BY_KEY: dict[str, KpiDefinition] = {k.key: k for k in KPI_DEFINITIONS}


@dataclass
class RobotTimeUsage:
    """로봇 1대의 상태별 누적 시간과 카운터."""

    state_time: dict[str, float]
    charge_sessions: int
    tasks_done: int


def _mean(values: Iterable[float]) -> float:
    """빈 시퀀스면 NaN을 반환하는 평균."""
    items = list(values)
    return sum(items) / len(items) if items else math.nan


class MetricsCollector:
    """시뮬레이션 1개의 KPI 원천 데이터를 수집한다."""

    def __init__(self, window: float = config.THROUGHPUT_WINDOW) -> None:
        """수집기를 초기화한다.

        Args:
            window: 처리량 계산 슬라이딩 윈도우 (sim-sec)
        """
        self.window = window
        self.completions: deque[float] = deque()
        self.wait_times: deque[float] = deque(maxlen=config.METRIC_SAMPLE_LIMIT)
        self.cycle_times: deque[float] = deque(maxlen=config.METRIC_SAMPLE_LIMIT)
        self.charge_waits: deque[float] = deque(maxlen=config.METRIC_SAMPLE_LIMIT)
        self.response_times: deque[float] = deque(maxlen=config.METRIC_SAMPLE_LIMIT)
        self.total_completed = 0
        self.orders_finished = 0
        self.orders_on_time = 0

    def record_task_completed(self, now: float, cycle_time: float) -> None:
        """작업 완료를 기록한다.

        Args:
            now: 완료 시각 (sim-sec)
            cycle_time: 배정~완료 소요시간 (sim-sec)
        """
        self.completions.append(now)
        self.cycle_times.append(cycle_time)
        self.total_completed += 1

    def record_wait(self, wait: float) -> None:
        """작업 큐 대기시간(생성~처리 시작)을 기록한다.

        Args:
            wait: 대기시간 (sim-sec)
        """
        self.wait_times.append(max(0.0, wait))

    def record_charge_wait(self, wait: float) -> None:
        """충전 대기시간을 기록한다.

        Args:
            wait: 대기시간 (sim-sec)
        """
        self.charge_waits.append(max(0.0, wait))

    def record_response(self, response: float) -> None:
        """긴급 이벤트 대응시간을 기록한다.

        Args:
            response: 감지~첫 배정까지 시간 (sim-sec)
        """
        self.response_times.append(max(0.0, response))

    def record_order_finished(self, on_time: bool) -> None:
        """주문 종료를 기록한다.

        Args:
            on_time: 납기 내 완료 여부
        """
        self.orders_finished += 1
        if on_time:
            self.orders_on_time += 1

    def avg_cycle_time(self) -> float:
        """최근 작업 평균 사이클 타임. 샘플이 없으면 기본 추정치.

        Returns:
            평균 사이클 타임 (sim-sec)
        """
        value = _mean(self.cycle_times)
        return config.DEFAULT_TASK_CYCLE if math.isnan(value) else value

    def throughput(self, now: float) -> float:
        """슬라이딩 윈도우 기준 처리량.

        Args:
            now: 현재 시각 (sim-sec)

        Returns:
            분당 완료 작업 수
        """
        while self.completions and self.completions[0] < now - self.window:
            self.completions.popleft()
        span = min(self.window, now)
        if span <= 1e-9:
            return 0.0
        return len(self.completions) / span * 60.0

    def compute(self, now: float, robots: Iterable[RobotTimeUsage]) -> dict[str, float]:
        """KPI를 계산한다.

        Args:
            now: 현재 시각 (sim-sec)
            robots: 로봇별 시간 사용량

        Returns:
            KPI key → 값 (측정 불가 시 NaN)
        """
        busy = idle = total = 0.0
        sessions = 0
        for usage in robots:
            busy += sum(usage.state_time.get(s, 0.0) for s in RobotState.BUSY)
            idle += usage.state_time.get(RobotState.IDLE, 0.0)
            total += sum(usage.state_time.values())
            sessions += usage.charge_sessions
        order_rate = (self.orders_on_time / self.orders_finished * 100.0) if self.orders_finished else math.nan
        return {
            "throughput": self.throughput(now),
            "avg_wait": _mean(self.wait_times),
            "utilization": busy / total * 100.0 if total > 0 else 0.0,
            "charge_wait": _mean(self.charge_waits),
            "idle_rate": idle / total * 100.0 if total > 0 else 0.0,
            "battery_eff": self.total_completed / sessions if sessions else math.nan,
            "order_fulfillment": order_rate,
            "response_time": _mean(self.response_times),
        }


if __name__ == "__main__":
    collector = MetricsCollector()
    for t in range(1, 61):
        collector.record_task_completed(float(t), 15.0)
        collector.record_wait(2.0)
    usage = RobotTimeUsage({RobotState.MOVING: 30.0, RobotState.IDLE: 30.0}, charge_sessions=2, tasks_done=60)
    print(collector.compute(60.0, [usage]))
