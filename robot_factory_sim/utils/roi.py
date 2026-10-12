"""ROI / 수익성 계산.

시뮬레이션 결과(완료 생산 건수·로봇 이동거리)와 비용 파라미터로
운영안의 수익성 지표(총투입비용·총순이익·건당 순이익·ROI·전력 대비 순이익)를 계산한다.

투입비의 중심은 전력량(에너지)이며, 전력량 = 이동거리 × 소비효율(Wh/m)로 추정한다.
(10/7 회의록 산식 기준. 비용 파라미터 fallback은 config에 있으며 기업 데이터 수령 시 교체.)

입력값은 6개뿐이다: 이동거리 d, 소비효율 e(Wh/m), 전력 단가 p,
완료 건수 Q, 건당 가치 v, 기타 비용(AI·로봇운영 등). 나머지는 자동 계산된다.
"""

from __future__ import annotations

from dataclasses import dataclass

import config


@dataclass(frozen=True)
class ROIResult:
    """ROI 계산 결과. 금액 단위는 원(KRW), 전력은 Wh."""

    energy_wh: float          # 전력 소비량 E = d × e
    power_cost: float         # 전력 비용 = E × 전력 단가
    other_cost: float         # 기타 비용 (AI + 로봇운영 등)
    total_cost: float         # 총투입비용 = 전력 비용 + 기타 비용
    revenue: float            # 총매출 = 완료 건수 × 건당 가치
    net_profit: float         # 총순이익 = 총매출 − 총투입비용
    profit_per_unit: float    # 건당 순이익 = 총순이익 ÷ 완료 건수
    roi_pct: float            # ROI(%) = 총순이익 ÷ 총투입비용 × 100
    profit_per_wh: float      # 전력 대비 순이익 = 총순이익 ÷ 전력 소비량(Wh)

    def to_dict(self) -> dict[str, float]:
        """표시/직렬화용 dict."""
        return {
            "energy_wh": self.energy_wh,
            "power_cost": self.power_cost,
            "other_cost": self.other_cost,
            "total_cost": self.total_cost,
            "revenue": self.revenue,
            "net_profit": self.net_profit,
            "profit_per_unit": self.profit_per_unit,
            "roi_pct": self.roi_pct,
            "profit_per_wh": self.profit_per_wh,
        }


def compute_roi(
    completed_units: int,
    total_distance_m: float,
    *,
    wh_per_m: float = config.WH_PER_M,
    power_price_per_wh: float = config.POWER_PRICE_PER_WH,
    other_cost: float = config.OTHER_COST_KRW,
    revenue_per_unit: float = config.REVENUE_PER_UNIT_KRW,
    energy_wh: float | None = None,
) -> ROIResult:
    """수익성 지표를 계산한다.

    전력량은 ``energy_wh``가 주어지면 그 값을, 없으면
    ``이동거리 × 소비효율(Wh/m)``로 추정한다.

    Args:
        completed_units: 완료 생산 건수 (Q)
        total_distance_m: 로봇 총 이동거리 (m, d)
        wh_per_m: 소비효율 e (Wh/m) = 배터리용량 ÷ 완충 주행거리
        power_price_per_wh: 전력 단가 (원/Wh, p)
        other_cost: 기타 비용 (AI·로봇운영 등, 원)
        revenue_per_unit: 건당 매출/가치 (원, v)
        energy_wh: 전력 소비량(Wh)을 직접 지정 (없으면 거리로 추정)

    Returns:
        ROIResult
    """
    if energy_wh is None:
        energy_wh = max(0.0, total_distance_m) * wh_per_m

    power_cost = energy_wh * power_price_per_wh
    total_cost = power_cost + other_cost
    revenue = max(0, completed_units) * revenue_per_unit
    net_profit = revenue - total_cost
    profit_per_unit = net_profit / completed_units if completed_units > 0 else 0.0
    roi_pct = net_profit / total_cost * 100.0 if total_cost > 0 else 0.0
    profit_per_wh = net_profit / energy_wh if energy_wh > 0 else 0.0

    return ROIResult(
        energy_wh=energy_wh,
        power_cost=power_cost,
        other_cost=other_cost,
        total_cost=total_cost,
        revenue=revenue,
        net_profit=net_profit,
        profit_per_unit=profit_per_unit,
        roi_pct=roi_pct,
        profit_per_wh=profit_per_wh,
    )


def compare(baseline: ROIResult, optimized: ROIResult) -> dict[str, dict[str, float]]:
    """베이스라인 대비 최적화안의 변화를 계산한다.

    Args:
        baseline: 현행(baseline) 결과
        optimized: 최적화안 결과

    Returns:
        지표 → {baseline, optimized, delta, pct} (pct는 절감률/증가율 %)
    """
    out: dict[str, dict[str, float]] = {}
    for key in baseline.to_dict():
        b = getattr(baseline, key)
        o = getattr(optimized, key)
        pct = (o - b) / b * 100.0 if b not in (0, 0.0) else 0.0
        out[key] = {"baseline": b, "optimized": o, "delta": o - b, "pct": pct}
    return out


if __name__ == "__main__":
    # 데모: 같은 작업량에서 최적화안이 이동거리를 줄여 전력·비용을 절감한 경우
    base = compute_roi(completed_units=500, total_distance_m=20000)
    opt = compute_roi(completed_units=510, total_distance_m=16500)
    print("[baseline ]", {k: round(v, 2) for k, v in base.to_dict().items()})
    print("[optimized]", {k: round(v, 2) for k, v in opt.to_dict().items()})
    print("ROI baseline  : %.1f%%" % base.roi_pct)
    print("ROI optimized : %.1f%%" % opt.roi_pct)
    diff = compare(base, opt)
    print("전력량 변화   : %.1f%%" % diff["energy_wh"]["pct"])
    print("순이익 변화   : %+.0f원" % diff["net_profit"]["delta"])
