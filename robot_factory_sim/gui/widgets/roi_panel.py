"""RoiPanel — ROI 파라미터 입력 + 숫자 요약 + 베이스라인 vs 최적화안 2x2 막대 그래프.

ROI 관련 화면은 전부 이 탭 하나에 모은다:
- 파라미터(사용자 입력, 조절 가능): 소비효율(Wh/m)·전력 단가·기타 비용·건당 가치. config 기본값으로 시작한다.
- 완료 건수(Q)·로봇 총 이동거리는 시뮬레이션이 자동 수집해 ``update_kpis()``로 들어온다.
- 숫자 요약(KpiCard 재사용)과 차트는 모두 같은 compute_roi() 결과로 다시 그려 항상 일치한다.
"""

from __future__ import annotations

import json
import math

import pyqtgraph as pg
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.widgets.kpi_card import KpiCard
from utils.comparison_models import ComparisonConditions, calculate_economics
from utils.event_types import SchedulerModeLabel
from utils.metrics import KpiDefinition
from utils.roi import ROIResult, compare

# (키, 제목, 단위, 높을수록 좋은지) — 차트 4종
_PANELS: tuple[tuple[str, str, str, bool], ...] = (
    ("energy_wh", "전력 소비량", "Wh", False),
    ("total_cost", "총투입비용", "원", False),
    ("net_profit", "총순이익", "원", True),
    ("roi_pct", "ROI", "%", True),
)
_TICKS: list[tuple[float, str]] = [(0, "베이스라인"), (1, "최적화안")]

# 숫자 요약 카드 5종 (KpiCard 재사용)
_CARD_DEFS: tuple[KpiDefinition, ...] = (
    KpiDefinition("total_cost", "총투입비용", "원", False, 0),
    KpiDefinition("net_profit", "총순이익", "원", True, 0),
    KpiDefinition("profit_per_unit", "건당 순이익", "원", True, 0),
    KpiDefinition("roi_pct", "ROI", "%", True, 1),
    KpiDefinition("profit_per_wh", "전력대비 순이익", "원/Wh", True, 1),
)


def _spin(value: float, minimum: float, maximum: float, step: float, decimals: int = 3,
          parent: QWidget | None = None) -> QDoubleSpinBox:
    """ROI 파라미터 입력용 스핀박스 (config 기본값으로 시작)."""
    spin = QDoubleSpinBox(parent)
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setKeyboardTracking(False)
    return spin


