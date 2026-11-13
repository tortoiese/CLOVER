"""장기 실험의 공통 입력, 전체 기간 표본과 취소를 검증한다."""

from datetime import datetime

import pytest

import config
from database.seed_data import build_default_config
from simulation.period_experiment import run_period


def test_one_day_experiment_has_full_history_and_common_arrivals() -> None:
    """시작·종료 표본을 보존하고 같은 시드의 작업 요청을 양쪽에 적용한다."""
    factory = build_default_config()
    original = factory.to_dict()
    updates: list[int] = []
    result = run_period(factory, [], datetime(2026, 1, 1), config.TASK_RATE_MIN, 1, progress=updates.append)
    assert result.times[0] == 0 and result.times[-1] == 1
    assert len(result.times) == len(result.throughput["baseline"]) == len(result.throughput["optimized"])
    assert result.metrics["baseline"]["background_arrivals"] == result.metrics["optimized"]["background_arrivals"]
    assert result.metrics["baseline"]["background_arrivals"] > 0
    assert result.metrics["optimized"]["throughput"] == pytest.approx(
        result.metrics["optimized"]["completed_units"] / 1440)
    assert updates == sorted(updates) and updates[-1] == 100
    assert factory.to_dict() == original


def test_period_experiment_checks_cancellation_during_execution() -> None:
    """취소 신호가 도착하면 기간 종료까지 기다리지 않는다."""
    checks = iter((False, False, True))
    with pytest.raises(InterruptedError):
        run_period(build_default_config(), [], datetime(2026, 1, 1), config.TASK_RATE_MIN, 30,
                   cancelled=lambda: next(checks))
