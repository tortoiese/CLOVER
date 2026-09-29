"""StationItem / ChargerItem — 작업 스테이션과 충전소 그래픽.

공장 뷰(실시간 상태)와 설정 에디터(편집) 양쪽에서 사용한다. 아이템의 원점은 설비 중심이다.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from gui import constants as c
from simulation.state import ChargerSnapshot, StationSnapshot
from utils.config_models import ChargingStationConfig, WorkStationConfig

TITLE_FONT_SIZE: int = 7
SMALL_FONT_SIZE: int = 6
CORNER_RADIUS: float = 6.0
UTIL_BAR_HEIGHT: float = 3.0
SELECTION_PEN_WIDTH: float = 2.0


class StationItem(QGraphicsItem):
    """작업 스테이션 (직사각형 + 라벨 + 처리 중 주문 + 공정 색상)."""

    def __init__(self, cfg: WorkStationConfig, parent: QGraphicsItem | None = None) -> None:
        """아이템을 생성한다.

        Args:
            cfg: 스테이션 설정
            parent: 부모 아이템
        """
        super().__init__(parent)
        self.cfg = cfg
        self.snapshot: StationSnapshot | None = None
        self.setZValue(5)
        self.apply_config(cfg)

    def apply_config(self, cfg: WorkStationConfig) -> None:
        """설정(위치/크기/공정)을 반영한다.

        Args:
            cfg: 스테이션 설정
        """
        self.prepareGeometryChange()
        self.cfg = cfg
        self.setPos(QPointF(cfg.x, cfg.y))
        self.update()

    def apply_snapshot(self, snap: StationSnapshot) -> None:
        """실시간 상태를 반영한다.

        Args:
            snap: 스테이션 스냅샷
        """
        self.snapshot = snap
        self.setToolTip(
            f"{snap.id} {snap.label} ({snap.task_type})\n{'가동' if snap.active else '⚫ 고장/비활성'}\n"
            f"처리 중: {snap.current_robot or '-'} {snap.current_order or ''}\n대기 로봇: {snap.queue_length} · "
            f"미배정 작업: {snap.pending_tasks}\n처리 완료: {snap.processed} · 가동률 {snap.utilization:.0f}%"
            + (f"\n전용: {snap.dedicated_order}" if snap.dedicated_order else "")
        )
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 — Qt override
        """그리기 영역."""
        w, h = self.cfg.width, self.cfg.height
        return QRectF(-w / 2 - 4, -h / 2 - 14, w + 8, h + 18)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        """스테이션을 그린다."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.cfg.width, self.cfg.height
        rect = QRectF(-w / 2, -h / 2, w, h)
        snap = self.snapshot
        active = snap.active if snap else self.cfg.is_active
        base = QColor(c.TASK_TYPE_COLORS.get(self.cfg.task_type, c.DEFAULT_TASK_COLOR))
        fill = QColor(base)
        fill.setAlpha(150 if active else 50)
        if not active:
            fill = QColor(c.GRID)

        pen = QPen(base.lighter(140), 1.5)
        if snap and snap.dedicated_order:
            pen = QPen(QColor(c.EMERGENCY_COLOR), 2.2, Qt.PenStyle.DashLine)
        if self.isSelected():
            pen = QPen(QColor(c.HIGHLIGHT), SELECTION_PEN_WIDTH + 1)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, CORNER_RADIUS, CORNER_RADIUS)

        if snap and snap.busy:
            glow = QColor(c.SUCCESS)
            glow.setAlpha(70)
            painter.setBrush(glow)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect.adjusted(3, 3, -3, -3), CORNER_RADIUS, CORNER_RADIUS)

        font = QFont()
        font.setPointSize(TITLE_FONT_SIZE)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(c.TEXT))
        painter.drawText(QRectF(-w / 2, -h / 2 - 14, w, 12), Qt.AlignmentFlag.AlignCenter,
                         f"{self.cfg.id} {self.cfg.label}")

        font.setPointSize(SMALL_FONT_SIZE)
        font.setBold(False)
        painter.setFont(font)
        lines = [self.cfg.task_type]
        if snap:
            if not active:
                lines = ["⚫ 고장"]
            else:
                lines.append(f"Q{snap.queue_length} · P{snap.pending_tasks}")
                if snap.current_order:
                    lines.append(snap.current_order.replace("ORD-", ""))
                elif snap.busy:
                    lines.append("일반 작업")
                if snap.dedicated_order:
                    lines.append("전용 " + snap.dedicated_order.replace("ORD-", ""))
        painter.drawText(rect.adjusted(2, 2, -2, -UTIL_BAR_HEIGHT - 2), Qt.AlignmentFlag.AlignCenter, "\n".join(lines))

        if not active:
            painter.setPen(QPen(QColor(c.CRITICAL), 2))
            painter.drawLine(rect.topLeft(), rect.bottomRight())
            painter.drawLine(rect.topRight(), rect.bottomLeft())
        elif snap:
            bar = QRectF(rect.left() + 4, rect.bottom() - UTIL_BAR_HEIGHT - 3, rect.width() - 8, UTIL_BAR_HEIGHT)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(c.BG))
            painter.drawRect(bar)
            painter.setBrush(QColor(c.SUCCESS))
            painter.drawRect(QRectF(bar.left(), bar.top(), bar.width() * snap.utilization / 100.0, bar.height()))


