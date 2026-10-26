"""비교 실험 입력과 비용 산식. RAG나 LLM이 수치를 계산하지 않는다."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ComparisonConditions:
    """사용자가 확인하는 실험 조건과 비용 모델 (KRW, Wh, m, sim-sec)."""

    duration: float = 120.0
    repeats: int = 3
    seed: int = 42
    horizon: float = 60.0
    replan: float = 5.0
    revenue_per_task: float | None = None
    electricity_per_kwh: float | None = None
    wh_per_battery_unit: float | None = None
    charge_efficiency: float = 0.9
    movement_cost_per_m: float = 0.0
    fixed_cost: float = 0.0
    ai_cost: float = 0.0
    cost_basis: str = "사용자 입력 · 실측 미검증"
    robot_energy_profiles: tuple[tuple[str, float, float], ...] = ()

    def __post_init__(self) -> None:
        raw = self.robot_energy_profiles
        if not isinstance(raw, (tuple, list, dict)) or len(raw) > 1000:
            raise ValueError("robot energy profiles must be an object or a bounded row list")
        if isinstance(raw, dict):
            if any(not isinstance(value, (tuple, list)) or len(value) != 2 for value in raw.values()):
                raise ValueError("robot profile requires [Wh/unit, efficiency]")
            raw = tuple((key, *value) for key, value in sorted(raw.items()))
        object.__setattr__(self, "robot_energy_profiles", tuple(tuple(row) for row in raw))

    def validate(self) -> None:
        """비정상 수치와 GUI에서 감당할 수 없는 실험 크기를 거부한다."""
        for key, value in asdict(self).items():
            if key in {"cost_basis", "robot_energy_profiles"}:
                continue
            if value is None and key in {"revenue_per_task", "electricity_per_kwh", "wh_per_battery_unit"}:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key}: 유한한 숫자가 필요합니다.")
            if value < 0:
                raise ValueError(f"{key}: 음수는 사용할 수 없습니다.")
        if not 30 <= self.duration <= 600:
            raise ValueError("기간은 30~600초입니다.")
        if int(self.repeats) != self.repeats or not 2 <= self.repeats <= 10:
            raise ValueError("반복은 2~10회의 정수입니다.")
        if int(self.seed) != self.seed or not 0 <= self.seed <= 1_000_000:
            raise ValueError("시드는 0~1000000의 정수입니다.")
        if not 1 <= self.replan <= self.horizon <= 300:
            raise ValueError("1 ≤ 재계획 주기 ≤ 계획 구간 ≤ 300초여야 합니다.")
        if not 0 < self.charge_efficiency <= 1:
            raise ValueError("충전효율은 0 초과 1 이하여야 합니다.")
        limits = {"revenue_per_task": 10000000, "electricity_per_kwh": 100000,
                  "wh_per_battery_unit": 100000, "movement_cost_per_m": 100000,
                  "fixed_cost": 100000000, "ai_cost": 10000000}
        if any(getattr(self, key) is not None and getattr(self, key) > limit for key, limit in limits.items()):
            raise ValueError("비용 입력이 GUI 지원 범위를 초과했습니다.")
        if not isinstance(self.cost_basis, str) or not self.cost_basis.strip() or len(self.cost_basis) > 200:
            raise ValueError("비용 근거는 1~200자여야 합니다.")
        identifiers = set()
        for row in self.robot_energy_profiles:
            if len(row) != 3:
                raise ValueError("robot energy profile requires [robot_id, Wh/unit, efficiency]")
            rid, factor, efficiency = row
            if not isinstance(rid, str) or not rid or rid in identifiers:
                raise ValueError("robot energy profile IDs must be unique strings")
            identifiers.add(rid)
            if (not isinstance(factor, (int, float)) or not isinstance(efficiency, (int, float)) or
                    isinstance(factor, bool) or isinstance(efficiency, bool) or not math.isfinite(factor) or
                    not math.isfinite(efficiency) or not 0 <= factor <= 100000 or not 0 < efficiency <= 1):
                raise ValueError("invalid robot energy profile")

    @classmethod
    def from_dict(cls, values: dict[str, Any]) -> ComparisonConditions:
        """알려진 필드만 받아 검증한다."""
        conditions = cls(**values)
        conditions.validate()
        return conditions


def calculate_economics(completed: int, battery_units: float, distance_m: float,
                        conditions: ComparisonConditions, candidate: bool,
                        robot_energy: dict[str, float] | None = None) -> dict[str, float | None]:
    """소비 에너지 기준 비용과 공정 작업 1건 기준 수익을 계산한다.

    충전효율을 적용해 소비량을 보충하는 데 필요한 계통 전력을 추정한다.
    고정비는 평가 기간 전체 비용이며 이동 단가는 전력 제외 비용이다.
    """
    conditions.validate()
    if completed < 0 or any(not math.isfinite(v) or v < 0 for v in (battery_units, distance_m)):
        raise ValueError("측정량은 유한한 비음수여야 합니다.")
    energy = (battery_units * conditions.wh_per_battery_unit / conditions.charge_efficiency
              if conditions.wh_per_battery_unit is not None else None)
    if conditions.robot_energy_profiles:
        if robot_energy is None:
            raise ValueError("per-robot energy observations are required for robot-specific conversion")
        if any(not math.isfinite(value) or value < 0 for value in robot_energy.values()):
            raise ValueError("invalid per-robot energy observation")
        profiles = {rid: (factor, efficiency) for rid, factor, efficiency in conditions.robot_energy_profiles}
        energy = 0.0
        for rid, units in robot_energy.items():
            factor, efficiency = profiles.get(rid, (conditions.wh_per_battery_unit, conditions.charge_efficiency))
            if factor is None:
                energy = None
                break
            energy += units * factor / efficiency
    electricity = (energy / 1000 * conditions.electricity_per_kwh
                   if energy is not None and conditions.electricity_per_kwh is not None else None)
    movement = distance_m * conditions.movement_cost_per_m
    ai = conditions.ai_cost if candidate else 0.0
    total = electricity + movement + conditions.fixed_cost + ai if electricity is not None else None
    revenue = completed * conditions.revenue_per_task if conditions.revenue_per_task is not None else None
    profit = revenue - total if revenue is not None and total is not None else None
    return {
        "energy_wh": energy, "electricity_cost": electricity, "movement_cost": movement,
        "ai_cost": ai, "total_cost": total, "revenue": revenue, "net_profit": profit,
        "profit_per_task": profit / completed if profit is not None and completed else None,
        "roi": profit / total * 100 if profit is not None and total > 0 else None,
        "profit_per_wh": profit / energy if profit is not None and energy > 0 else None,
    }
