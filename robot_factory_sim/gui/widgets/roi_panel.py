"""RoiPanel — ROI 비교(베이스라인 vs 최적화안) 2x2 막대 그래프.

utils.roi.compute_roi()로 계산한 ROIResult 두 개를 받아 전력 소비량 · 총투입비용 ·
총순이익 · ROI(%)를 베이스라인/최적화안 막대로 나란히 비교한다.
"""

from __future__ import annotations

import pyqtgraph as pg
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QWidget

from gui import constants as c
from utils.roi import ROIResult

# (키, 제목, 단위, 높을수록 좋은지)
_PANELS: tuple[tuple[str, str, str, bool], ...] = (
    ("energy_wh", "전력 소비량", "Wh", False),
    ("total_cost", "총투입비용", "원", False),
    ("net_profit", "총순이익", "원", True),
    ("roi_pct", "ROI", "%", True),
)

_TICKS: list[tuple[float, str]] = [(0, "베이스라인"), (1, "최적화안")]


class RoiPanel(QWidget):
    """ROI 결과 2x2 비교 차트."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        layout = QGridLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)
        self._plots: dict[str, pg.PlotWidget] = {}
        for index, (key, title, unit, _) in enumerate(_PANELS):
            plot = pg.PlotWidget(parent=self)
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

    def update_roi(self, baseline: ROIResult, optimized: ROIResult) -> None:
        """베이스라인/최적화안 ROI 결과로 4개 그래프를 다시 그린다.

        Args:
            baseline: 현행(baseline) ROI 결과
            optimized: 최적화안 ROI 결과
        """
        base = baseline.to_dict()
        opt = optimized.to_dict()
        for key, title, unit, higher_better in _PANELS:
            plot = self._plots[key]
            plot.clear()
            b, o = base[key], opt[key]
            plot.addItem(pg.BarGraphItem(x=[0], height=[b], width=0.6, brush=QColor(c.ENGINE_COLORS["baseline"]),
                                         pen=None))
            plot.addItem(pg.BarGraphItem(x=[1], height=[o], width=0.6, brush=QColor(c.ENGINE_COLORS["optimized"]),
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
