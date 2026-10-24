"""BuildingView — 층 바닥판을 쌓아 올린 경량 3D(등각 투영) 건물 모델.

OpenGL 없이 QPainter 다각형만으로 그린다. 구성 요소는 층 바닥판·벽/문/장애물·작업 스테이션·충전소·엘리베이터·로봇
일곱 가지뿐이라 상자(프리즘) 하나만 그릴 줄 알면 된다. 층을 클릭하면 floor_clicked(층 번호)가 나가고,
드래그하면 건물이 회전한다.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QMouseEvent, QPaintEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QToolTip, QWidget

from gui import constants as c
from gui.widgets.obstacle_item import is_area
from simulation.state import ChargerSnapshot, ElevatorSnapshot, RobotSnapshot, SimState, StationSnapshot
from utils.config_models import (
    ChargingStationConfig,
    ElevatorConfig,
    FactoryConfig,
    ObstacleConfig,
    WorkStationConfig,
)
from utils.event_types import RobotState

Plan = tuple[float, float]

WALL_THICKNESS: float = 8.0
DOOR_POST: float = 7.0
ROBOT_SIZE: float = 22.0
ROBOT_HEIGHT: float = 14.0
CHARGER_PAD_HEIGHT: float = 3.0
CHARGER_POST_HEIGHT: float = 24.0
CABIN_HEIGHT: float = 40.0
SHAFT_EXTRA: float = 80.0
COMPACT_WIDTH: int = 360
MARGIN_X: float = 16.0
MARGIN_TOP: float = 16.0
MARGIN_BOTTOM: float = 26.0
LABEL_ROOM: float = 54.0
FLOOR_EPSILON: float = 0.02
ROTATE_DEG_PER_PX: float = 0.45
PITCH_DEG_PER_PX: float = 0.30


class Projection:
    """평면 좌표(중심 원점)와 높이 z를 화면 좌표로 보내는 회전 + 기울임 투영."""

    def __init__(self, yaw_deg: float, pitch_deg: float) -> None:
        """투영을 만든다.

        Args:
            yaw_deg: 수직축 회전각 (도)
            pitch_deg: 지면과 시선이 이루는 각 (도, 90이면 위에서 내려다봄)
        """
        yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)
        self._cy, self._sy = math.cos(yaw), math.sin(yaw)
        self._cp, self._sp = math.cos(pitch), math.sin(pitch)
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0

    def depth(self, x: float, y: float) -> float:
        """시점에서 멀수록 작은 값 (그리는 순서용)."""
        return x * self._sy + y * self._cy

    def side(self, x: float, y: float) -> float:
        """화면 가로 방향 위치 (좌우 면 음영용)."""
        return x * self._cy - y * self._sy

    def project(self, x: float, y: float, z: float) -> QPointF:
        """월드 좌표를 화면 좌표로 변환한다."""
        xr = x * self._cy - y * self._sy
        yr = x * self._sy + y * self._cy
        return QPointF(self.ox + self.scale * xr, self.oy + self.scale * (yr * self._sp - z * self._cp))

    def fit(self, points: list[tuple[float, float, float]], rect: QRectF) -> None:
        """points가 rect 안에 꽉 차도록 배율과 이동량을 정한다."""
        self.scale, self.ox, self.oy = 1.0, 0.0, 0.0
        projected = [self.project(*p) for p in points]
        left = min(p.x() for p in projected)
        right = max(p.x() for p in projected)
        top = min(p.y() for p in projected)
        bottom = max(p.y() for p in projected)
        width, height = max(1e-6, right - left), max(1e-6, bottom - top)
        self.scale = max(1e-6, min(rect.width() / width, rect.height() / height))
        self.ox = rect.center().x() - (left + right) / 2 * self.scale
        self.oy = rect.center().y() - (top + bottom) / 2 * self.scale


def convex_hull(points: list[QPointF]) -> QPolygonF:
    """점들의 볼록 껍질 (Andrew monotone chain)."""
    pts = sorted({(round(p.x(), 3), round(p.y(), 3)) for p in points})
    if len(pts) <= 2:
        return QPolygonF([QPointF(*p) for p in pts])

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return QPolygonF([QPointF(*p) for p in lower[:-1] + upper[:-1]])


def rect_corners(cx: float, cy: float, w: float, h: float) -> list[Plan]:
    """중심과 크기로 사각형 네 꼭짓점."""
    return [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2), (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]


def thick_segment(a: Plan, b: Plan, thickness: float) -> list[Plan]:
    """선분에 두께를 준 사각형 꼭짓점."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * thickness / 2, dx / length * thickness / 2
    return [(a[0] + nx, a[1] + ny), (b[0] + nx, b[1] + ny), (b[0] - nx, b[1] - ny), (a[0] - nx, a[1] - ny)]


