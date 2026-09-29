"""공장 시각화 (Factory Floor View).

DB 구성(SimState.layout)으로 바닥·설비·장애물·경로망을 그리고, 매 스냅샷마다 로봇/설비 상태를 갱신한다.
레이아웃 버전이 바뀌면(핫리로드) 정적 요소를 다시 그린다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.widgets.obstacle_item import ObstacleItem
from gui.widgets.robot_item import RobotItem
from gui.widgets.station_item import ChargerItem, StationItem
from simulation.path_graph import PathGraph
from simulation.state import RobotSnapshot, SimState
from utils.config_models import FactoryConfig
from utils.event_types import OrderStatus, SchedulerModeLabel

SCENE_MARGIN: float = 30.0
ROUTE_WIDTH: float = 1.6
DASH_STEP: float = 1.0


def draw_grid(parent_scene: QGraphicsScene, factory: FactoryConfig) -> list[QGraphicsItem]:
    """바닥과 격자를 그린다.

    Args:
        parent_scene: 대상 씬
        factory: 공장 구성

    Returns:
        생성된 아이템
    """
    profile = factory.profile
    floor = QGraphicsRectItem(0, 0, profile.floor_width, profile.floor_height)
    floor.setBrush(QBrush(QColor(c.FLOOR)))
    floor.setPen(QPen(QColor(c.ACCENT), 2))
    floor.setZValue(-10)
    parent_scene.addItem(floor)

    grid = QPainterPath()
    step = max(10.0, profile.grid_cell_size)
    x = step
    while x < profile.floor_width:
        grid.moveTo(x, 0)
        grid.lineTo(x, profile.floor_height)
        x += step
    y = step
    while y < profile.floor_height:
        grid.moveTo(0, y)
        grid.lineTo(profile.floor_width, y)
        y += step
    grid_item = QGraphicsPathItem(grid)
    grid_item.setPen(QPen(QColor(c.GRID), 0.6))
    grid_item.setZValue(-9)
    parent_scene.addItem(grid_item)
    return [floor, grid_item]


def build_network_item(factory: FactoryConfig) -> QGraphicsPathItem:
    """경로망(노드 + 엣지) 아이템을 만든다. 장애물로 차단된 엣지는 제외된다.

    Args:
        factory: 공장 구성

    Returns:
        경로망 아이템
    """
    graph = PathGraph(factory)
    path = QPainterPath()
    for a, b, blocked in graph.edges:
        if blocked:
            continue
        (ax, ay), (bx, by) = graph.nodes[a], graph.nodes[b]
        path.moveTo(ax, ay)
        path.lineTo(bx, by)
    for x, y in graph.nodes.values():
        path.addEllipse(QPointF(x, y), c.PATH_NODE_RADIUS, c.PATH_NODE_RADIUS)
    item = QGraphicsPathItem(path)
    color = QColor(c.HIGHLIGHT)
    color.setAlpha(70)
    item.setPen(QPen(color, 1.0))
    item.setZValue(-5)
    return item


class FactoryView(QGraphicsView):
    """공장 바닥 2D 뷰."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """뷰를 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setBackgroundBrush(QBrush(QColor(c.BG)))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)

        self._layout: FactoryConfig | None = None
        self._layout_version = -1
        self._static_items: list[QGraphicsItem] = []
        self._network_item: QGraphicsPathItem | None = None
        self._stations: dict[str, StationItem] = {}
        self._chargers: dict[str, ChargerItem] = {}
        self._robots: dict[str, RobotItem] = {}
        self._routes: dict[str, QGraphicsPathItem] = {}
        self._last_robots: list[RobotSnapshot] = []
        self._show_network = True
        self._dash_offset = 0.0
        self._ticks = 0
        self._blink_on = False

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(c.ANIMATION_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start()

    # ------------------------------------------------------------------
    # 레이아웃
    # ------------------------------------------------------------------
    def set_layout(self, factory: FactoryConfig) -> None:
        """정적 레이아웃(바닥/설비/장애물/경로망)을 다시 그린다.

        Args:
            factory: 공장 구성
        """
        for item in self._static_items:
            self._scene.removeItem(item)
        for item in [*self._stations.values(), *self._chargers.values()]:
            self._scene.removeItem(item)
        self._static_items.clear()
        self._stations.clear()
        self._chargers.clear()
        self._layout = factory

        self._static_items += draw_grid(self._scene, factory)
        for obstacle in factory.obstacles:
            item = ObstacleItem(obstacle)
            self._scene.addItem(item)
            self._static_items.append(item)
        self._network_item = build_network_item(factory)
        self._network_item.setVisible(self._show_network)
        self._scene.addItem(self._network_item)
        self._static_items.append(self._network_item)
        for station in factory.stations:
            item = StationItem(station)
            self._scene.addItem(item)
            self._stations[station.id] = item
        for charger in factory.chargers:
            item = ChargerItem(charger)
            self._scene.addItem(item)
            self._chargers[charger.id] = item

        profile = factory.profile
        self._scene.setSceneRect(QRectF(-SCENE_MARGIN, -SCENE_MARGIN, profile.floor_width + 2 * SCENE_MARGIN,
                                        profile.floor_height + 2 * SCENE_MARGIN))
        self._fit()

    def set_network_visible(self, visible: bool) -> None:
        """경로망 표시 토글.

        Args:
            visible: 표시 여부
        """
        self._show_network = visible
        if self._network_item is not None:
            self._network_item.setVisible(visible)

    # ------------------------------------------------------------------
    # 실시간 갱신
    # ------------------------------------------------------------------
    def update_state(self, state: SimState) -> None:
        """스냅샷을 반영한다.

        Args:
            state: 시뮬레이션 상태
        """
        if state.layout is not self._layout or state.layout_version != self._layout_version:
            self._layout_version = state.layout_version
            self.set_layout(state.layout)
        for snap in state.stations:
            item = self._stations.get(snap.id)
            if item is not None:
                item.apply_snapshot(snap)
        for snap in state.chargers:
            item = self._chargers.get(snap.id)
            if item is not None:
                item.apply_snapshot(snap)
        self._update_robots(state.robots)

    def _update_robots(self, robots: list[RobotSnapshot]) -> None:
        """로봇 아이템과 이동 경로 점선을 갱신한다."""
        self._last_robots = robots
        alive = {r.id for r in robots}
        for robot_id in [rid for rid in self._robots if rid not in alive]:
            self._scene.removeItem(self._robots.pop(robot_id))
            self._scene.removeItem(self._routes.pop(robot_id))
        for snap in robots:
            item = self._robots.get(snap.id)
            if item is None:
                item = RobotItem(snap.id, snap.color_index)
                route = QGraphicsPathItem()
                route.setZValue(8)
                self._scene.addItem(route)
                self._scene.addItem(item)
                self._robots[snap.id] = item
                self._routes[snap.id] = route
            item.apply_snapshot(snap, self._blink_on)
            self._update_route(self._routes[snap.id], snap, item)

    def _update_route(self, route_item: QGraphicsPathItem, snap: RobotSnapshot, robot_item: RobotItem) -> None:
        """이동 경로 점선."""
        if len(snap.route) < 2:
            route_item.setVisible(False)
            return
        path = QPainterPath(QPointF(*snap.route[0]))
        for x, y in snap.route[1:]:
            path.lineTo(x, y)
        route_item.setPath(path)
        color = QColor(c.EMERGENCY_COLOR) if snap.emergency_order else QColor(robot_item.unique_color)
        color.setAlpha(190)
        pen = QPen(color, ROUTE_WIDTH, Qt.PenStyle.CustomDashLine)
        pen.setDashPattern(list(c.ROUTE_DASH))
        pen.setDashOffset(self._dash_offset)
        route_item.setPen(pen)
        route_item.setVisible(True)

    def _animate(self) -> None:
        """경로 점선 흐름 + 긴급배정 깜빡임."""
        self._ticks += 1
        self._dash_offset -= DASH_STEP
        for route in self._routes.values():
            if route.isVisible():
                pen = route.pen()
                pen.setDashOffset(self._dash_offset)
                route.setPen(pen)
        if self._ticks % c.BLINK_INTERVAL_TICKS == 0:
            self._blink_on = not self._blink_on
            for snap in self._last_robots:
                item = self._robots.get(snap.id)
                if item is not None and snap.emergency_order:
                    item.apply_snapshot(snap, self._blink_on)

    # ------------------------------------------------------------------
    # 뷰
    # ------------------------------------------------------------------
    def _fit(self) -> None:
        """씬 전체가 보이도록 맞춘다."""
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        """크기 변경 시 다시 맞춘다."""
        super().resizeEvent(event)
        self._fit()


class FactoryPanel(QWidget):
    """공장 시각화 패널 (정보 칩 + 뷰)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self._title = QLabel("🏭 공장 시각화", self)
        self._title.setObjectName("PanelTitle")
        header.addWidget(self._title)
        header.addStretch(1)
        self._network_toggle = QCheckBox("경로망", self)
        self._network_toggle.setChecked(True)
        header.addWidget(self._network_toggle)
        layout.addLayout(header)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._sim_time = self._chip("Sim T+00:00:00")
        self._wall = self._chip("경과 00:00")
        self._orders = self._chip("활성 주문 0")
        self._mode = self._chip("모드 -")
        self._pending = self._chip("대기 작업 0")
        for chip in (self._sim_time, self._wall, self._orders, self._pending, self._mode):
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        self.view = FactoryView(self)
        layout.addWidget(self.view, 1)

        legend = QLabel(self._legend_text(), self)
        legend.setObjectName("Muted")
        legend.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(legend)
        self._network_toggle.toggled.connect(self.view.set_network_visible)

    def _chip(self, text: str) -> QLabel:
        """정보 칩 라벨."""
        label = QLabel(text, self)
        label.setObjectName("InfoChip")
        return label

    @staticmethod
    def _legend_text() -> str:
        """로봇 상태 색상 범례."""
        items = [("이동", "moving"), ("작업", "working"), ("충전", "charging"), ("대기", "idle"), ("고장", "fault")]
        parts = [f"<span style='color:{c.ROBOT_STATE_COLORS[key]}'>●</span> {name}" for name, key in items]
        parts.append(f"<span style='color:{c.EMERGENCY_COLOR}'>●</span> 긴급배정(깜빡임)")
        return "&nbsp;&nbsp;".join(parts)

    def update_state(self, state: SimState) -> None:
        """스냅샷을 반영한다.

        Args:
            state: 시뮬레이션 상태
        """
        self._title.setText(f"🏭 공장 시각화 — {state.layout.profile.factory_name}")
        seconds = int(state.sim_time)
        self._sim_time.setText(f"Sim T+{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
        wall = int(state.wall_elapsed)
        self._wall.setText(f"경과 {wall // 60:02d}:{wall % 60:02d} · {state.speed}x")
        active = sum(1 for o in state.orders if o.status in OrderStatus.ACTIVE)
        self._orders.setText(f"활성 주문 {active}")
        self._pending.setText(f"대기 작업 {state.pending_tasks}")
        self._mode.setText(f"모드 {SchedulerModeLabel.LABELS.get(state.mode, state.mode)}")
        self.view.update_state(state)
