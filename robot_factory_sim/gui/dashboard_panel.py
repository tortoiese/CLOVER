"""성능 대시보드 — KPI 카드 그리드 + pyqtgraph 실시간 그래프 7탭.

스냅샷은 수신할 때마다 히스토리에 누적하고, 그래프는 CHART_REFRESH_MS 주기로 현재 탭만 다시 그린다.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsPolygonItem, QGridLayout, QLabel, QSplitter, QTabWidget, QVBoxLayout, QWidget

from gui import constants as c
from gui.widgets.kpi_card import KpiCard
from simulation.state import SimState
from utils.event_types import OrderStatus, RobotState, SchedulerModeLabel
from utils.metrics import KPI_DEFINITIONS, KpiDefinition

MAX_SAMPLES: int = int(c.HISTORY_WINDOW / c.HISTORY_SAMPLE_INTERVAL)
GANTT_BAR_HEIGHT: float = 0.7
ORDER_BAR_HEIGHT: float = 0.55
RADAR_RINGS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
RADAR_LABEL_RADIUS: float = 1.22
PRIORITY_COLORS: dict[str, str] = {"critical": c.CRITICAL, "high": c.WARNING, "normal": c.INFO, "low": c.TEXT_MUTED}


@dataclass
class GanttSegment:
    """로봇 상태 구간."""

    start: float
    end: float
    state: str
    emergency: bool


def radar_score(definition: KpiDefinition, value: float, other: float) -> float:
    """두 엔진 값을 0~1 점수로 정규화한다 (좋을수록 1).

    Args:
        definition: KPI 정의
        value: 대상 값
        other: 비교 값

    Returns:
        점수
    """
    if math.isnan(value):
        return 0.0
    if math.isnan(other):
        return 1.0
    if definition.higher_is_better:
        best = max(value, other)
        return value / best if best > 1e-9 else 1.0
    best = min(value, other)
    return best / value if value > 1e-9 else 1.0


def make_plot(parent: QWidget, x_label: str = "sim-time (분)", y_label: str = "") -> pg.PlotWidget:
    """테마를 적용한 PlotWidget.

    Args:
        parent: 부모 위젯
        x_label: X축 이름
        y_label: Y축 이름

    Returns:
        PlotWidget
    """
    plot = pg.PlotWidget(parent=parent)
    plot.setBackground(c.PANEL)
    plot.showGrid(x=True, y=True, alpha=0.15)
    plot.setMenuEnabled(False)
    if x_label:
        plot.setLabel("bottom", x_label)
    if y_label:
        plot.setLabel("left", y_label)
    for axis in ("left", "bottom"):
        plot.getAxis(axis).setPen(pg.mkPen(c.GRID))
        plot.getAxis(axis).setTextPen(pg.mkPen(c.TEXT_MUTED))
    return plot


class DashboardPanel(QWidget):
    """KPI + 그래프 대시보드."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._state: SimState | None = None
        self._reset_history()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        layout.addWidget(splitter)

        kpi_box = QWidget(splitter)
        kpi_layout = QVBoxLayout(kpi_box)
        kpi_layout.setContentsMargins(0, 0, 0, 0)
        self._kpi_title = QLabel("📊 성능 KPI", kpi_box)
        self._kpi_title.setObjectName("PanelTitle")
        kpi_layout.addWidget(self._kpi_title)
        grid = QGridLayout()
        grid.setSpacing(6)
        self._cards: dict[str, KpiCard] = {}
        for index, definition in enumerate(KPI_DEFINITIONS):
            card = KpiCard(definition, kpi_box)
            grid.addWidget(card, index // c.KPI_COLUMNS, index % c.KPI_COLUMNS)
            self._cards[definition.key] = card
        kpi_layout.addLayout(grid)
        kpi_layout.addStretch(1)

        self._tabs = QTabWidget(splitter)
        self._plots = [make_plot(self._tabs) for _ in c.CHART_TABS]
        for plot, title in zip(self._plots, c.CHART_TABS, strict=True):
            self._tabs.addTab(plot, title)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self._setup_plots()
        self._tabs.currentChanged.connect(lambda _: self._refresh_charts())

        self._timer = QTimer(self)
        self._timer.setInterval(c.CHART_REFRESH_MS)
        self._timer.timeout.connect(self._refresh_charts)
        self._timer.start()

    # ------------------------------------------------------------------
    # 히스토리
    # ------------------------------------------------------------------
    def _reset_history(self) -> None:
        """누적 히스토리를 비운다 (리셋 시)."""
        self._times: deque[float] = deque(maxlen=MAX_SAMPLES)
        self._throughput: dict[str, deque[float]] = {m: deque(maxlen=MAX_SAMPLES) for m in c.ENGINE_COLORS}
        self._queues: deque[dict[str, int]] = deque(maxlen=MAX_SAMPLES)
        self._battery: dict[str, deque[tuple[float, float]]] = {}
        self._robot_colors: dict[str, int] = {}
        self._gantt: dict[str, list[GanttSegment]] = {}
        self._last_sample = -math.inf
        self._last_time = 0.0

    def update_state(self, state: SimState) -> None:
        """스냅샷을 누적하고 KPI 카드를 갱신한다.

        Args:
            state: 시뮬레이션 상태
        """
        if state.sim_time + 1e-6 < self._last_time:
            self._reset_history()
        self._last_time = state.sim_time
        self._state = state
        self._update_gantt(state)
        if state.sim_time - self._last_sample >= c.HISTORY_SAMPLE_INTERVAL:
            self._last_sample = state.sim_time
            minutes = state.sim_time / 60.0
            self._times.append(minutes)
            for mode, values in self._throughput.items():
                values.append(state.engine_kpis.get(mode, {}).get("throughput", math.nan))
            self._queues.append({s.id: s.queue_length + s.pending_tasks for s in state.stations})
            for robot in state.robots:
                self._robot_colors[robot.id] = robot.color_index
                series = self._battery.setdefault(robot.id, deque(maxlen=MAX_SAMPLES))
                series.append((minutes, robot.battery_pct))
        self._update_cards(state)

    def _update_gantt(self, state: SimState) -> None:
        """로봇별 상태 구간을 갱신한다."""
        now = state.sim_time
        alive = set()
        for robot in state.robots:
            alive.add(robot.id)
            segments = self._gantt.setdefault(robot.id, [])
            emergency = bool(robot.emergency_order)
            if segments and segments[-1].state == robot.state and segments[-1].emergency == emergency:
                segments[-1].end = now
            else:
                if segments:
                    segments[-1].end = now
                segments.append(GanttSegment(now, now, robot.state, emergency))
            cutoff = now - c.GANTT_WINDOW
            while segments and segments[0].end < cutoff:
                segments.pop(0)
        for robot_id in [rid for rid in self._gantt if rid not in alive]:
            del self._gantt[robot_id]

    def _update_cards(self, state: SimState) -> None:
        """KPI 카드 갱신 (표시 엔진 vs 비교 엔진)."""
        other_mode = next((m for m in state.engine_kpis if m != state.mode), None)
        other = state.engine_kpis.get(other_mode, {}) if other_mode else {}
        label = SchedulerModeLabel.LABELS.get(other_mode or "", "비교")
        self._kpi_title.setText(f"📊 성능 KPI — {SchedulerModeLabel.LABELS.get(state.mode, state.mode)} 모드")
        for key, card in self._cards.items():
            card.set_values(state.kpis.get(key, math.nan), other.get(key, math.nan), label)

    # ------------------------------------------------------------------
    # 차트
    # ------------------------------------------------------------------
    def _setup_plots(self) -> None:
        """차트별 고정 요소를 만든다."""
        throughput = self._plots[0]
        throughput.setLabel("left", "처리량 (건/분)")
        throughput.addLegend(offset=(10, 10))
        self._throughput_curves = {
            mode: throughput.plot([], [], pen=pg.mkPen(color, width=2), name=SchedulerModeLabel.LABELS[mode])
            for mode, color in c.ENGINE_COLORS.items()
        }
        self._plots[1].setLabel("bottom", "sim-time (초)")
        self._plots[2].setLabel("left", "대기 작업 (건)")
        self._plots[2].addLegend(offset=(10, 10))
        self._plots[3].setLabel("left", "배터리 (%)")
        self._plots[3].addLegend(offset=(10, 10))
        self._plots[3].setYRange(0, 100)
        radar = self._plots[4]
        radar.setAspectLocked(True)
        radar.hideAxis("left")
        radar.hideAxis("bottom")
        radar.showGrid(x=False, y=False)
        radar.addLegend(offset=(10, 10))
        self._plots[5].setLabel("bottom", "주문 경과 + 예상 소요 (분)")
        self._plots[6].setLabel("bottom", "sim-time (분)")
        self._plots[6].getAxis("left").setTicks(
            [[(row, label) for row, _, _, label in c.TIMELINE_STYLE.values()]])
        self._plots[6].setYRange(-0.5, len(c.TIMELINE_STYLE) - 0.5)

    def _refresh_charts(self) -> None:
        """현재 탭 차트를 다시 그린다."""
        if self._state is None or not self.isVisible():
            return
        index = self._tabs.currentIndex()
        drawers = (self._draw_throughput, self._draw_gantt, self._draw_queue, self._draw_battery, self._draw_radar,
                   self._draw_orders, self._draw_events)
        drawers[index]()

    def _draw_throughput(self) -> None:
        """Tab 1 — Baseline vs 최적화 처리량."""
        x = np.fromiter(self._times, float)
        for mode, curve in self._throughput_curves.items():
            y = np.fromiter(self._throughput[mode], float)
            curve.setData(x, np.nan_to_num(y), connect="finite")

    def _draw_gantt(self) -> None:
        """Tab 2 — 로봇별 상태 타임라인 (긴급배정 주황 테두리)."""
        plot = self._plots[1]
        plot.clear()
        robots = sorted(self._gantt)
        by_state: dict[str, tuple[list[float], list[float], list[float]]] = {}
        emergency: tuple[list[float], list[float], list[float]] = ([], [], [])
        for row, robot_id in enumerate(robots):
            for seg in self._gantt[robot_id]:
                width = max(0.2, seg.end - seg.start)
                bucket = by_state.setdefault(seg.state, ([], [], []))
                bucket[0].append(seg.start)
                bucket[1].append(width)
                bucket[2].append(row)
                if seg.emergency:
                    emergency[0].append(seg.start)
                    emergency[1].append(width)
                    emergency[2].append(row)
        for state, (x0, width, y) in by_state.items():
            color = c.ROBOT_STATE_COLORS.get(state, c.TEXT_MUTED)
            plot.addItem(pg.BarGraphItem(x0=x0, width=width, y=y, height=GANTT_BAR_HEIGHT, brush=color, pen=None))
        if emergency[0]:
            plot.addItem(pg.BarGraphItem(x0=emergency[0], width=emergency[1], y=emergency[2],
                                         height=GANTT_BAR_HEIGHT + 0.15, brush=None,
                                         pen=pg.mkPen(c.EMERGENCY_COLOR, width=2)))
        plot.getAxis("left").setTicks([[(i, rid) for i, rid in enumerate(robots)]])
        if self._state is not None:
            plot.setXRange(max(0.0, self._state.sim_time - c.GANTT_WINDOW), max(c.GANTT_WINDOW, self._state.sim_time),
                           padding=0.01)
        plot.setYRange(-0.6, max(1, len(robots)) - 0.4)
        legend = "  ".join(f"<span style='color:{c.ROBOT_STATE_COLORS[s]}'>■</span> {RobotState.LABELS[s]}"
                           for s in (RobotState.MOVING, RobotState.WORKING, RobotState.CHARGING, RobotState.IDLE,
                                     RobotState.FAULT))
        plot.setTitle(legend + f"  <span style='color:{c.EMERGENCY_COLOR}'>□</span> 긴급배정", size="8pt")

    def _draw_queue(self) -> None:
        """Tab 3 — 스테이션별 대기 작업 누적 영역."""
        plot = self._plots[2]
        plot.clear()
        if not self._queues:
            return
        stations = sorted({sid for sample in self._queues for sid in sample})
        x = np.fromiter(self._times, float)
        cumulative = np.zeros(len(x))
        layers = []
        for station_id in stations:
            cumulative = cumulative + np.array([sample.get(station_id, 0) for sample in self._queues], float)
            layers.append((station_id, cumulative.copy()))
        for index, (station_id, values) in reversed(list(enumerate(layers))):
            color = QColor(c.ROBOT_PALETTE[index % len(c.ROBOT_PALETTE)])
            fill = QColor(color)
            fill.setAlpha(150)
            plot.plot(x, values, pen=pg.mkPen(color, width=1), fillLevel=0, brush=fill, name=station_id)

    def _draw_battery(self) -> None:
        """Tab 4 — 로봇별 배터리 추이."""
        plot = self._plots[3]
        plot.clear()
        for robot_id in sorted(self._battery):
            series = self._battery[robot_id]
            if not series:
                continue
            x, y = zip(*series, strict=True)
            color = c.ROBOT_PALETTE[self._robot_colors.get(robot_id, 0) % len(c.ROBOT_PALETTE)]
            plot.plot(list(x), list(y), pen=pg.mkPen(color, width=1.5), name=robot_id)

    def _draw_radar(self) -> None:
        """Tab 5 — KPI 종합 비교 레이더."""
        plot = self._plots[4]
        plot.clear()
        state = self._state
        assert state is not None
        n = len(KPI_DEFINITIONS)
        angles = [math.pi / 2 - 2 * math.pi * i / n for i in range(n)]
        grid_pen = pg.mkPen(c.GRID, width=1)
        for ring in RADAR_RINGS:
            xs = [ring * math.cos(a) for a in angles] + [ring * math.cos(angles[0])]
            ys = [ring * math.sin(a) for a in angles] + [ring * math.sin(angles[0])]
            plot.plot(xs, ys, pen=grid_pen)
        font = QFont()
        font.setPointSize(8)
        for angle, definition in zip(angles, KPI_DEFINITIONS, strict=True):
            plot.plot([0, math.cos(angle)], [0, math.sin(angle)], pen=grid_pen)
            label = pg.TextItem(definition.label, color=c.TEXT_MUTED, anchor=(0.5, 0.5))
            label.setFont(font)
            label.setPos(RADAR_LABEL_RADIUS * math.cos(angle), RADAR_LABEL_RADIUS * math.sin(angle))
            plot.addItem(label)
        modes = list(c.ENGINE_COLORS)
        for mode in modes:
            other_mode = next(m for m in modes if m != mode)
            values = state.engine_kpis.get(mode, {})
            others = state.engine_kpis.get(other_mode, {})
            scores = [radar_score(d, values.get(d.key, math.nan), others.get(d.key, math.nan)) for d in KPI_DEFINITIONS]
            xs = [s * math.cos(a) for s, a in zip(scores, angles, strict=True)]
            ys = [s * math.sin(a) for s, a in zip(scores, angles, strict=True)]
            color = QColor(c.ENGINE_COLORS[mode])
            fill = QColor(color)
            fill.setAlpha(60)
            item = pg.PlotDataItem(xs + xs[:1], ys + ys[:1], pen=pg.mkPen(color, width=2),
                                   name=SchedulerModeLabel.LABELS[mode])
            plot.addItem(item)
            polygon = QGraphicsPolygonItem(QPolygonF([QPointF(x, y) for x, y in zip(xs, ys, strict=True)]))
            polygon.setBrush(fill)
            polygon.setPen(QPen(Qt.PenStyle.NoPen))
            plot.addItem(polygon)
        plot.setXRange(-1.45, 1.45)
        plot.setYRange(-1.35, 1.35)

    def _draw_orders(self) -> None:
        """Tab 6 — 주문 진행률 (경과 + 예상 소요, 빨간 선 = 납기)."""
        plot = self._plots[5]
        plot.clear()
        state = self._state
        assert state is not None
        orders = state.orders
        if not orders:
            text = pg.TextItem("등록된 주문 없음", color=c.TEXT_MUTED, anchor=(0.5, 0.5))
            plot.addItem(text)
            text.setPos(0.5, 0.5)
            return
        ticks = []
        max_x = 1.0
        for row, order in enumerate(orders):
            elapsed = order.elapsed / 60.0
            eta = 0.0 if math.isinf(order.eta_seconds) else order.eta_seconds / 60.0
            color = QColor(PRIORITY_COLORS.get(order.priority, c.INFO))
            if order.status == OrderStatus.COMPLETED:
                color = QColor(c.SUCCESS)
            elif order.status == OrderStatus.CANCELLED:
                color = QColor(c.TEXT_MUTED)
            plot.addItem(pg.BarGraphItem(x0=[0], width=[max(0.05, elapsed)], y=[row], height=ORDER_BAR_HEIGHT,
                                         brush=color, pen=None))
            if order.status in OrderStatus.ACTIVE and eta > 0:
                faded = QColor(color)
                faded.setAlpha(70)
                plot.addItem(pg.BarGraphItem(x0=[elapsed], width=[eta], y=[row], height=ORDER_BAR_HEIGHT, brush=faded,
                                             pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine)))
            max_x = max(max_x, elapsed + eta)
            if order.deadline_remaining is not None:
                deadline_x = elapsed + order.deadline_remaining / 60.0
                max_x = max(max_x, deadline_x)
                plot.plot([deadline_x, deadline_x], [row - 0.45, row + 0.45], pen=pg.mkPen(c.CRITICAL, width=3))
            pct = order.completed / max(1, order.quantity) * 100.0
            risk = " ⚠" if order.at_risk else ""
            ticks.append((row, f"{order.order_id.replace('ORD-', '')} {pct:.0f}%{risk}"))
        plot.getAxis("left").setTicks([ticks])
        plot.setYRange(-0.6, len(orders) - 0.4)
        plot.setXRange(0, max_x * 1.05)

    def _draw_events(self) -> None:
        """Tab 7 — 이벤트 타임라인 (🔴⚙✅⚫ 마커, 마우스를 올리면 내용 표시)."""
        plot = self._plots[6]
        plot.clear()
        state = self._state
        assert state is not None
        spots = []
        for event in state.events:
            row, color, symbol, _ = c.TIMELINE_STYLE.get(event.kind, c.TIMELINE_STYLE["info"])
            spots.append({"pos": (event.sim_time / 60.0, row), "brush": pg.mkBrush(color), "symbol": symbol,
                          "size": 11, "pen": pg.mkPen(color), "data": event.message})
        scatter = pg.ScatterPlotItem(hoverable=True, hoverSize=15,
                                     tip=lambda x, y, data: f"T+{x:.1f}분\n{data}")
        scatter.addPoints(spots)
        plot.addItem(scatter)
        end = max(1.0, state.sim_time / 60.0)
        plot.setXRange(max(0.0, end - c.HISTORY_WINDOW / 60.0), end * 1.02)