def bolt_polygon(size: float) -> QPolygonF:
    """번개 아이콘 폴리곤.

    Args:
        size: 아이콘 크기

    Returns:
        원점 중심 폴리곤
    """
    s = size / 2.0
    points = [(0.15, -1.0), (-0.55, 0.1), (-0.05, 0.1), (-0.2, 1.0), (0.55, -0.15), (0.05, -0.15)]
    return QPolygonF([QPointF(x * s, y * s) for x, y in points])


class ChargerItem(QGraphicsItem):
    """충전 스테이션 (번개 아이콘 + 점유 슬롯)."""

    def __init__(self, cfg: ChargingStationConfig, parent: QGraphicsItem | None = None) -> None:
        """아이템을 생성한다.

        Args:
            cfg: 충전소 설정
            parent: 부모 아이템
        """
        super().__init__(parent)
        self.cfg = cfg
        self.snapshot: ChargerSnapshot | None = None
        self.setZValue(5)
        self.apply_config(cfg)

    def apply_config(self, cfg: ChargingStationConfig) -> None:
        """설정을 반영한다.

        Args:
            cfg: 충전소 설정
        """
        self.cfg = cfg
        self.setPos(QPointF(cfg.x, cfg.y))
        self.update()

    def apply_snapshot(self, snap: ChargerSnapshot) -> None:
        """실시간 상태를 반영한다.

        Args:
            snap: 충전소 스냅샷
        """
        self.snapshot = snap
        self.setToolTip(f"{snap.id} 충전소\n점유 {snap.occupied}/{snap.capacity} · 대기 {snap.queue_length}\n"
                        f"충전 속도 {snap.charge_rate:.1f}/s" + ("" if snap.active else "\n⚫ 비활성"))
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802 — Qt override
        """그리기 영역."""
        half = c.CHARGER_SIZE / 2
        return QRectF(-half - 4, -half - 14, c.CHARGER_SIZE + 8, c.CHARGER_SIZE + 30)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        """충전소를 그린다."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        half = c.CHARGER_SIZE / 2
        rect = QRectF(-half, -half, c.CHARGER_SIZE, c.CHARGER_SIZE)
        snap = self.snapshot
        active = snap.active if snap else self.cfg.is_active
        yellow = QColor(c.ROBOT_STATE_COLORS["charging"])
        fill = QColor(yellow)
        fill.setAlpha(45 if active else 15)
        pen = QPen(QColor(c.HIGHLIGHT) if self.isSelected() else yellow, 2 if self.isSelected() else 1.4)
        painter.setPen(pen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(yellow if active else QColor(c.TEXT_MUTED))
        painter.drawPolygon(bolt_polygon(c.CHARGER_SIZE * 0.7))

        font = QFont()
        font.setPointSize(TITLE_FONT_SIZE)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(c.TEXT))
        painter.drawText(QRectF(-half - 10, -half - 14, c.CHARGER_SIZE + 20, 12), Qt.AlignmentFlag.AlignCenter,
                         self.cfg.id)
        font.setPointSize(SMALL_FONT_SIZE)
        font.setBold(False)
        painter.setFont(font)
        occupied = snap.occupied if snap else 0
        queue = f" +{snap.queue_length}" if snap and snap.queue_length else ""
        painter.drawText(QRectF(-half - 10, half + 2, c.CHARGER_SIZE + 20, 12), Qt.AlignmentFlag.AlignCenter,
                         f"{occupied}/{self.cfg.capacity}{queue}")
