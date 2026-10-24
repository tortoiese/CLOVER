"""ElevatorItem — 도면 위의 엘리베이터 (승강장 + 문 + 캐빈 위치 표시).

공장 뷰(실시간)와 설정 에디터(편집) 양쪽에서 쓴다. 엘리베이터는 모든 층의 같은 자리를 관통하므로 층마다 같은 모양으로 보이고,
지금 보고 있는 층에 캐빈이 있는지는 표시등으로 알려 준다. 아이템의 원점은 엘리베이터 중심이다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from gui import constants as c
from simulation.state import ElevatorSnapshot
from utils.config_models import ElevatorConfig

TITLE_FONT_SIZE: int = 7
SMALL_FONT_SIZE: int = 6
CORNER_RADIUS: float = 5.0
ARRIVED_TOLERANCE: float = 0.5


class ElevatorItem(QGraphicsItem):
    """엘리베이터 1대."""

    def __init__(self, cfg: ElevatorConfig, parent: QGraphicsItem | None = None) -> None:
        """아이템을 생성한다.

        Args:
            cfg: 엘리베이터 설정
            parent: 부모 아이템
        """
        super().__init__(parent)
        self.cfg = cfg
        self.snapshot: ElevatorSnapshot | None = None
        self.view_floor = 1
        self.setZValue(6)
        self.apply_config(cfg)

    def apply_config(self, cfg: ElevatorConfig) -> None:
        """설정(위치/크기)을 반영한다.

        Args:
            cfg: 엘리베이터 설정
        """
        self.prepareGeometryChange()
        self.cfg = cfg
        self.setPos(QPointF(cfg.x, cfg.y))
        self.setToolTip(f"{cfg.id} {cfg.label}\n한 층 이동 {cfg.travel_time:.1f}s · 동시 탑승 {cfg.capacity}")
        self.update()

    def set_view_floor(self, floor: int) -> None:
        """지금 도면에 보이는 층을 알린다 (캐빈 도착 표시등용).

        Args:
            floor: 층 번호
        """
        if floor != self.view_floor:
            self.view_floor = floor
            self.update()

    def apply_snapshot(self, snap: ElevatorSnapshot) -> None:
        """실시간 상태를 반영한다.

        Args:
            snap: 엘리베이터 스냅샷
        """
        changed = self.snapshot is None or round(self.snapshot.floor_pos, 2) != round(snap.floor_pos, 2) or \
            self.snapshot.occupied != snap.occupied or self.snapshot.queue_length != snap.queue_length
        self.snapshot = snap
        self.setToolTip(f"{snap.id} {snap.label}\n캐빈: {snap.floor_pos:.1f}층" + (" (이동 중)" if snap.moving else "")
                        + f"\n탑승 {snap.occupied} · 대기 {snap.queue_length} · 누적 {snap.rides}회"
                        + ("" if snap.active else "\n⚫ 운행 중지"))
        if changed:
            self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 — Qt override
        """그리기 영역."""
        w, h = self.cfg.width, self.cfg.height
        return QRectF(-w / 2 - 50, -h / 2 - 16, w + 100, h + 34)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        """승강장, 문, 층 표시등을 그린다."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.cfg.width, self.cfg.height
        rect = QRectF(-w / 2, -h / 2, w, h)
        snap = self.snapshot
        active = snap.active if snap else self.cfg.is_active
        base = QColor(c.ELEVATOR if active else c.TEXT_MUTED)
        fill = QColor(base)
        fill.setAlpha(60 if active else 22)
        painter.setPen(QPen(QColor(c.HIGHLIGHT) if self.isSelected() else base, 2.2 if self.isSelected() else 1.5))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

        # 가운데에서 열리는 두 짝 문
        door = QColor(base)
        door.setAlpha(150 if active else 50)
        painter.setPen(QPen(base.lighter(130), 1.0))
        painter.setBrush(door)
        gap = 2.0
        panel_w = w / 2 - 7
        painter.drawRect(QRectF(-panel_w - gap, h / 2 - 12, panel_w, 8))
        painter.drawRect(QRectF(gap, h / 2 - 12, panel_w, 8))

        # 위/아래 화살표 (이동 중일 때 방향)
        arrow = QPolygonF([QPointF(0, -h * 0.22), QPointF(-6, -h * 0.06), QPointF(6, -h * 0.06)])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c.TEXT) if active else QColor(c.TEXT_MUTED))
        painter.drawPolygon(arrow)
        painter.save()
        painter.scale(1, -1)
        painter.translate(0, -h * 0.02)
        painter.drawPolygon(arrow)
        painter.restore()

        font = QFont()
        font.setPointSize(TITLE_FONT_SIZE)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(c.TEXT))
        painter.drawText(QRectF(-w / 2 - 50, -h / 2 - 16, w + 100, 13), Qt.AlignmentFlag.AlignCenter,
                         f"{self.cfg.id} {self.cfg.label}")

        font.setPointSize(SMALL_FONT_SIZE)
        font.setBold(False)
        painter.setFont(font)
        here = snap is not None and abs(snap.floor_pos - self.view_floor) < ARRIVED_TOLERANCE
        lamp = QColor(c.SUCCESS if here and not (snap and snap.moving) else c.WARNING if here else c.TEXT_MUTED)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(lamp)
        painter.drawEllipse(QPointF(-w / 2 + 8, h / 2 + 9), 3.2, 3.2)
        painter.setPen(QColor(c.TEXT_MUTED))
        if snap is None:
            status = "-"
        elif not snap.active:
            status = "운행 중지"
        else:
            status = f"캐빈 {snap.floor_pos:.1f}F" + (f" · 탑승 {snap.occupied}" if snap.occupied else "") + \
                (f" · 대기 {snap.queue_length}" if snap.queue_length else "")
        painter.drawText(QRectF(-w / 2 + 14, h / 2 + 2, w + 8, 13), Qt.AlignmentFlag.AlignLeft, status)
