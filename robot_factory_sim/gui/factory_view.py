"""공장 층별 도면 (Floor Plan View).

DB 구성(SimState.layout)으로 한 층의 바닥·설비·장애물·경로망을 그리고, 매 스냅샷마다 로봇/설비 상태를 갱신한다.
층은 set_floor()로 바꾸며, 모든 층의 아이템을 한 씬에 두고 보이는 층만 켠다(층 전환이 즉시 이뤄진다).
Ctrl+휠로 축척(1 : N)을 격자에 딱 떨어지는 단계(SCALE_RATIOS)로 바꿀 수 있다.
레이아웃 버전이 바뀌면(핫리로드) 정적 요소를 다시 그린다.
"""

from __future__ import annotations

import math
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QWidget,
)

import config
from gui import constants as c
from gui.widgets.elevator_item import ElevatorItem
from gui.widgets.obstacle_item import ObstacleItem
from gui.widgets.robot_item import RobotItem
from gui.widgets.station_item import ChargerItem, StationItem
from gui.widgets.traffic_item import TrafficItem
from simulation.path_graph import PathGraph
from simulation.state import RobotSnapshot, SimState
from utils.config_models import FactoryConfig

SCENE_MARGIN: float = 30.0
ROUTE_WIDTH: float = 1.6
DASH_STEP: float = 1.0
WHEEL_NOTCH: int = 120
SCALE_BAR_CHOICES_M: tuple[float, ...] = (1, 2, 5, 10, 20, 50, 100, 200)
SCALE_BAR_MIN_PX: float = 60.0
SCALE_BAR_MAX_PX: float = 170.0
RATIO_EPSILON: float = 1e-6


