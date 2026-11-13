"""성능 대시보드 — KPI 카드 그리드 + pyqtgraph 실시간 그래프 7탭.

스냅샷은 수신할 때마다 히스토리에 누적하고, 그래프는 CHART_REFRESH_MS 주기로 현재 탭만 다시 그린다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsPolygonItem,
    QGridLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.widgets.elided_label import ElidedLabel
from gui.widgets.kpi_card import KpiCard
from gui.widgets.roi_panel import RoiPanel
from simulation.state import SimState, TimelineEvent
from utils.event_types import OrderStatus, RobotState, SchedulerModeLabel
from utils.metrics import KPI_DEFINITIONS, KpiDefinition

GANTT_BAR_HEIGHT: float = 0.7
ORDER_BAR_HEIGHT: float = 0.55
RADAR_RINGS: tuple[float, ...] = (0.25, 0.5, 0.75, 1.0)
RADAR_LABEL_RADIUS: float = 1.22
FEATURED_METRICS: tuple[str, ...] = ("throughput", "avg_wait")
PRIORITY_COLORS: dict[str, str] = {"critical": c.CRITICAL, "high": c.WARNING, "normal": c.INFO, "low": c.TEXT_MUTED}


@dataclass
class GanttSegment:
    """로봇 상태 구간."""

    start: float
    end: float
    state: str
    emergency: bool


@dataclass
class HistorySample:
    """같은 시각에 수집한 그래프 원천 데이터."""

    minute: float
    throughput: dict[str, float]
    queues: dict[str, int]
    batteries: dict[str, float]


LIVE_METRICS = (*KPI_DEFINITIONS, KpiDefinition("completed_units", "완료 작업", "건", True, 0),
                KpiDefinition("distance_m", "로봇 이동거리", "m", False))


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


def make_plot(parent: QWidget, x_label: str = "시뮬레이션 시간 (분)", y_label: str = "") -> pg.PlotWidget:
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
    plot.setMouseEnabled(x=False, y=False)
    plot.hideButtons()
    if x_label:
        plot.setLabel("bottom", x_label)
    if y_label:
        plot.setLabel("left", y_label)
    for axis in ("left", "bottom"):
        scale = plot.getAxis(axis)
        scale.setPen(pg.mkPen(c.GRID))
        scale.setTextPen(pg.mkPen(c.TEXT_MUTED))
        scale.enableAutoSIPrefix(False)
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
        title = QLabel("운영 성과", kpi_box)
        title.setObjectName("PanelTitle")
        kpi_layout.addWidget(title)
        self._comparison_caption = ElidedLabel("표시 정책 확인 중", kpi_box)
        self._comparison_caption.setObjectName("Muted")
        kpi_layout.addWidget(self._comparison_caption)

        featured = tuple(definition for definition in KPI_DEFINITIONS if definition.key in FEATURED_METRICS)
        details = tuple(definition for definition in KPI_DEFINITIONS if definition.key not in FEATURED_METRICS)
        self._cards: dict[str, KpiCard] = {}
        kpi_layout.addWidget(self._build_metrics(kpi_box, featured, len(FEATURED_METRICS), featured=True))
        toggle = QToolButton(kpi_box)
        toggle.setObjectName("KpiDetailsToggle")
        toggle.setText("세부 지표")
        toggle.setCheckable(True)
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        kpi_layout.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        detail_scroll = QScrollArea(kpi_box)
        detail_scroll.setObjectName("KpiDetails")
        detail_scroll.setFixedHeight(c.KPI_DETAILS_HEIGHT)
        detail_scroll.setWidgetResizable(True)
        detail_panel = self._build_metrics(detail_scroll, details, c.KPI_COLUMNS)
        detail_panel.setObjectName("KpiDetailContent")
        detail_scroll.setWidget(detail_panel)
        detail_scroll.hide()
        toggle.toggled.connect(detail_scroll.setVisible)
        toggle.toggled.connect(lambda shown: toggle.setArrowType(Qt.ArrowType.DownArrow if shown
                                                                 else Qt.ArrowType.RightArrow))
        kpi_layout.addWidget(detail_scroll)
        kpi_layout.addStretch(1)

        self._tabs = QTabWidget(splitter)
        self._tabs.setUsesScrollButtons(True)
        self._plots = [make_plot(self._tabs) for _ in c.CHART_TABS]
        for plot, title in zip(self._plots, c.CHART_TABS, strict=True):
            self._tabs.addTab(plot, title)
        self._roi_panel = RoiPanel(self._tabs)
        self._tabs.addTab(self._roi_panel, "ROI 비교")
        self._planning_view = QPlainTextEdit(self._tabs)
        self._planning_view.setReadOnly(True)
        self._tabs.addTab(self._planning_view, "계획·예약")
        self._metrics = QTableWidget(len(LIVE_METRICS), 4, self._tabs)
        self._metrics.setHorizontalHeaderLabels(("지표", "라운드로빈", "가중합", "차이"))
        self._metrics.verticalHeader().hide()
        self._metrics.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._metrics.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._metrics.setAlternatingRowColors(True)
        self._tabs.insertTab(0, self._metrics, "지표")
        self._tabs.setCurrentWidget(self._plots[0])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self._setup_plots()
        self._tabs.currentChanged.connect(lambda _: self._refresh_charts())

        self._timer = QTimer(self)
        self._timer.setInterval(c.CHART_REFRESH_MS)
        self._timer.timeout.connect(self._refresh_charts)
        self._timer.start()

    def _build_metrics(self, parent: QWidget, definitions: tuple[KpiDefinition, ...], columns: int,
                       *, featured: bool = False) -> QWidget:
        panel = QWidget(parent)
        grid = QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for column in range(columns):
            grid.setColumnStretch(column, 1)
        for index, definition in enumerate(definitions):
            card = KpiCard(definition, panel, featured=featured)
            row, column = divmod(index, columns)
            grid.addWidget(card, row, column)
            self._cards[definition.key] = card
        return panel

    # ------------------------------------------------------------------
    # 히스토리
    # ------------------------------------------------------------------
    def _reset_history(self) -> None:
        """누적 히스토리를 비운다 (리셋 시)."""
        self._history: list[HistorySample] = []
        self._sample_interval = c.HISTORY_SAMPLE_INTERVAL
        self._robot_colors: dict[str, int] = {}
        self._gantt: dict[str, list[GanttSegment]] = {}
        self._active_robot_ids: set[str] = set()
        self._events: dict[str, list[TimelineEvent]] = {}
        self._last_events: dict[str, TimelineEvent] = {}
        self._throughput_top = c.THROUGHPUT_START_RANGE
        self._queue_top = 5.0
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
        self._update_events(state)
        if state.sim_time - self._last_sample >= self._sample_interval:
            self._last_sample = state.sim_time
            if len(self._history) >= c.HISTORY_MAX_POINTS:
                self._history = self._history[::2]
                self._sample_interval *= 2
            self._history.append(HistorySample(
                state.sim_time / 60.0,
                {mode: state.engine_kpis.get(mode, {}).get("throughput", math.nan) for mode in c.ENGINE_COLORS},
                {s.id: s.queue_length + s.pending_tasks for s in state.stations},
                {robot.id: robot.battery_pct for robot in state.robots},
            ))
            for robot in state.robots:
                self._robot_colors[robot.id] = robot.color_index
        self._update_cards(state)

    def _update_gantt(self, state: SimState) -> None:
        """로봇별 상태 구간을 갱신한다."""
        now = state.sim_time
        active = {robot.id for robot in state.robots}
        for robot_id in self._active_robot_ids - active:
            self._gantt[robot_id][-1].end = now
        for robot in state.robots:
            segments = self._gantt.setdefault(robot.id, [])
            emergency = bool(robot.emergency_order)
            present = robot.id in self._active_robot_ids
            if present and segments[-1].state == robot.state and segments[-1].emergency == emergency:
                segments[-1].end = now
            else:
                if present:
                    segments[-1].end = now
                segments.append(GanttSegment(now, now, robot.state, emergency))
        self._active_robot_ids = active

    def _update_events(self, state: SimState) -> None:
        """새 이벤트를 보존한다. 엔진은 기존 이벤트 객체를 스냅샷에 재사용한다."""
        if not state.events or state.events[-1] is self._last_events.get(state.mode):
            return
        previous = self._last_events.get(state.mode)
        start = next((i + 1 for i in range(len(state.events) - 1, -1, -1) if state.events[i] is previous), 0)
        archive = self._events.setdefault(state.mode, [])
        archive.extend(state.events[start:])
        if len(archive) > c.HISTORY_MAX_POINTS:
            archive[:] = archive[:-1:2] + archive[-1:]
        self._last_events[state.mode] = state.events[-1]

    def _update_cards(self, state: SimState) -> None:
        """KPI 카드 갱신 (표시 엔진 vs 비교 엔진)."""
        other_mode = next((m for m in state.engine_kpis if m != state.mode), None)
        other = state.engine_kpis.get(other_mode, {}) if other_mode else {}
        label = SchedulerModeLabel.LABELS.get(other_mode or "", "비교")
        mode = SchedulerModeLabel.LABELS.get(state.mode, state.mode)
        caption = f"표시 {mode}"
        if other_mode:
            caption += f"  /  비교 {label}"
        self._comparison_caption.setText(caption)
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
        self._plots[1].setLabel("bottom", "시뮬레이션 시간 (초)")
        self._plots[2].setLabel("left", "대기 작업 (건)")
        self._plots[2].addLegend(offset=(10, 10))
        self._plots[3].setLabel("left", "배터리 (%)")
        self._plots[3].addLegend(offset=(10, 10))
        self._plots[3].setYRange(0, 100, padding=0)
        radar = self._plots[4]
        radar.setAspectLocked(True)
        radar.hideAxis("left")
        radar.hideAxis("bottom")
        radar.showGrid(x=False, y=False)
        radar.addLegend(offset=(10, 10))
        self._plots[5].setLabel("bottom", "주문 경과 + 예상 소요 (분)")
        self._plots[6].setLabel("bottom", "시뮬레이션 시간 (분)")
        self._plots[6].getAxis("left").setTicks(
            [[(row, label) for row, _, _, label in c.TIMELINE_STYLE.values()]])
        self._plots[6].setYRange(-0.5, len(c.TIMELINE_STYLE) - 0.5, padding=0)
        for index in (0, 2, 3, 6):
            self._plots[index].setXRange(0, c.HISTORY_START_MINUTES, padding=0)
        throughput.setYRange(0, self._throughput_top, padding=0)
        self._plots[2].setYRange(0, self._queue_top, padding=0)

    def _refresh_charts(self) -> None:
        """현재 탭 차트를 다시 그린다."""
        if self._state is None or not self.isVisible():
            return
        if self._tabs.currentWidget() is self._metrics:
            self._draw_metrics()
            return
        index = self._tabs.currentIndex() - 1
        drawers = (self._draw_throughput, self._draw_gantt, self._draw_queue, self._draw_battery, self._draw_radar,
                   self._draw_orders, self._draw_events, self._draw_roi, self._draw_planning)
        drawers[index]()

    def _draw_metrics(self) -> None:
        """라운드로빈과 현재 비교 정책의 지표를 같은 표에 표시한다."""
        assert self._state is not None
        baseline = self._state.engine_kpis.get("baseline", {})
        candidate = self._state.mode if self._state.mode != "baseline" else "optimized"
        optimized = self._state.engine_kpis.get(candidate, {})
        self._metrics.horizontalHeaderItem(2).setText(SchedulerModeLabel.LABELS.get(candidate, candidate))
        for row, metric in enumerate(LIVE_METRICS):
            base = baseline.get(metric.key, math.nan)
            opt = optimized.get(metric.key, math.nan)
            unit = "초" if metric.unit == "s" else metric.unit
            label = f"{metric.label} ({unit})"
            numbers = (base, opt, opt - base)
            values = (label, *(f"{value:,.{metric.decimals}f}" if math.isfinite(value) else "—"
                               for value in numbers))
            for column, value in enumerate(values):
                item = self._metrics.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    if column:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    self._metrics.setItem(row, column, item)
                if item.text() != value:
                    item.setText(value)

    def _set_time_axis(self, plot: pg.PlotWidget, *, seconds: bool = False) -> None:
        """실행 시작을 고정하고 오른쪽 축을 5분 단위로 늘린다."""
        minutes = self._last_time / 60.0
        step = c.HISTORY_START_MINUTES
        right = max(step, math.ceil(minutes / step) * step)
        plot.setXRange(0, right * 60.0 if seconds else right, padding=0)

    def _draw_throughput(self) -> None:
        """Tab 1 — Baseline vs 최적화 처리량."""
        x = np.fromiter((sample.minute for sample in self._history), float)
        for mode, curve in self._throughput_curves.items():
            active = self._state is not None and mode in self._state.engine_kpis
            curve.setVisible(active)
            legend = self._plots[0].plotItem.legend
            label = legend.getLabel(curve) if legend is not None else None
            if label is not None:
                label.setText(SchedulerModeLabel.LABELS[mode] + ("" if active else " (미실행)"))
            y = np.fromiter((sample.throughput[mode] for sample in self._history), float)
            curve.setData(x, y, connect="finite")
            finite = y[np.isfinite(y)]
            if finite.size:
                self._throughput_top = max(self._throughput_top, math.ceil(finite.max() / 5) * 5)
        plot = self._plots[0]
        self._set_time_axis(plot)
        plot.setYRange(0, self._throughput_top, padding=0)

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
        self._set_time_axis(plot, seconds=True)
        plot.setYRange(-0.6, max(1, len(robots)) - 0.4)
        legend = "  ".join(f"<span style='color:{c.ROBOT_STATE_COLORS[s]}'>■</span> {RobotState.LABELS[s]}"
                           for s in (RobotState.MOVING, RobotState.WORKING, RobotState.CHARGING, RobotState.IDLE,
                                     RobotState.FAULT))
        plot.setTitle(legend + f"  <span style='color:{c.EMERGENCY_COLOR}'>□</span> 긴급배정", size="8pt")

    def _draw_queue(self) -> None:
        """Tab 3 — 스테이션별 대기 작업 누적 영역."""
        plot = self._plots[2]
        plot.clear()
        self._set_time_axis(plot)
        if not self._history:
            plot.setYRange(0, self._queue_top, padding=0)
            return
        stations = sorted({sid for sample in self._history for sid in sample.queues})
        x = np.fromiter((sample.minute for sample in self._history), float)
        cumulative = np.zeros(len(x))
        layers = []
        for station_id in stations:
            cumulative = cumulative + np.array([sample.queues.get(station_id, 0) for sample in self._history], float)
            layers.append((station_id, cumulative.copy()))
        for index, (station_id, values) in reversed(list(enumerate(layers))):
            color = QColor(c.ROBOT_PALETTE[index % len(c.ROBOT_PALETTE)])
            fill = QColor(color)
            fill.setAlpha(150)
            plot.plot(x, values, pen=pg.mkPen(color, width=1), fillLevel=0, brush=fill, name=station_id)
        self._queue_top = max(self._queue_top, math.ceil(cumulative.max() / 5) * 5)
        plot.setYRange(0, self._queue_top, padding=0)

    def _draw_battery(self) -> None:
        """Tab 4 — 로봇별 배터리 추이."""
        plot = self._plots[3]
        plot.clear()
        x = np.fromiter((sample.minute for sample in self._history), float)
        for robot_id in sorted(self._robot_colors):
            y = np.fromiter((sample.batteries.get(robot_id, math.nan) for sample in self._history), float)
            color = c.ROBOT_PALETTE[self._robot_colors.get(robot_id, 0) % len(c.ROBOT_PALETTE)]
            plot.plot(x, y, pen=pg.mkPen(color, width=1.5), name=robot_id, connect="finite")
        self._set_time_axis(plot)

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
        modes = [m for m in c.ENGINE_COLORS if m in state.engine_kpis]
        for mode in modes:
            other_mode = next((m for m in modes if m != mode), mode)
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
            plot.getAxis("left").setTicks([[]])
            plot.setXRange(0, 1, padding=0)
            plot.setYRange(0, 1, padding=0)
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
        for event in self._events.get(state.mode, []):
            row, color, symbol, _ = c.TIMELINE_STYLE.get(event.kind, c.TIMELINE_STYLE["info"])
            spots.append({"pos": (event.sim_time / 60.0, row), "brush": pg.mkBrush(color), "symbol": symbol,
                          "size": 11, "pen": pg.mkPen(color), "data": event.message})
        scatter = pg.ScatterPlotItem(hoverable=True, hoverSize=15,
                                     tip=lambda x, y, data: f"T+{x:.1f}분\n{data}")
        scatter.addPoints(spots)
        plot.addItem(scatter)
        self._set_time_axis(plot)

    def _draw_roi(self) -> None:
        """Tab 8 — ROI 파라미터/숫자 요약/비교 차트. 완료 건수·이동거리만 넘기고 계산은 RoiPanel이 한다."""
        state = self._state
        assert state is not None
        base_kpis = state.engine_kpis.get("baseline", {})
        candidate = state.mode if state.mode != "baseline" else "optimized"
        opt_kpis = state.engine_kpis.get(candidate, {})
        self._roi_panel.update_kpis(
            int(base_kpis.get("completed_units", 0.0)), base_kpis.get("distance_m", 0.0),
            int(opt_kpis.get("completed_units", 0.0)), opt_kpis.get("distance_m", 0.0),
        )

    def _draw_planning(self) -> None:
        """Show planning scope, bounds, candidates, failure reasons and actual commitments."""
        state = self._state
        if state is None:
            return
        lines = ["실행 중: " + ", ".join(SchedulerModeLabel.LABELS.get(m, m) for m in state.active_policies),
                 "표시·개입 대상: " + SchedulerModeLabel.LABELS.get(state.mode, state.mode),
                 "라이브 비동기 실행은 headless staged-D1 실험과 시간 규약이 다릅니다."]
        for mode, plan in state.planning.items():
            report = plan.get("report") or {}
            lines += ["", SchedulerModeLabel.LABELS.get(mode, mode) + " · " + plan["execution_status"],
                      f"계획 범위: {plan['scope']} · 후보 {plan.get('candidates', 0)}/{plan.get('total_pending', 0)}",
                      "아래 해·경계는 마지막 반환 계획이며, 실제 채택은 적용 판정을 따릅니다.",
                      f"1단계 {report.get('phase1_status', '대기')}: 해 {report.get('phase1_cost')} / 경계 {report.get('phase1_bound')}",
                      f"2단계 {report.get('phase2_status', '대기')}: 해 {report.get('phase2_cost')} / 경계 {report.get('phase2_bound')}",
                      f"모델 {report.get('version', '')} · {report.get('backend_version', '')}",
                      "최근 적용 판정: " + (str(plan.get("reason", "")) or report.get("message") or "대기"),
                      f"채택 {plan['accepted']} / 대체 {plan['fallbacks']} / 폐기 {plan.get('stale', 0)} / 지연 {plan['overruns']}",
                      "확정한 첫 행동은 실제 통행 자원 승인 뒤 실행합니다."]
            reservations = plan.get("reservations", [])
            lines += [f"[{r['start']:.2f}, {r['end']:.2f}) {r['resource']} · {r['robot']} · {r['amount']}"
                      for r in reservations[:100]]
            if len(reservations) > 100:
                lines.append(f"추가 {len(reservations) - 100}개 예약은 내보낸 결과에서 확인")
        if not state.planning:
            lines.append("RH 정책을 선택하면 계획과 예약이 표시됩니다.")
        self._planning_view.setPlainText("\n".join(lines))
