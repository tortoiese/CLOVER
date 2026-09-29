"""RobotItem — 공장 뷰의 로봇 그래픽 (원형 + 고유색 링 + ID + 배터리 바 + 주문 ID)."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from gui import constants as c
from simulation.state import RobotSnapshot
from utils.event_types import RobotState

LABEL_FONT_SIZE: int = 6
ORDER_FONT_SIZE: int = 6
RING_WIDTH: float = 3.0
BOUND_MARGIN: float = 34.0


def battery_color(pct: float) -> QColor:
    """배터리 잔량별 색상.

    Args:
        pct: 잔량 (%)

    Returns:
        QColor
    """
    if pct < c.LOW_BATTERY:
        return QColor(c.CRITICAL)
    if pct < c.MID_BATTERY:
        return QColor(c.WARNING)
    return QColor(c.SUCCESS)


class RobotItem(QGraphicsItem):
    """로봇 1대."""

    def __init__(self, robot_id: str, color_index: int, parent: QGraphicsItem | None = None) -> None:
        """로봇 아이템을 생성한다.

        Args:
            robot_id: 로봇 ID
            color_index: 고유 색상 인덱스
            parent: 부모 아이템
        """
        super().__init__(parent)
        self.robot_id = robot_id
        self.unique_color = QColor(c.ROBOT_PALETTE[color_index % len(c.ROBOT_PALETTE)])
        self.state = RobotState.IDLE
        self.battery = 100.0
        self.order_id: str | None = None
        self.emergency = False
        self.blink_on = False
        self.setZValue(20)
        self.setAcceptHoverEvents(True)

    def boundingRect(self) -> QRectF:  # noqa: N802 — Qt override
        """그리기 영역."""
        return QRectF(-BOUND_MARGIN, -BOUND_MARGIN, 2 * BOUND_MARGIN, 2 * BOUND_MARGIN)

    def apply_snapshot(self, snap: RobotSnapshot, blink_on: bool) -> None:
        """스냅샷을 반영한다.

        Args:
            snap: 로봇 스냅샷
            blink_on: 긴급배정 깜빡임 위상
        """
        self.setPos(QPointF(snap.x, snap.y))
        changed = (
            self.state != snap.state or abs(self.battery - snap.battery_pct) >= 0.5 or self.order_id != snap.order_id
            or self.emergency != bool(snap.emergency_order) or self.blink_on != blink_on
        )
        self.state = snap.state
        self.battery = snap.battery_pct
        self.order_id = snap.order_id
        self.emergency = bool(snap.emergency_order)
        self.blink_on = blink_on
        order_text = snap.order_id or ("일반 작업" if snap.task_id else "-")
        self.setToolTip(
            f"{snap.id} ({snap.robot_type})\n상태: {RobotState.LABELS.get(snap.state, snap.state)}\n"
            f"배터리: {snap.battery_pct:.0f}%\n작업: {order_text}\n목표: {snap.target_id or '-'}\n"
            f"완료 작업: {snap.tasks_done}" + (f"\n🚨 긴급 배정: {snap.emergency_order}" if snap.emergency_order else "")
        )
        if changed:
            self.update()

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        """로봇을 그린다."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        radius = c.ROBOT_RADIUS
        fill = QColor(c.ROBOT_STATE_COLORS.get(self.state, c.TEXT_MUTED))
        if self.emergency and self.blink_on:
            fill = QColor(c.EMERGENCY_COLOR)
        if self.emergency:
            glow = QColor(c.EMERGENCY_COLOR)
            glow.setAlpha(90 if self.blink_on else 40)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(0, 0), radius + 7, radius + 7)

        painter.setPen(QPen(self.unique_color, RING_WIDTH))
        painter.setBrush(QBrush(fill))
        painter.drawEllipse(QPointF(0, 0), radius, radius)

        font = QFont()
        font.setPointSize(LABEL_FONT_SIZE)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#0b0f1e"))
        painter.drawText(QRectF(-radius, -radius, 2 * radius, 2 * radius), Qt.AlignmentFlag.AlignCenter,
                         self.robot_id.replace("R-", ""))

        bar = QRectF(-c.BATTERY_BAR_WIDTH / 2, radius + 4, c.BATTERY_BAR_WIDTH, c.BATTERY_BAR_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(c.GRID))
        painter.drawRoundedRect(bar, 1.5, 1.5)
        filled = QRectF(bar.left(), bar.top(), bar.width() * max(0.0, min(100.0, self.battery)) / 100.0, bar.height())
        painter.setBrush(battery_color(self.battery))
        painter.drawRoundedRect(filled, 1.5, 1.5)

        if self.order_id:
            font.setPointSize(ORDER_FONT_SIZE)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(c.EMERGENCY_COLOR if self.emergency else c.TEXT))
            painter.drawText(QRectF(-BOUND_MARGIN, -radius - 15, 2 * BOUND_MARGIN, 12),
                             Qt.AlignmentFlag.AlignCenter, self.order_id.replace("ORD-", ""))