@dataclass
class _Drawable:
    """깊이 정렬되는 그리기 단위."""

    depth: float
    draw: Callable[[], None]


class BuildingView(QWidget):
    """여러 층을 쌓아 보여 주는 3D 건물. 층 클릭 → floor_clicked."""

    floor_clicked = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        """뷰를 만든다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setMinimumSize(120, 90)
        self._layout: FactoryConfig | None = None
        self._state: SimState | None = None
        self._selected: int | None = None
        self._hover: int | None = None
        self._yaw = c.BUILDING_YAW_DEG
        self._pitch = c.BUILDING_PITCH_DEG
        self._hit: dict[int, QPolygonF] = {}
        self._press: QPointF | None = None
        self._dragged = False
        self._painter: QPainter | None = None
        self._proj = Projection(self._yaw, self._pitch)

    # ------------------------------------------------------------------
    # 데이터
    # ------------------------------------------------------------------
    def set_state(self, state: SimState) -> None:
        """시뮬레이션 상태를 반영하고 다시 그린다.

        Args:
            state: 시뮬레이션 상태 스냅샷
        """
        self._layout = state.layout
        self._state = state
        self.update()

    def set_selected(self, floor: int | None) -> None:
        """강조할 층을 정한다.

        Args:
            floor: 층 번호 (None이면 강조 없음)
        """
        if floor != self._selected:
            self._selected = floor
            self.update()

    def reset_view(self) -> None:
        """시점을 기본값으로 되돌린다."""
        self._yaw, self._pitch = c.BUILDING_YAW_DEG, c.BUILDING_PITCH_DEG
        self.update()

    @property
    def compact(self) -> bool:
        """작게 줄어든 상태인지 (글자·장식을 줄인다)."""
        return self.width() < COMPACT_WIDTH

    def floor_summary(self, floor: int) -> str:
        """층 요약 문장 (툴팁/헤더용).

        Args:
            floor: 층 번호

        Returns:
            "3F · 포장 — 로봇 1대, 스테이션 1" 형태의 문자열
        """
        if self._layout is None:
            return f"{floor}F"
        names = " · ".join(s.label for s in self._layout.stations if s.floor == floor)
        robots = sum(1 for r in (self._state.robots if self._state else []) if round(r.floor_pos) == floor)
        stations = sum(1 for s in self._layout.stations if s.floor == floor)
        return f"{floor}F" + (f" · {names}" if names else "") + f" — 로봇 {robots}대, 스테이션 {stations}"

    # ------------------------------------------------------------------
    # 그리기
    # ------------------------------------------------------------------
    def _z(self, floor: float) -> float:
        """층 바닥 윗면의 높이."""
        return (floor - 1.0) * c.BUILDING_FLOOR_GAP

    def _prepare_projection(self, layout: FactoryConfig) -> None:
        """현재 시점으로 모든 층이 위젯 안에 들어오도록 투영을 맞춘다."""
        proj = Projection(self._yaw, self._pitch)
        width, height = layout.profile.floor_width, layout.profile.floor_height
        floors = layout.floor_count
        points: list[tuple[float, float, float]] = []
        for floor in range(1, floors + 1):
            for px, py in rect_corners(0, 0, width, height):
                points.append((px, py, self._z(floor)))
                points.append((px, py, self._z(floor) - c.BUILDING_SLAB_THICKNESS))
        for elevator in layout.elevators:
            ex, ey = elevator.x - width / 2, elevator.y - height / 2
            points.append((ex, ey, self._z(floors) + SHAFT_EXTRA))
        room_left = 0.0 if self.compact else LABEL_ROOM
        bottom = MARGIN_BOTTOM if not self.compact else 8.0
        area = QRectF(MARGIN_X + room_left, MARGIN_TOP, max(10.0, self.width() - 2 * MARGIN_X - room_left),
                      max(10.0, self.height() - MARGIN_TOP - bottom))
        proj.fit(points, area)
        self._proj = proj

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 — Qt override
        """배경과 건물 전체를 그린다."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint_background(painter)
        layout = self._layout
        if layout is None:
            painter.setPen(QColor(c.TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "공장 구성을 불러오는 중…")
            painter.end()
            return
        self._painter = painter
        self._prepare_projection(layout)
        self._hit.clear()
        for floor in range(1, layout.floor_count + 1):
            self._paint_floor(floor, layout)
        self._paint_elevators(layout)
        if not self.compact:
            self._paint_hint(painter)
        self._painter = None
        painter.end()

    def _paint_background(self, painter: QPainter) -> None:
        """둥근 모서리 배경과 은은한 그라데이션."""
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        gradient.setColorAt(0.0, QColor(c.BUILDING_BG_TOP))
        gradient.setColorAt(1.0, QColor(c.BUILDING_BG_BOTTOM))
        painter.setBrush(gradient)
        painter.setPen(QPen(QColor(c.GRID), 1))
        painter.drawRoundedRect(rect, 10, 10)

    def _paint_hint(self, painter: QPainter) -> None:
        """하단 조작 안내."""
        font = QFont(self.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(c.TEXT_MUTED))
        painter.drawText(QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignmentFlag.AlignCenter,
                         "층을 클릭하면 도면이 열립니다 · 드래그: 회전 · 더블클릭: 시점 초기화")

    # ----- 프리미티브 -------------------------------------------------
    def _poly(self, points: list[Plan], z: float, origin: Plan) -> QPolygonF:
        """평면 꼭짓점들을 높이 z에서 투영한 다각형."""
        return QPolygonF([self._proj.project(x - origin[0], y - origin[1], z) for x, y in points])

    def _prism(self, base: list[Plan], z0: float, height: float, top: QColor, side: QColor,
               origin: Plan, outline: QColor | None = None) -> None:
        """밑면 다각형을 위로 height만큼 뽑은 상자를 그린다 (보이는 옆면만)."""
        painter = self._painter
        assert painter is not None
        proj = self._proj
        cx = sum(p[0] for p in base) / len(base) - origin[0]
        cy = sum(p[1] for p in base) / len(base) - origin[1]
        center_depth, center_side = proj.depth(cx, cy), proj.side(cx, cy)
        painter.setPen(QPen(outline, 0.9) if outline else Qt.PenStyle.NoPen)
        faces: list[tuple[float, QPolygonF, QColor]] = []
        for i, a in enumerate(base):
            b = base[(i + 1) % len(base)]
            mx, my = (a[0] + b[0]) / 2 - origin[0], (a[1] + b[1]) / 2 - origin[1]
            if proj.depth(mx, my) <= center_depth + 1e-6:
                continue
            shade = side if proj.side(mx, my) < center_side else side.darker(135)
            quad = QPolygonF([proj.project(a[0] - origin[0], a[1] - origin[1], z0),
                              proj.project(b[0] - origin[0], b[1] - origin[1], z0),
                              proj.project(b[0] - origin[0], b[1] - origin[1], z0 + height),
                              proj.project(a[0] - origin[0], a[1] - origin[1], z0 + height)])
            faces.append((proj.depth(mx, my), quad, shade))
        for _, quad, shade in sorted(faces, key=lambda f: f[0]):
            painter.setBrush(shade)
            painter.drawPolygon(quad)
        painter.setBrush(top)
        painter.drawPolygon(self._poly(base, z0 + height, origin))

    def _label(self, text: str, anchor: QPointF, color: QColor, size: int = 7, bold: bool = True) -> None:
        """앵커 위쪽 가운데에 글자를 쓴다."""
        painter = self._painter
        assert painter is not None
        font = QFont(self.font())
        font.setPointSize(size)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRectF(anchor.x() - 60, anchor.y() - 16, 120, 14), Qt.AlignmentFlag.AlignCenter, text)

    # ----- 층 -----------------------------------------------------------
    def _paint_floor(self, floor: int, layout: FactoryConfig) -> None:
        """바닥판 → 벽·설비·로봇(뒤에서 앞 순서) → 라벨."""
        painter = self._painter
        assert painter is not None
        width, height = layout.profile.floor_width, layout.profile.floor_height
        origin: Plan = (width / 2, height / 2)
        z = self._z(floor)
        slab = rect_corners(width / 2, height / 2, width, height)
        selected, hover = floor == self._selected, floor == self._hover
        dim = self._selected is not None and not selected and self.compact

        self._paint_slab(floor, slab, z, origin, selected, hover, dim, layout)
        items: list[_Drawable] = []
        compact = self.compact
        labels: list[tuple[str, QPointF, QColor, int]] = []
        proj = self._proj

        def add(cx: float, cy: float, draw: Callable[[], None]) -> None:
            items.append(_Drawable(proj.depth(cx - origin[0], cy - origin[1]), draw))

        floor_obstacles = [o for o in layout.obstacles if o.floor == floor]
        doors = [o for o in floor_obstacles if o.obstacle_type == "door"]
        for obstacle in floor_obstacles:
            self._add_obstacle(obstacle, doors, z, origin, add)
        station_snaps = {s.id: s for s in (self._state.stations if self._state else [])}
        for station in layout.stations:
            if station.floor != floor:
                continue
            snap = station_snaps.get(station.id)
            add(station.x, station.y, lambda s=station, n=snap: self._draw_station(s, n, z, origin, labels, compact))
        charger_snaps = {s.id: s for s in (self._state.chargers if self._state else [])}
        for charger in layout.chargers:
            if charger.floor != floor:
                continue
            snap_c = charger_snaps.get(charger.id)
            add(charger.x, charger.y, lambda ch=charger, n=snap_c: self._draw_charger(ch, n, z, origin))
        for elevator in layout.elevators:
            add(elevator.x, elevator.y, lambda e=elevator: self._draw_elevator_pad(e, z, origin))
        for robot in (self._state.robots if self._state else []):
            if round(robot.floor_pos) == floor and abs(robot.floor_pos - round(robot.floor_pos)) < FLOOR_EPSILON:
                add(robot.x, robot.y, lambda r=robot: self._draw_robot(r, z, origin))
        for item in sorted(items, key=lambda i: i.depth):
            item.draw()
        if not compact:
            for text, anchor, color, size in labels:
                self._label(text, anchor, color, size)
        self._paint_floor_tag(floor, layout, slab, z, origin, selected or hover)

    def _paint_slab(self, floor: int, slab: list[Plan], z: float, origin: Plan, selected: bool, hover: bool,
                    dim: bool, layout: FactoryConfig) -> None:
        """바닥판(두께 있는 판) + 격자 + 선택/호버 테두리."""
        painter = self._painter
        assert painter is not None
        top = QColor(c.BUILDING_SLAB)
        top.setAlpha(150 if dim else 210)
        if hover:
            top = top.lighter(125)
        edge = QColor(c.HIGHLIGHT if selected else c.BUILDING_SLAB_EDGE)
        self._prism(slab, z - c.BUILDING_SLAB_THICKNESS, c.BUILDING_SLAB_THICKNESS, top,
                    QColor(c.BUILDING_SLAB_SIDE), origin, edge)
        step = max(10.0, layout.profile.grid_cell_size)
        width, height = layout.profile.floor_width, layout.profile.floor_height
        grid = QColor(c.HIGHLIGHT)
        grid.setAlpha(34)
        painter.setPen(QPen(grid, 0.6))
        x = step
        while x < width:
            painter.drawLine(self._proj.project(x - origin[0], -origin[1], z),
                             self._proj.project(x - origin[0], height - origin[1], z))
            x += step
        y = step
        while y < height:
            painter.drawLine(self._proj.project(-origin[0], y - origin[1], z),
                             self._proj.project(width - origin[0], y - origin[1], z))
            y += step
        if selected or hover:
            glow = QColor(c.HIGHLIGHT)
            glow.setAlpha(46 if selected else 28)
            painter.setBrush(glow)
            painter.setPen(QPen(QColor(c.HIGHLIGHT if selected else "#8fb5ff"), 2.2 if selected else 1.6))
            painter.drawPolygon(self._poly(slab, z, origin))
        corners = [self._proj.project(px - origin[0], py - origin[1], zz)
                   for px, py in slab for zz in (z, z - c.BUILDING_SLAB_THICKNESS)]
        self._hit[floor] = convex_hull(corners)

    def _paint_floor_tag(self, floor: int, layout: FactoryConfig, slab: list[Plan], z: float, origin: Plan,
                         active: bool) -> None:
        """바닥판 왼쪽 모서리에 '3F' 와 층 이름."""
        painter = self._painter
        assert painter is not None
        corners = [self._proj.project(px - origin[0], py - origin[1], z) for px, py in slab]
        left = min(corners, key=lambda p: p.x())
        font = QFont(self.font())
        font.setPointSize(9 if self.compact else 13)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(c.HIGHLIGHT) if active else QColor(c.TEXT))
        if self.compact:
            painter.drawText(QRectF(left.x() - 6, left.y() - 22, 40, 16), Qt.AlignmentFlag.AlignLeft, f"{floor}F")
            return
        painter.drawText(QRectF(left.x() - LABEL_ROOM - 6, left.y() - 14, LABEL_ROOM, 22),
                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, f"{floor}F")
        names = [s.label for s in layout.stations if s.floor == floor][:2]
        if names:
            font.setPointSize(7)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(c.TEXT_MUTED))
            painter.drawText(QRectF(left.x() - LABEL_ROOM - 6, left.y() + 6, LABEL_ROOM, 14),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, " · ".join(names))

    # ----- 설비 -------------------------------------------------------
    def _add_obstacle(self, obstacle: ObstacleConfig, doors: list[ObstacleConfig], z: float, origin: Plan,
                      add: Callable[[float, float, Callable[[], None]], None]) -> None:
        """벽/문/장애물을 깊이 정렬 가능한 조각으로 나눠 등록한다."""
        color = QColor(c.OBSTACLE_COLORS.get(obstacle.obstacle_type, c.TEXT_MUTED))
        a, b = (obstacle.x1, obstacle.y1), (obstacle.x2, obstacle.y2)
        if obstacle.obstacle_type == "door":
            for end in (a, b):
                post = rect_corners(end[0], end[1], DOOR_POST, DOOR_POST)
                add(end[0], end[1], lambda p=post: self._prism(p, z, c.BUILDING_WALL_HEIGHT, color.lighter(130), color,
                                                               origin, color.lighter(160)))
            strip = thick_segment(a, b, WALL_THICKNESS)
            glow = QColor(color)
            glow.setAlpha(120)
            add((a[0] + b[0]) / 2, (a[1] + b[1]) / 2,
                lambda s=strip: self._prism(s, z, 1.2, glow, glow, origin))
            return
        if is_area(obstacle):
            cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            base = rect_corners(cx, cy, abs(b[0] - a[0]), abs(b[1] - a[1]))
            fill = QColor(color)
            fill.setAlpha(235)
            add(cx, cy, lambda p=base: self._prism(p, z, 18.0, fill.lighter(125), fill, origin, color.lighter(150)))
            return
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        pieces = max(1, math.ceil(length / c.BUILDING_WALL_STEP))
        wall = obstacle.obstacle_type == "wall"
        height = c.BUILDING_WALL_HEIGHT if wall else 16.0
        for i in range(pieces):
            p0 = (a[0] + (b[0] - a[0]) * i / pieces, a[1] + (b[1] - a[1]) * i / pieces)
            p1 = (a[0] + (b[0] - a[0]) * (i + 1) / pieces, a[1] + (b[1] - a[1]) * (i + 1) / pieces)
            mid = ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)
            if wall and any(self._in_door(mid, d) for d in doors):
                continue
            base = thick_segment(p0, p1, WALL_THICKNESS)
            add(mid[0], mid[1], lambda p=base: self._prism(p, z, height, color.lighter(125), color, origin))

    @staticmethod
    def _in_door(point: Plan, door: ObstacleConfig) -> bool:
        """벽 조각의 중심이 문 범위 안인지."""
        pad = 6.0
        return (min(door.x1, door.x2) - pad <= point[0] <= max(door.x1, door.x2) + pad
                and min(door.y1, door.y2) - pad <= point[1] <= max(door.y1, door.y2) + pad)

    def _draw_station(self, cfg: WorkStationConfig, snap: StationSnapshot | None, z: float, origin: Plan,
                      labels: list[tuple[str, QPointF, QColor, int]], compact: bool) -> None:
        """작업 스테이션: 본체 + 제어부 + 상태 램프."""
        active = snap.active if snap else cfg.is_active
        base_color = QColor(c.TASK_TYPE_COLORS.get(cfg.task_type, c.DEFAULT_TASK_COLOR))
        if not active:
            base_color = QColor(c.GRID)
        body = rect_corners(cfg.x, cfg.y, cfg.width, cfg.height)
        self._prism(body, z, c.BUILDING_STATION_HEIGHT, base_color.lighter(120), base_color, origin,
                    base_color.lighter(160))
        unit = rect_corners(cfg.x - cfg.width * 0.12, cfg.y, cfg.width * 0.46, cfg.height * 0.5)
        self._prism(unit, z + c.BUILDING_STATION_HEIGHT, 11.0, base_color.lighter(160), base_color.lighter(120), origin)
        lamp_color = QColor(c.CRITICAL if not active else c.SUCCESS if (snap and snap.busy) else c.WARNING)
        lamp = rect_corners(cfg.x + cfg.width * 0.32, cfg.y + cfg.height * 0.28, 9.0, 9.0)
        self._prism(lamp, z + c.BUILDING_STATION_HEIGHT, 7.0, lamp_color.lighter(130), lamp_color, origin)
        if not compact:
            anchor = self._proj.project(cfg.x - origin[0], cfg.y - origin[1],
                                        z + c.BUILDING_STATION_HEIGHT + 14.0)
            labels.append((f"{cfg.id} {cfg.label}", anchor, QColor(c.TEXT), 7))

    def _draw_charger(self, cfg: ChargingStationConfig, snap: ChargerSnapshot | None, z: float,
                      origin: Plan) -> None:
        """충전소: 바닥 패드 + 충전 기둥."""
        active = snap.active if snap else cfg.is_active
        yellow = QColor(c.ROBOT_STATE_COLORS[RobotState.CHARGING] if active else c.TEXT_MUTED)
        pad = rect_corners(cfg.x, cfg.y, c.CHARGER_SIZE, c.CHARGER_SIZE)
        pad_color = QColor(yellow)
        pad_color.setAlpha(110)
        self._prism(pad, z, CHARGER_PAD_HEIGHT, pad_color, pad_color.darker(150), origin, yellow)
        post = rect_corners(cfg.x, cfg.y - c.CHARGER_SIZE * 0.25, 10.0, 10.0)
        self._prism(post, z + CHARGER_PAD_HEIGHT, CHARGER_POST_HEIGHT, yellow.lighter(120), yellow.darker(115), origin)
        if snap and snap.occupied:
            glow = rect_corners(cfg.x, cfg.y - c.CHARGER_SIZE * 0.25, 14.0, 14.0)
            glow_color = QColor(c.SUCCESS)
            glow_color.setAlpha(190)
            self._prism(glow, z + CHARGER_PAD_HEIGHT + CHARGER_POST_HEIGHT, 4.0, glow_color, glow_color, origin)

    def _draw_elevator_pad(self, cfg: ElevatorConfig, z: float, origin: Plan) -> None:
        """각 층의 엘리베이터 승강장 바닥."""
        color = QColor(c.ELEVATOR)
        color.setAlpha(90)
        pad = rect_corners(cfg.x, cfg.y, cfg.width, cfg.height)
        self._prism(pad, z, 1.5, color, color.darker(140), origin, QColor(c.ELEVATOR))

    def _draw_robot(self, robot: RobotSnapshot, z: float, origin: Plan, lift: float = 0.0) -> None:
        """로봇: 상태색 몸체 + 고유색 윗면."""
        body = QColor(c.ROBOT_STATE_COLORS.get(robot.state, c.TEXT_MUTED))
        unique = QColor(c.ROBOT_PALETTE[robot.color_index % len(c.ROBOT_PALETTE)])
        base = rect_corners(robot.x, robot.y, ROBOT_SIZE, ROBOT_SIZE)
        self._prism(base, z + lift, ROBOT_HEIGHT, unique, body, origin, unique.lighter(150))

    # ----- 엘리베이터 승강로 ---------------------------------------------
    def _paint_elevators(self, layout: FactoryConfig) -> None:
        """모든 층을 관통하는 유리 승강로, 캐빈, 탑승 중인 로봇."""
        painter = self._painter
        assert painter is not None
        if layout.floor_count < 1:
            return
        width, height = layout.profile.floor_width, layout.profile.floor_height
        origin: Plan = (width / 2, height / 2)
        snaps: dict[str, ElevatorSnapshot] = {e.id: e for e in (self._state.elevators if self._state else [])}
        riders = [r for r in (self._state.robots if self._state else [])
                  if abs(r.floor_pos - round(r.floor_pos)) >= FLOOR_EPSILON]
        for elevator in layout.elevators:
            snap = snaps.get(elevator.id)
            active = snap.active if snap else elevator.is_active
            shaft_color = QColor(c.ELEVATOR if active else c.TEXT_MUTED)
            glass = QColor(shaft_color)
            glass.setAlpha(26)
            shaft = rect_corners(elevator.x, elevator.y, elevator.width, elevator.height)
            bottom = self._z(1.0)
            top = self._z(layout.floor_count) + SHAFT_EXTRA - bottom
            edge = QColor(shaft_color)
            edge.setAlpha(120)
            self._prism(shaft, bottom, top, glass, glass, origin, edge)
            cabin_floor = snap.floor_pos if snap else 1.0
            cabin = QColor(c.ELEVATOR)
            cabin.setAlpha(175 if (snap and (snap.moving or snap.occupied)) else 120)
            inner = rect_corners(elevator.x, elevator.y, elevator.width * 0.82, elevator.height * 0.82)
            self._prism(inner, self._z(cabin_floor) + 2.0, CABIN_HEIGHT, cabin.lighter(125), cabin, origin,
                        QColor(c.ELEVATOR).lighter(150))
            for robot in riders:
                if abs(robot.x - elevator.x) < elevator.width and abs(robot.y - elevator.y) < elevator.height:
                    self._draw_robot(robot, self._z(robot.floor_pos), origin, lift=4.0)
            if not self.compact:
                anchor = self._proj.project(elevator.x - origin[0], elevator.y - origin[1],
                                            self._z(layout.floor_count) + SHAFT_EXTRA + 14.0)
                self._label(f"{elevator.id} 🛗", anchor, QColor(c.ELEVATOR), 7)

    # ------------------------------------------------------------------
    # 마우스
    # ------------------------------------------------------------------
    def _floor_at(self, pos: QPointF) -> int | None:
        """화면 좌표에 있는 층 (위층이 우선)."""
        for floor in sorted(self._hit, reverse=True):
            if self._hit[floor].containsPoint(pos, Qt.FillRule.OddEvenFill):
                return floor
        return None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """드래그 시작 지점을 기억한다."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position()
            self._dragged = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """드래그면 회전, 아니면 호버 층을 갱신한다."""
        pos = event.position()
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = pos - self._press
            if self._dragged or math.hypot(delta.x(), delta.y()) > c.BUILDING_DRAG_THRESHOLD:
                self._dragged = True
                low, high = c.BUILDING_PITCH_RANGE_DEG
                self._yaw += delta.x() * ROTATE_DEG_PER_PX
                self._pitch = max(low, min(high, self._pitch + delta.y() * PITCH_DEG_PER_PX))
                self._press = pos
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
                self.update()
            return
        hover = self._floor_at(pos)
        if hover != self._hover:
            self._hover = hover
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover else Qt.CursorShape.ArrowCursor)
            self.update()
        if hover:
            QToolTip.showText(event.globalPosition().toPoint(), self.floor_summary(hover), self)
        else:
            QToolTip.hideText()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """드래그가 아니면 클릭한 층을 알린다."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_drag = self._dragged
        self._press, self._dragged = None, False
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if not was_drag:
            floor = self._floor_at(event.position())
            if floor is not None:
                self.floor_clicked.emit(floor)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """시점 초기화."""
        self.reset_view()

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 — Qt override
        """호버 해제."""
        if self._hover is not None:
            self._hover = None
            self.update()