class RoiPanel(QWidget):
    """ROI 파라미터 + 숫자 요약 + 비교 차트."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self._base_q = 0
        self._base_d = 0.0
        self._opt_q = 0
        self._opt_d = 0.0
        self._base_units = self._opt_units = 0.0
        self._base_robot_energy = self._opt_robot_energy = None
        self._candidate_id = "optimized"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(8)
        outer.addWidget(self._build_params())
        self._robot_profiles = QLineEdit("{}", self)
        self._robot_profiles.setPlaceholderText('로봇별 환산 선택 입력: {"R0": [2.0, 0.9]}')
        self._robot_profiles.editingFinished.connect(self._recompute)
        outer.addWidget(self._robot_profiles)
        outer.addWidget(self._build_summary())
        outer.addWidget(self._build_charts(), 1)

        self._recompute()

    # ------------------------------------------------------------------
    # 구성
    # ------------------------------------------------------------------
    def _build_params(self) -> QWidget:
        """사용자 입력 파라미터(소비효율·전력 단가·기타 비용·건당 가치) 영역."""
        box = QGroupBox("ROI 파라미터 (조정 가능 — 기본값은 config.py)", self)
        layout = QGridLayout(box)
        self._wh_per_unit = _spin(-1, -1, 100000, .01, parent=box)
        self._power_price = _spin(-1, -1, 100000, 1, parent=box)
        self._other_cost = _spin(0, 0, 10_000_000, 1000, decimals=0, parent=box)
        self._revenue_per_unit = _spin(-1, -1, 1_000_000, 100, decimals=0, parent=box)
        self._efficiency = _spin(.9, .001, 1, .01, parent=box)
        self._move_cost = _spin(0, 0, 100000, .1, parent=box)
        self._policy_cost = _spin(0, 0, 10000000, 1, parent=box)
        for spin in (self._wh_per_unit, self._power_price, self._revenue_per_unit):
            spin.setSpecialValueText("미입력")
        for index, (label, spin, unit) in enumerate((
            ("배터리 환산", self._wh_per_unit, "Wh/단위"),
            ("전력 단가", self._power_price, "원/kWh"),
            ("충전 효율", self._efficiency, ""),
            ("기타 비용", self._other_cost, "원"),
            ("건당 가치", self._revenue_per_unit, "원"),
            ("이동 운영비", self._move_cost, "원/m"),
            ("선택 정책 추가비", self._policy_cost, "원"),
        )):
            row, column = index // 3, (index % 3) * 3
            layout.addWidget(QLabel(label, box), row, column)
            layout.addWidget(spin, row, column + 1)
            layout.addWidget(QLabel(unit, box), row, column + 2)
            spin.valueChanged.connect(self._recompute)
        return box

    def _build_summary(self) -> QWidget:
        """숫자 요약 카드(5종, KpiCard 재사용) + baseline 대비 개선 요약 라벨."""
        box = QWidget(self)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        cards_row = QHBoxLayout()
        self._cards: dict[str, KpiCard] = {}
        for definition in _CARD_DEFS:
            card = KpiCard(definition, box)
            cards_row.addWidget(card)
            self._cards[definition.key] = card
        layout.addLayout(cards_row)
        self._summary = QLabel("—", box)
        layout.addWidget(self._summary)
        return box

    def _build_charts(self) -> QWidget:
        """베이스라인 vs 최적화안 2x2 막대 그래프 영역."""
        box = QWidget(self)
        layout = QGridLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self._plots: dict[str, pg.PlotWidget] = {}
        for index, (key, title, unit, _) in enumerate(_PANELS):
            plot = pg.PlotWidget(parent=box)
            plot.setBackground(c.PANEL)
            plot.showGrid(y=True, alpha=0.15)
            plot.setMenuEnabled(False)
            plot.setTitle(title, color=c.TEXT_MUTED, size="10pt")
            plot.setLabel("left", unit)
            plot.getAxis("bottom").setTicks([_TICKS])
            for axis in ("left", "bottom"):
                plot.getAxis(axis).setPen(pg.mkPen(c.GRID))
                plot.getAxis(axis).setTextPen(pg.mkPen(c.TEXT_MUTED))
            layout.addWidget(plot, index // 2, index % 2)
            self._plots[key] = plot
        return box

    # ------------------------------------------------------------------
    # 갱신
    # ------------------------------------------------------------------
    def update_kpis(self, base_completed: int, base_distance: float, opt_completed: int, opt_distance: float,
                    base_units: float = 0, opt_units: float = 0,
                    base_robot_energy: dict[str, float] | None = None,
                    opt_robot_energy: dict[str, float] | None = None, candidate_id: str = "optimized") -> None:
        """시뮬레이션이 자동 수집한 값(완료 건수 Q·로봇 총 이동거리)으로 ROI를 다시 계산한다.

        Args:
            base_completed: baseline 완료 생산 건수
            base_distance: baseline 로봇 총 이동거리 (m)
            opt_completed: optimized 완료 생산 건수
            opt_distance: optimized 로봇 총 이동거리 (m)
        """
        self._base_q, self._base_d = base_completed, base_distance
        self._opt_q, self._opt_d = opt_completed, opt_distance
        self._base_units, self._opt_units = base_units, opt_units
        self._base_robot_energy, self._opt_robot_energy = base_robot_energy, opt_robot_energy
        self._candidate_id = candidate_id
        self._recompute()

    def _recompute(self) -> None:
        """현재 파라미터 + 최근 수집된 완료 건수·이동거리로 ROI를 계산하고 화면을 갱신한다."""
        optional = lambda spin: None if spin.value() < 0 else spin.value()
        try:
            profiles = json.loads(self._robot_profiles.text())
            ComparisonConditions(robot_energy_profiles=profiles).validate()
        except (ValueError, TypeError):
            self._summary.setText("로봇별 환산 JSON을 확인하세요. 이전 계산값은 갱신되지 않았습니다.")
            return
        conditions = ComparisonConditions(wh_per_battery_unit=optional(self._wh_per_unit),
                                          electricity_per_kwh=optional(self._power_price),
                                          revenue_per_task=optional(self._revenue_per_unit),
                                          charge_efficiency=self._efficiency.value(), fixed_cost=self._other_cost.value(),
                                          movement_cost_per_m=self._move_cost.value(), ai_cost=self._policy_cost.value(),
                                          robot_energy_profiles=profiles)

        def result(quantity: int, units: float, distance: float, by_robot: dict | None, proposed: bool) -> ROIResult:
            values = calculate_economics(quantity, units, distance, conditions, proposed, by_robot)
            aliases = ("energy_wh", "electricity_cost", "fixed_cost", "total_cost", "revenue", "net_profit",
                       "profit_per_task", "roi", "profit_per_wh")
            values["fixed_cost"] = conditions.fixed_cost
            return ROIResult(*(values[key] if values[key] is not None else math.nan for key in aliases))

        try:
            baseline = result(self._base_q, self._base_units, self._base_d, self._base_robot_energy, False)
            optimized = result(self._opt_q, self._opt_units, self._opt_d, self._opt_robot_energy, True)
        except (ValueError, TypeError) as exc:
            self._summary.setText(str(exc))
            return
        for key, card in self._cards.items():
            card.set_values(getattr(optimized, key), getattr(baseline, key), "베이스라인")
        delta = compare(baseline, optimized)
        profit_delta = delta["net_profit"]["delta"]
        roi_delta = delta["roi_pct"]["delta"]  # roi_pct가 이미 %이므로 delta는 %p
        arrow = "▲" if profit_delta >= 0 else "▼"
        color = c.SUCCESS if profit_delta >= 0 else c.CRITICAL
        self._summary.setText(
            f"<span style='color:{color}'>{arrow} {SchedulerModeLabel.LABELS.get(self._candidate_id, self._candidate_id)} · FIFO 대비 "
            f"순이익 {profit_delta:+,.0f}원, ROI {roi_delta:+.1f}%p</span>"
        )
        if not math.isfinite(profit_delta) or not math.isfinite(roi_delta):
            self._summary.setText("미입력 단가는 ‘미입력’으로 표시합니다. 알려진 0원은 0으로 입력하세요. "
                                  "배터리 소모 × Wh/단위 ÷ 충전효율을 사용합니다.")
        self._draw_charts(baseline, optimized)

    def _draw_charts(self, baseline: ROIResult, optimized: ROIResult) -> None:
        """베이스라인/최적화안 ROIResult로 2x2 막대 그래프를 다시 그린다."""
        base = baseline.to_dict()
        opt = optimized.to_dict()
        for key, title, unit, higher_better in _PANELS:
            plot = self._plots[key]
            plot.clear()
            plot.getAxis("bottom").setTicks([[(0, "FIFO"), (1, SchedulerModeLabel.LABELS.get(self._candidate_id, self._candidate_id))]])
            b, o = base[key], opt[key]
            if not math.isfinite(b) or not math.isfinite(o):
                plot.setTitle(title + " · 미산정", color=c.TEXT_MUTED, size="10pt")
                continue
            plot.addItem(pg.BarGraphItem(x=[0], height=[b], width=0.6, brush=QColor(c.ENGINE_COLORS["baseline"]),
                                         pen=None))
            plot.addItem(pg.BarGraphItem(x=[1], height=[o], width=0.6, brush=QColor(c.ENGINE_COLORS.get(self._candidate_id, c.HIGHLIGHT)),
                                         pen=None))
            top = max(b, o, 1e-9)
            for x, val in ((0, b), (1, o)):
                text = pg.TextItem(f"{val:,.0f}", color=c.TEXT, anchor=(0.5, 1.0))
                text.setPos(x, val + top * 0.03)
                plot.addItem(text)
            if abs(b) > 1e-9:
                pct = (o - b) / b * 100.0
                improved = (o > b) if higher_better else (o < b)
                title_color = c.SUCCESS if improved else c.CRITICAL
                plot.setTitle(f"{title}  ({pct:+.1f}%)", color=title_color, size="10pt")
            else:
                plot.setTitle(title, color=c.TEXT_MUTED, size="10pt")
            plot.setXRange(-0.6, 1.6)
            plot.setYRange(min(0.0, b, o), top * 1.3)
