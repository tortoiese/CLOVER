"""ObstacleItem — 벽 / 문 / 장애물 그래픽 (DB obstacles 테이블 기반)."""

from __future__ import annotations

from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QStyleOptionGraphicsItem, QWidget

from gui import constants as c
from utils.config_models import ObstacleConfig

WALL_WIDTH: float = 7.0
DOOR_WIDTH: float = 9.0
LABEL_FONT_SIZE: int = 6
PICK_MARGIN: float = 6.0


def is_area(obstacle: ObstacleConfig) -> bool:
    """사각형 장애물인지 (선분이 아닌).

    Args:
        obstacle: 장애물 설정

    Returns:
        사각형 여부
    """
    return obstacle.obstacle_type == "obstacle" and abs(obstacle.x2 - obstacle.x1) > 1 and \
        abs(obstacle.y2 - obstacle.y1) > 1


class ObstacleItem(QGraphicsItem):
    """벽/문/장애물. 아이템 좌표는 (x1, y1) 기준 상대 좌표로 그린다."""

    def __init__(self, cfg: ObstacleConfig, parent: QGraphicsItem | None = None) -> None:
        """아이템을 생성한다.

        Args:
            cfg: 장애물 설정
            parent: 부모 아이템
        """
        super().__init__(parent)
        self.cfg = cfg
        self.setZValue(3)
        self.apply_config(cfg)

    def apply_config(self, cfg: ObstacleConfig) -> None:
        """설정을 반영한다.

        Args:
            cfg: 장애물 설정
        """
        self.prepareGeometryChange()
        self.cfg = cfg
        self.setPos(QPointF(cfg.x1, cfg.y1))
        label = {"wall": "벽", "door": "문", "obstacle": "장애물"}.get(cfg.obstacle_type, cfg.obstacle_type)
        self.setToolTip(f"{label} {cfg.label or ''}\n({cfg.x1:.0f}, {cfg.y1:.0f}) - ({cfg.x2:.0f}, {cfg.y2:.0f})"
                        + ("\n통과 가능" if cfg.is_passable else ""))
        self.update()

    def _local_end(self) -> QPointF:
        """(x2, y2)의 아이템 좌표."""
        return QPointF(self.cfg.x2 - self.cfg.x1, self.cfg.y2 - self.cfg.y1)

    def boundingRect(self) -> QRectF:  # noqa: N802 — Qt override
        """그리기 영역."""
        rect = QRectF(QPointF(0, 0), self._local_end()).normalized()
        return rect.adjusted(-DOOR_WIDTH - 12, -DOOR_WIDTH - 12, DOOR_WIDTH + 12, DOOR_WIDTH + 12)

    def shape(self) -> QPainterPath:
        """선택/충돌 영역."""
        path = QPainterPath()
        if is_area(self.cfg):
            path.addRect(QRectF(QPointF(0, 0), self._local_end()).normalized())
            return path
        path.moveTo(0, 0)
        path.lineTo(self._local_end())
        stroker = QPainterPathStroker()
        stroker.setWidth(DOOR_WIDTH + PICK_MARGIN)
        return stroker.createStroke(path)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget | None = None) -> None:
        """장애물을 그린다."""
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(c.OBSTACLE_COLORS.get(self.cfg.obstacle_type, c.TEXT_MUTED))
        end = self._local_end()
        if self.isSelected():
            painter.setPen(QPen(QColor(c.HIGHLIGHT), 1, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self.shape())

        if is_area(self.cfg):
            rect = QRectF(QPointF(0, 0), end).normalized()
            painter.setPen(QPen(color.lighter(130), 1.2))
            fill = QColor(color)
            fill.setAlpha(110)
            painter.setBrush(fill)
            painter.drawRect(rect)
            painter.setBrush(QBrush(color.lighter(150), Qt.BrushStyle.BDiagPattern))
            painter.drawRect(rect)
        elif self.cfg.obstacle_type == "door":
            pen = QPen(color, DOOR_WIDTH, Qt.PenStyle.DashLine, Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawLine(QLineF(QPointF(0, 0), end))
        else:
            painter.setPen(QPen(color, WALL_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.SquareCap))
            painter.drawLine(QLineF(QPointF(0, 0), end))

        if self.cfg.label:
            font = QFont()
            font.setPointSize(LABEL_FONT_SIZE)
            painter.setFont(font)
            painter.setPen(QColor(c.TEXT_MUTED))
            mid = QPointF(end.x() / 2, end.y() / 2)
            painter.drawText(QRectF(mid.x() + 6, mid.y() - 6, 80, 12), Qt.AlignmentFlag.AlignLeft, self.cfg.label)
