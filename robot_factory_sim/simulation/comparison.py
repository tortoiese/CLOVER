"""사용자 공장 스냅샷으로 독립된 쌍대 비교 실험을 실행한다 (DB 쓰기 없음)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any

import numpy as np
from scipy.stats import t

import config
from simulation.energy import per_robot
from simulation.engine import SimulationEngine
from simulation.milp_scheduler import RollingMilpScheduler
from utils.comparison_models import ComparisonConditions, calculate_economics
from utils.config_models import FactoryConfig, OrderRecord
from utils.event_types import OrderStatus

EXPERIMENT_WALL_LIMIT = 30.0


def snapshot_fingerprint(context: dict[str, Any]) -> str:
    """설정·평가 주문·입력 파라미터 변경을 감지할 해시를 반환한다."""
    values = {key: context.get(key) for key in ("factory_config", "comparison_orders", "params")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_factory(factory: FactoryConfig) -> None:
    """비교 실험의 크기·좌표·단위를 검사한다."""
    if not 1 <= len(factory.robots) <= 20 or not 1 <= len(factory.stations) <= 20:
        raise ValueError("비교 실험은 로봇·설비 각각 1~20개까지 지원합니다.")
    if not 1 <= len(factory.chargers) <= 20 or not factory.nodes or len(factory.nodes) > 5000:
        raise ValueError("충전소 1~20개와 유효한 경로망이 필요합니다.")
    if len(factory.edges) > 20000:
        raise ValueError("경로 엣지가 너무 많습니다.")
    for dimension in (factory.profile.floor_width, factory.profile.floor_height):
        if not math.isfinite(dimension) or dimension <= 0:
            raise ValueError("공장 크기는 유한한 양수여야 합니다.")
    for group in (factory.robots, factory.stations, factory.chargers, factory.nodes):
        ids = [item.id for item in group]
        if len(ids) != len(set(ids)):
            raise ValueError("구성 ID가 중복되었습니다.")
    for collection in (factory.robots, factory.stations, factory.chargers, factory.nodes, factory.edges,
                       factory.obstacles):
        for item in collection:
            if any(isinstance(v, (float, int)) and not math.isfinite(v) for v in asdict(item).values()):
                raise ValueError("공장 설정에 비정상 숫자가 있습니다.")
    for collection in (factory.stations, factory.chargers, factory.nodes):
        for item in collection:
            if not 0 <= item.x <= factory.profile.floor_width or not 0 <= item.y <= factory.profile.floor_height:
                raise ValueError("설비·충전소·노드 위치가 공장 범위 밖입니다.")
    for robot in factory.robots:
        if robot.battery_capacity <= 0 or robot.move_speed <= 0 or robot.battery_drain_rate < 0:
            raise ValueError("로봇 용량·속도는 양수, 소모율은 비음수여야 합니다.")
        if not 0 <= robot.charge_threshold <= 100:
            raise ValueError("충전 임계값은 0~100%여야 합니다.")
    for station in factory.stations:
        if station.avg_process_time <= 0 or station.process_time_std < 0:
            raise ValueError("설비 처리시간과 표준편차가 잘못되었습니다.")
    for charger in factory.chargers:
        if charger.capacity < 1 or int(charger.capacity) != charger.capacity or charger.charge_rate <= 0:
            raise ValueError("충전 슬롯은 양의 정수, 충전 속도는 양수여야 합니다.")
    if not all(any(item.is_active for item in group) for group in (factory.robots, factory.stations, factory.chargers)):
        raise ValueError("활성 로봇·설비·충전소가 필요합니다.")


def _energy_distance(engine: SimulationEngine) -> tuple[float, float]:
    from simulation.energy import measure
    ledger = measure(engine)
    return ledger["battery_units"], ledger["distance_m"]


def _demand_process(engine: SimulationEngine, arrivals: list[tuple[float, str]]) -> Any:
    for when, station_id in arrivals:
        yield engine.env.timeout(max(0, when - engine.now))
        engine.add_task(config.BACKGROUND_PRODUCT, station_id, config.BACKGROUND_PRIORITY,
                        tuple(sorted(engine.stations)))


def _summary(samples: list[dict[str, Any]]) -> dict[str, float | None]:
    result = {}
    for key in samples[0]:
        values = [sample[key] for sample in samples]
        result[key] = (float(np.mean(values)) if all(value is not None and math.isfinite(value) for value in values)
                       else None)
    profit, cost = result.get("net_profit"), result.get("total_cost")
    result["roi"] = profit / cost * 100 if profit is not None and cost is not None and cost > 0 else None
    return result


def paired_interval(differences: list[float]) -> dict[str, float]:
    """독립 반복에서 정책 차이의 평균에 대한 95% t 신뢰구간을 계산한다."""
    values = np.asarray(differences, dtype=float)
    if len(values) < 2 or not np.all(np.isfinite(values)):
        raise ValueError("신뢰구간에는 유한한 차이 값 2개 이상이 필요합니다.")
    mean = float(values.mean())
    margin = float(t.ppf(0.975, len(values) - 1) * values.std(ddof=1) / math.sqrt(len(values)))
    return {"mean": mean, "low": mean - margin, "high": mean + margin}


def run_comparison(conditions: ComparisonConditions, context: dict[str, Any],
                   cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """동일 구성·주문·도착·작업별 난수로 베이스라인과 MILP 배정을 비교한다.

    로봇은 기본 배치·만충전으로 재시작한다. 진행 중 실행의 이어하기가 아니다.
    """
    conditions.validate()
    if context.get("comparison_policies"):
        from simulation.experiments import run_policy_comparison
        return run_policy_comparison(conditions, context, cancelled)
    if not context.get("factory_config"):
        raise ValueError("공장 설정이 없습니다. GUI가 준비된 뒤 다시 실행하세요.")
    factory = FactoryConfig.from_dict(context["factory_config"])
    validate_factory(factory)
    epoch = datetime.fromisoformat(context.get("sim_datetime", "2026-01-01 00:00:00"))
    orders = []
    raw_orders = context.get("comparison_orders", [])
    if len(raw_orders) > 100:
        raise ValueError("비교 주문은 최대 100개입니다.")
    for raw in raw_orders:
        quantity = int(raw["quantity"]) - int(raw.get("completed", 0))
        if quantity <= 0 or raw.get("status") not in OrderStatus.ACTIVE:
            continue
        if quantity > 100000 or raw.get("priority", "normal") not in config.ORDER_PRIORITIES:
            raise ValueError("주문 수량·우선순위가 잘못되었습니다.")
        required = list(raw.get("required_stations", []))
        if any(factory.station(station_id) is None for station_id in required):
            raise ValueError("주문이 존재하지 않는 설비를 참조합니다.")
        orders.append(OrderRecord(raw["order_id"], raw["product_type"], quantity,
                                  priority=raw.get("priority", "normal"), required_stations=required,
                                  deadline=datetime.fromisoformat(raw["deadline"]) if raw.get("deadline") else None,
                                  created_at=epoch))
    if len({order.order_id for order in orders}) != len(orders):
        raise ValueError("주문 ID가 중복되었습니다.")
    rate = float(context.get("params", {}).get("task_rate", config.DEFAULT_TASK_RATE))
    if not math.isfinite(rate) or not config.TASK_RATE_MIN <= rate <= config.TASK_RATE_MAX:
        raise ValueError("작업 생성 빈도가 허용 범위 밖입니다.")
    min_charge_mode = float(context.get("params", {}).get("min_charge_mode", 0))
    if min_charge_mode not in (0, 1):
        raise ValueError("최소충전 모드는 0 또는 1이어야 합니다.")
    parameters = {"task_rate": rate, "min_charge_mode": min_charge_mode}
    started = time.monotonic()
    samples: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    solver_records = []
    for offset in range(int(conditions.repeats)):
        seed = int(conditions.seed) + offset
        arrivals = []
        # 주문이 있으면 남은 주문만 평가. 없으면 동일한 합성 포아송 수요를 두 정책에 주입한다.
        if not orders:
            rng = np.random.default_rng(seed)
            when = float(rng.exponential(1 / rate))
            station_ids = sorted(s.id for s in factory.stations if s.is_active)
            while when < conditions.duration:
                arrivals.append((when, str(rng.choice(station_ids))))
                when += float(rng.exponential(1 / rate))
        for mode, policy_samples in samples.items():
            engine = SimulationEngine(copy.deepcopy(factory), "baseline" if mode == "baseline" else "optimized",
                                      seed=seed, epoch=epoch, comparison_seed=seed,
                                      params=dict(parameters))
            if mode == "candidate":
                engine.scheduler = RollingMilpScheduler(conditions.horizon, conditions.replan)
            engine.sync_orders(copy.deepcopy(orders))
            if arrivals:
                engine.env.process(_demand_process(engine, arrivals))
            while engine.now < conditions.duration:
                if cancelled and cancelled():
                    raise InterruptedError("비교 실험이 취소되었습니다.")
                if time.monotonic() - started > EXPERIMENT_WALL_LIMIT:
                    raise TimeoutError("비교 실험 시간 제한입니다. 기간·반복 횟수·공장 규모를 줄여주세요.")
                engine.env.run(until=min(conditions.duration, engine.now + 5))
            units, distance = _energy_distance(engine)
            metrics = {key: float(value) if math.isfinite(value) else None for key, value in engine.kpis().items()}
            sample = {**metrics, "completed": engine.metrics.total_completed,
                      "distance_m": distance, "battery_units": units,
                      **calculate_economics(engine.metrics.total_completed, units, distance, conditions,
                                            mode == "candidate", per_robot(engine))}
            policy_samples.append(sample)
            if mode == "candidate":
                solver_records.extend(engine.scheduler.solve_records)
    baseline, candidate = _summary(samples["baseline"]), _summary(samples["candidate"])
    intervals = {}
    for key in ("completed", "total_cost", "net_profit", "roi"):
        pairs = [c[key] - b[key] for b, c in zip(samples["baseline"], samples["candidate"], strict=True)
                 if b[key] is not None and c[key] is not None]
        if len(pairs) == conditions.repeats:
            intervals[key] = paired_interval(pairs)
    profit_ci = intervals.get("net_profit")
    if profit_ci is None:
        recommendation = "비용 모델 미설정: KPI 비교만 가능하며 ROI 추천은 보류합니다."
    elif profit_ci["low"] > 0 and candidate["completed"] >= baseline["completed"]:
        recommendation = "평가한 조건에서 평균 순이익 증가가 관측되어 MILP 배정안을 추천합니다."
    elif profit_ci["high"] < 0:
        recommendation = "평가한 조건에서 MILP 배정안의 순이익이 감소했습니다. 베이스라인 유지를 추천합니다."
    else:
        recommendation = "개선 효과가 불확실하거나 생산량이 감소해 추천을 보류합니다. 추가 검증이 필요합니다."
    gaps = [record["gap"] for record in solver_records if record["gap"] is not None]
    return {
        "experiment_id": str(uuid.uuid4()), "fingerprint": snapshot_fingerprint(context),
        "conditions": asdict(conditions), "factory_config": factory.to_dict(),
        "comparison_orders": raw_orders, "epoch": epoch.isoformat(),
        "parameters": parameters,
        "baseline": baseline, "candidate": candidate, "samples": samples, "intervals": intervals,
        "recommendation": recommendation,
        "solver": {"calls": len(solver_records), "optimal_calls": sum(r["status"] == 0 for r in solver_records),
                   "max_dispatch_gap": max(gaps) if gaps else None,
                   "scope": "현재 유휴 로봇의 단일 작업 배정 MILP; 충전은 기존 규칙; 전체 기간 최적성 미보장"},
        "workload": "남은 주문만" if orders else "합성 포아송 수요",
        "notes": ["현재 GUI의 설정을 복사한 독립 실험입니다. 기본 배치·만충전으로 시작합니다.",
                  "매출 단위는 완료 공정 작업 1건이며 완제품 매출과 구분해야 합니다.",
                  "전력은 이동·작업 배터리 소모를 환산한 보충 에너지 추정입니다. 실측·유휴전력은 미포함입니다.",
                  "95% 구간은 모델 내 평균 정책 차이의 t 신뢰구간입니다. 실제 공장 예측 오차가 아닙니다.",
                  "로봇·충전소 대수와 위치는 고정이며 자동 적용하지 않습니다."],
    }
