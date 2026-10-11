"""실제 MILP·동일 조건 실험·단위 및 실패 처리를 검증한다."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from database.seed_data import build_default_config
from simulation import comparison, milp_scheduler
from simulation.comparison import _energy_distance, paired_interval, run_comparison
from simulation.engine import SimulationEngine
from simulation.milp_scheduler import RollingMilpScheduler
from utils.comparison_models import ComparisonConditions, calculate_economics


@pytest.fixture
def conditions() -> ComparisonConditions:
    """명시적인 가상 단가를 사용하는 작은 비교 조건."""
    return ComparisonConditions(duration=60, repeats=2, revenue_per_task=1000, electricity_per_kwh=150,
                                wh_per_battery_unit=1, movement_cost_per_m=1, fixed_cost=100,
                                cost_basis="예시값 · 가상 비용 모델")


def test_economics_units_and_zero_denominators(conditions: ComparisonConditions) -> None:
    """Wh→kWh, 비용 합산, 건당·ROI 분모를 직접 확인한다."""
    inputs = replace(conditions, charge_efficiency=0.5, ai_cost=10)
    actual = calculate_economics(2, 1000, 20, inputs, True)
    assert actual["energy_wh"] == 2000
    assert actual["electricity_cost"] == 300
    assert actual["total_cost"] == 430
    assert actual["net_profit"] == 1570
    assert actual["profit_per_task"] == 785
    assert actual["roi"] == pytest.approx(1570 / 430 * 100)
    assert calculate_economics(0, 0, 0, replace(inputs, fixed_cost=0), False)["roi"] is None
    assert calculate_economics(0, 0, 0, inputs, True)["profit_per_task"] is None
    assert calculate_economics(2, 10, 1, ComparisonConditions(), True)["net_profit"] is None


@pytest.mark.parametrize("change", [
    {"duration": float("nan")}, {"duration": float("inf")}, {"repeats": 2.5}, {"seed": -1},
    {"revenue_per_task": -1}, {"charge_efficiency": 0}, {"horizon": 4, "replan": 5},
    {"wh_per_battery_unit": float("inf")},
])
def test_invalid_conditions_are_rejected(change: dict[str, Any]) -> None:
    """유효하지 않은 비용·실험 입력을 거부한다."""
    with pytest.raises(ValueError):
        replace(ComparisonConditions(), **change).validate()


def test_interval_uses_paired_differences() -> None:
    """쌍대 차이의 평균과 t 신뢰구간을 확인한다."""
    result = paired_interval([1, 2, 3])
    assert result["mean"] == 2
    assert result["low"] == pytest.approx(-0.4841377)
    assert result["high"] == pytest.approx(4.4841377)


def test_actual_comparison_is_reproducible_and_preserves_input(conditions: ComparisonConditions) -> None:
    """실제 solver와 SimPy를 사용하며 입력 설정을 변경하지 않는다."""
    factory = build_default_config()
    context = {"factory_config": factory.to_dict()}
    first = run_comparison(conditions, context)
    second = run_comparison(conditions, context)
    assert first["samples"] == second["samples"]
    assert context["factory_config"] == factory.to_dict()
    assert first["solver"]["calls"] > 0
    assert first["solver"]["max_dispatch_gap"] == 0
    assert first["baseline"]["completed"] > 0
    assert first["candidate"]["energy_wh"] > 0
    assert "전체 기간 최적성 미보장" in first["solver"]["scope"]


def test_orders_are_used_instead_of_synthetic_background(conditions: ComparisonConditions) -> None:
    """사용자가 정한 남은 주문 수를 공통 작업 부하로 사용한다."""
    context = {"factory_config": build_default_config().to_dict(), "comparison_orders": [
        {"order_id": "O1", "product_type": "A", "quantity": 3, "completed": 1, "priority": "normal",
         "status": "pending", "required_stations": ["WS-01"]},
    ]}
    result = run_comparison(conditions, context)
    assert result["workload"] == "남은 주문만"
    assert all(sample["completed"] <= 2 for group in result["samples"].values() for sample in group)


def test_cancel_and_timeout(conditions: ComparisonConditions, monkeypatch: Any) -> None:
    """창 종료와 계산 시간 제한 시 결과를 생성하지 않는다."""
    context = {"factory_config": build_default_config().to_dict()}
    with pytest.raises(InterruptedError):
        run_comparison(conditions, context, lambda: True)
    monkeypatch.setattr(comparison, "EXPERIMENT_WALL_LIMIT", -1)
    with pytest.raises(TimeoutError):
        run_comparison(conditions, context)


def test_milp_never_double_assigns_resources() -> None:
    """로봇·작업·설비 중복 제한과 재계획 전 안정성을 확인한다."""
    engine = SimulationEngine(build_default_config(), comparison_seed=10)
    engine.env.run(until=0.1)
    engine.scheduler = RollingMilpScheduler(horizon=300, replan=5)
    tasks = [engine.add_task("A", "WS-01", 2, ("WS-01", "WS-02")) for _ in range(4)]
    engine.scheduler.dispatch(engine)
    assigned = [task for task in tasks if task.robot_id]
    assert assigned
    assert len({task.robot_id for task in assigned}) == len(assigned)
    assert len({task.station_id for task in assigned}) == len(assigned)
    calls = len(engine.scheduler.solve_records)
    engine.scheduler.dispatch(engine)
    assert len(engine.scheduler.solve_records) == calls


def test_solver_without_incumbent_does_not_fabricate_schedule(monkeypatch: Any) -> None:
    """시간 제한으로 해가 없으면 무배정을 유지한다."""
    from types import SimpleNamespace

    engine = SimulationEngine(build_default_config(), comparison_seed=10)
    engine.env.run(until=0.1)
    engine.scheduler = RollingMilpScheduler(horizon=300)
    task = engine.add_task("A", "WS-01", 2)
    monkeypatch.setattr(milp_scheduler, "milp", lambda *args, **kwargs: SimpleNamespace(status=1, x=None, mip_gap=None))
    engine.scheduler.dispatch(engine)
    assert task.robot_id is None
    assert engine.scheduler.solve_records == [{"status": 1, "gap": None}]


def test_energy_meter_includes_partial_movement_once() -> None:
    """이동 중 측정을 반복해도 이중 합산하지 않는다."""
    engine = SimulationEngine(build_default_config(), comparison_seed=10)
    robot = engine.robot_list()[0]
    robot._segment = (0, 0, 100, 0, 0, 10, 20)
    engine.env.run(until=5)
    first = _energy_distance(engine)
    assert first == _energy_distance(engine)
    assert first[0] == 10
    assert first[1] == 5
    robot._apply_segment(0.5)
    assert _energy_distance(engine) == first


def test_common_noise_is_independent_of_dispatch_order() -> None:
    """주문별 공통 난수는 다른 주문 릴리즈 순서와 무관하다."""
    left = SimulationEngine(build_default_config(), comparison_seed=22)
    right = SimulationEngine(build_default_config(), comparison_seed=22)
    a = left.add_task("A", "WS-01", 2, order_id="A")
    right.add_task("B", "WS-01", 2, order_id="B")
    b = right.add_task("A", "WS-01", 2, order_id="A")
    assert a.process_noise == b.process_noise