def draw_grid(parent_scene: QGraphicsScene, factory: FactoryConfig) -> list[QGraphicsItem]:
    """바닥과 격자를 그린다 (모든 층의 바닥 크기가 같다).

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


def build_network_items(factory: FactoryConfig) -> dict[int, QGraphicsPathItem]:
    """층별 경로망(노드 + 엣지) 아이템을 만든다. 장애물로 차단된 엣지는 제외된다.

    Args:
        factory: 공장 구성

    Returns:
        층 번호 → 경로망 아이템
    """
    graph = PathGraph(factory)
    paths: dict[int, QPainterPath] = {floor: QPainterPath() for floor in range(1, factory.floor_count + 1)}
    for a, b, blocked in graph.edges:
        path = paths.get(graph.node_floor[a])
        if blocked or path is None:
            continue
        (ax, ay), (bx, by) = graph.nodes[a], graph.nodes[b]
        path.moveTo(ax, ay)
        path.lineTo(bx, by)
    for node, (x, y) in graph.nodes.items():
        path = paths.get(graph.node_floor[node])
        if path is not None:
            path.addEllipse(QPointF(x, y), c.PATH_NODE_RADIUS, c.PATH_NODE_RADIUS)
    items: dict[int, QGraphicsPathItem] = {}
    color = QColor(c.HIGHLIGHT)
    color.setAlpha(70)
    for floor, path in paths.items():
        item = QGraphicsPathItem(path)
        item.setPen(QPen(color, 1.0))
        item.setZValue(-5)
        items[floor] = item
    return items


class FactoryView(QGraphicsView):
    """공장 한 층의 2D 도면. 층 전환·Ctrl+휠 축척·드래그 이동을 지원한다."""

    scale_changed = Signal(float, bool)  # (축척 1:N 의 N, 맞춤 상태인지)

    def __init__(self, parent: QWidget | None = None) -> None:
        """뷰를 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setBackgroundBrush(QBrush(QColor(c.BG)))
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)

        self._layout: FactoryConfig | None = None
        self._layout_version = -1
        self._floor = 1
        self._static_items: list[QGraphicsItem] = []
        self._obstacles: list[ObstacleItem] = []
        self._network_items: dict[int, QGraphicsPathItem] = {}
        self._stations: dict[str, StationItem] = {}
        self._chargers: dict[str, ChargerItem] = {}
        self._elevators: dict[str, ElevatorItem] = {}
        self._robots: dict[str, RobotItem] = {}
        self._routes: dict[str, QGraphicsPathItem] = {}
        self._route_signatures: dict[str, tuple] = {}
        self._traffic_items: dict[str, TrafficItem] = {}
        self._traffic_state: SimState | None = None
        self._traffic_updated_at = -math.inf
        self._show_traffic = True
        self.route_rebuilds = 0
        self.traffic_refreshes = 0
        self._last_robots: list[RobotSnapshot] = []
        self._show_network = True
        self._fit_mode = True
        self._wheel_accumulator = 0
        self._dash_offset = 0.0
        self._ticks = 0
        self._blink_on = False
        self._apply_scrollbar_policy()

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(c.ANIMATION_INTERVAL_MS)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start()

    # ------------------------------------------------------------------
    # 층 / 레이아웃
    # ------------------------------------------------------------------
    @property
    def floor(self) -> int:
        """지금 보는 층 (1부터)."""
        return self._floor

    def set_floor(self, floor: int) -> None:
        """보여 줄 층을 바꾼다.

        Args:
            floor: 층 번호 (1부터)
        """
        count = self._layout.floor_count if self._layout else 1
        self._floor = max(1, min(count, floor))
        self._apply_floor_visibility()
        self._update_routes_for_floor()
        self.viewport().update()

    def _apply_floor_visibility(self) -> None:
        """현재 층의 아이템만 보이게 한다."""
        floor = self._floor
        for item in self._obstacles:
            item.setVisible(item.cfg.floor == floor)
        for station in self._stations.values():
            station.setVisible(station.cfg.floor == floor)
        for charger in self._chargers.values():
            charger.setVisible(charger.cfg.floor == floor)
        for elevator in self._elevators.values():
            elevator.set_view_floor(floor)
        for number, item in self._network_items.items():
            item.setVisible(self._show_network and number == floor)
        for item in self._traffic_items.values():
            item.setVisible(self._show_traffic and item.floor == floor)

    def set_layout(self, factory: FactoryConfig) -> None:
        """정적 레이아웃(바닥/설비/장애물/경로망)을 다시 그린다.

        Args:
            factory: 공장 구성
        """
        for item in self._static_items:
            self._scene.removeItem(item)
        for item in [*self._stations.values(), *self._chargers.values(), *self._elevators.values()]:
            self._scene.removeItem(item)
        self._static_items.clear()
        self._obstacles.clear()
        self._stations.clear()
        self._chargers.clear()
        self._elevators.clear()
        for item in self._traffic_items.values():
            self._scene.removeItem(item)
        self._traffic_items.clear()
        self._route_signatures.clear()
        self._traffic_updated_at = -math.inf
        self._layout = factory
        self._floor = max(1, min(factory.floor_count, self._floor))

        self._static_items += draw_grid(self._scene, factory)
        for obstacle in factory.obstacles:
            obstacle_item = ObstacleItem(obstacle)
            self._scene.addItem(obstacle_item)
            self._static_items.append(obstacle_item)
            self._obstacles.append(obstacle_item)
        self._network_items = build_network_items(factory)
        for item in self._network_items.values():
            self._scene.addItem(item)
            self._static_items.append(item)
        for station in factory.stations:
            station_item = StationItem(station)
            self._scene.addItem(station_item)
            self._stations[station.id] = station_item
        for charger in factory.chargers:
            charger_item = ChargerItem(charger)
            self._scene.addItem(charger_item)
            self._chargers[charger.id] = charger_item
        for elevator in factory.elevators:
            elevator_item = ElevatorItem(elevator)
            self._scene.addItem(elevator_item)
            self._elevators[elevator.id] = elevator_item
        self._apply_floor_visibility()

        profile = factory.profile
        self._scene.setSceneRect(QRectF(-SCENE_MARGIN, -SCENE_MARGIN, profile.floor_width + 2 * SCENE_MARGIN,
                                        profile.floor_height + 2 * SCENE_MARGIN))
        if self._fit_mode:
            self.fit_to_view()
        else:
            self._emit_scale()

    def set_network_visible(self, visible: bool) -> None:
        """경로망 표시 토글.

        Args:
            visible: 표시 여부
        """
        self._show_network = visible
        self._apply_floor_visibility()

    def set_traffic_visible(self, visible: bool) -> None:
        """통행 방향·병목 오버레이를 켜거나 끈다.

        Args:
            visible: 표시 여부.
        """
        self._show_traffic = visible
        self._update_traffic(force=True)
        self._apply_floor_visibility()

    def _update_traffic(self, force: bool = False) -> None:
        """최대 5Hz로 변경된 교통 아이템만 갱신한다."""
        if self._traffic_state is None or not self._show_traffic:
            return
        now = time.monotonic()
        if not force and (not self.isVisible() or now - self._traffic_updated_at < c.TRAFFIC_REFRESH_SECONDS):
            return
        self._traffic_updated_at = now
        self.traffic_refreshes += 1
        alive = {snap.id for snap in self._traffic_state.traffic}
        for identifier in list(self._traffic_items):
            if identifier not in alive:
                self._scene.removeItem(self._traffic_items.pop(identifier))
        for snap in self._traffic_state.traffic:
            item = self._traffic_items.get(snap.id)
            if item is None:
                item = TrafficItem(snap)
                self._scene.addItem(item)
                self._traffic_items[snap.id] = item
            elif snap.floor == self._floor:
                item.apply_snapshot(snap)
            item.setVisible(snap.floor == self._floor)

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
        for snap in state.elevators:
            item = self._elevators.get(snap.id)
            if item is not None:
                item.apply_snapshot(snap)
        self._update_robots(state.robots)
        self._traffic_state = state
        self._update_traffic()

    def _update_robots(self, robots: list[RobotSnapshot]) -> None:
        """로봇 아이템과 이동 경로 점선을 갱신한다 (현재 층에 있는 로봇만 보인다)."""
        self._last_robots = robots
        alive = {r.id for r in robots}
        for robot_id in [rid for rid in self._robots if rid not in alive]:
            self._scene.removeItem(self._robots.pop(robot_id))
            self._scene.removeItem(self._routes.pop(robot_id))
            self._route_signatures.pop(robot_id, None)
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
            visible = abs(snap.floor_pos - self._floor) < 0.5
            item.setVisible(visible)
            if visible:
                item.apply_snapshot(snap, self._blink_on)
                self._update_route(self._routes[snap.id], snap, item)
            else:
                self._routes[snap.id].setVisible(False)

    def _update_routes_for_floor(self) -> None:
        """층을 바꾼 직후 로봇/경로 표시를 마지막 스냅샷으로 다시 맞춘다."""
        for snap in self._last_robots:
            item = self._robots.get(snap.id)
            if item is not None:
                item.setVisible(abs(snap.floor_pos - self._floor) < 0.5)
                if item.isVisible():
                    item.apply_snapshot(snap, self._blink_on)
                    self._update_route(self._routes[snap.id], snap, item)
                else:
                    self._routes[snap.id].setVisible(False)
        self._update_traffic(force=True)

    def _update_route(self, route_item: QGraphicsPathItem, snap: RobotSnapshot, robot_item: RobotItem) -> None:
        """이동 경로 점선 (현재 층에 있는 구간만)."""
        signature = (self._floor, tuple(snap.route), tuple(snap.route_floors), snap.emergency_order)
        if signature == self._route_signatures.get(snap.id) and (route_item.isVisible() or len(snap.route) < 2):
            return
        self._route_signatures[snap.id] = signature
        self.route_rebuilds += 1
        if len(snap.route) < 2:
            route_item.setVisible(False)
            return
        floors = snap.route_floors if len(snap.route_floors) == len(snap.route) else [snap.floor] * len(snap.route)
        path = QPainterPath()
        pen_down = False
        for (x, y), floor in zip(snap.route, floors):
            if floor != self._floor:
                pen_down = False
                continue
            if pen_down:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
                pen_down = True
        if path.elementCount() < 2:
            route_item.setVisible(False)
            return
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
        if not self.isVisible():
            return
        self._update_traffic()
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
    # 축척 (1 : N)
    # ------------------------------------------------------------------
    def _px_per_mm(self) -> float:
        """화면 1mm가 차지하는 픽셀 수 (모니터 DPI 기준)."""
        return max(1.0, float(self.logicalDpiX())) / c.MM_PER_INCH

    def _scale_for_ratio(self, ratio: float) -> float:
        """축척 1:ratio 에 해당하는 뷰 배율 (씬 1px → 화면 px)."""
        return config.METERS_PER_PIXEL * 1000.0 * self._px_per_mm() / ratio

    def current_ratio(self) -> float:
        """지금의 축척 N (1 : N).

        Returns:
            실물 1 당 종이(화면)에 그려지는 크기의 역수
        """
        scale = max(1e-9, self.transform().m11())
        return config.METERS_PER_PIXEL * 1000.0 * self._px_per_mm() / scale

    def cell_size_text(self) -> str:
        """격자 한 칸의 실제 크기와 종이(화면) 위 크기.

        Returns:
            "격자 1칸 = 5 m (화면 10 mm)" 형태의 문자열
        """
        cell = self._layout.profile.grid_cell_size if self._layout else config.DEFAULT_GRID_CELL_SIZE
        meters = cell * config.METERS_PER_PIXEL
        paper_mm = meters * 1000.0 / self.current_ratio()
        return f"격자 1칸 = {meters:g} m (화면 {paper_mm:.1f} mm)"

    def _emit_scale(self) -> None:
        """축척이 바뀌었음을 알린다."""
        self.scale_changed.emit(self.current_ratio(), self._fit_mode)
        self.viewport().update()

    def _apply_scrollbar_policy(self) -> None:
        """맞춤 상태에서는 스크롤바를 숨기고, 확대하면 필요할 때만 보인다."""
        policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOff if self._fit_mode else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        self.setHorizontalScrollBarPolicy(policy)
        self.setVerticalScrollBarPolicy(policy)

    def fit_to_view(self) -> None:
        """도면 전체가 보이도록 맞춘다 (창 크기가 바뀌면 계속 맞춘다)."""
        self._fit_mode = True
        self._apply_scrollbar_policy()
        if not self._scene.sceneRect().isEmpty():
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._emit_scale()

    def set_ratio(self, ratio: float, anchor_mouse: bool = False) -> None:
        """축척을 1:ratio 로 정한다.

        Args:
            ratio: N (예: 500 → 1 : 500)
            anchor_mouse: True면 마우스 아래 지점을 고정한 채 확대/축소한다
        """
        self._fit_mode = False
        self._apply_scrollbar_policy()
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse if anchor_mouse
                                     else QGraphicsView.ViewportAnchor.AnchorViewCenter)
        scale = self._scale_for_ratio(ratio)
        self.setTransform(QTransform.fromScale(scale, scale))
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self._emit_scale()

    def zoom_step(self, direction: int, anchor_mouse: bool = False) -> None:
        """축척을 한 단계(SCALE_RATIOS) 확대/축소한다.

        Args:
            direction: +1 확대(N이 작아짐), -1 축소
            anchor_mouse: 마우스 아래 지점을 고정할지
        """
        now = self.current_ratio()
        steps = sorted(c.SCALE_RATIOS)
        if direction > 0:
            lower = [n for n in steps if n < now - RATIO_EPSILON * now]
            target = lower[-1] if lower else steps[0]
        else:
            higher = [n for n in steps if n > now + RATIO_EPSILON * now]
            target = higher[0] if higher else steps[-1]
        self.set_ratio(float(target), anchor_mouse)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 — Qt override
        """Ctrl+휠: 축척 단계 변경 / Shift+휠: 좌우 이동 / 그 외: 상하 이동."""
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            self._wheel_accumulator += event.angleDelta().y()
            while abs(self._wheel_accumulator) >= WHEEL_NOTCH:
                step = 1 if self._wheel_accumulator > 0 else -1
                self._wheel_accumulator -= step * WHEEL_NOTCH
                self.zoom_step(step, anchor_mouse=True)
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        """맞춤 상태면 크기 변경 시 다시 맞춘다."""
        super().resizeEvent(event)
        if self._fit_mode:
            self.fit_to_view()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """더블클릭: 맞춤."""
        self.fit_to_view()

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802 — Qt override
        """화면 고정 요소: 층 워터마크 + 축척 막대."""
        painter.save()
        painter.resetTransform()
        view = self.viewport().rect()
        font = QFont(self.font())
        font.setPointSize(30)
        font.setBold(True)
        painter.setFont(font)
        watermark = QColor(c.HIGHLIGHT)
        watermark.setAlpha(46)
        painter.setPen(watermark)
        painter.drawText(QRectF(view.right() - 150, 8, 140, 52), Qt.AlignmentFlag.AlignRight, f"{self._floor}F")
        self._draw_scale_bar(painter, view.left() + 14, view.bottom() - 16)
        painter.restore()

    def _draw_scale_bar(self, painter: QPainter, x: float, y: float) -> None:
        """실제 길이(m) 눈금이 있는 축척 막대와 1:N 표기."""
        scale = max(1e-9, self.transform().m11())
        px_per_m = scale / config.METERS_PER_PIXEL
        meters = next((m for m in SCALE_BAR_CHOICES_M if SCALE_BAR_MIN_PX <= m * px_per_m <= SCALE_BAR_MAX_PX),
                      SCALE_BAR_CHOICES_M[-1])
        length = meters * px_per_m
        color = QColor(c.TEXT)
        painter.setPen(QPen(color, 1.6))
        painter.drawLine(QPointF(x, y), QPointF(x + length, y))
        for tick_x in (x, x + length / 2, x + length):
            painter.drawLine(QPointF(tick_x, y - 4), QPointF(tick_x, y + 4))
        font = QFont(self.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(QRectF(x, y - 22, length + 150, 16), Qt.AlignmentFlag.AlignLeft,
                         f"{meters:g} m   ·   1 : {math.floor(self.current_ratio() + 0.5)}")
