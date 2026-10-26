"""통로 방향·점유와 교차로 대기열을 표시하는 재사용 그래픽 아이템."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem, QGraphicsSimpleTextItem

from gui import constants as c
from simulation.state import TrafficSnapshot


class TrafficItem(QGraphicsPathItem):
    """대기·점유가 변경된 경우에만 펜·라벨·도움말을 갱신한다."""

    def __init__(self, snap: TrafficSnapshot, parent: QGraphicsItem | None = None) -> None:
        """고정된 통로·교차로 도형을 한 번 생성한다.

        Args:
            snap: 통행 자원 상태.
            parent: 부모 그래픽 아이템.
        """
        super().__init__(parent)
        self.floor = snap.floor
        self._signature: tuple | None = None
        path = QPainterPath()
        if snap.kind == "intersection":
            path.addEllipse(QPointF(snap.x, snap.y), c.TRAFFIC_NODE_RADIUS, c.TRAFFIC_NODE_RADIUS)
        else:
            path.moveTo(snap.x, snap.y)
            path.lineTo(snap.end_x, snap.end_y)
            if not snap.bidirectional:
                angle = math.atan2(snap.end_y - snap.y, snap.end_x - snap.x)
                mx, my = (snap.x + snap.end_x) / 2, (snap.y + snap.end_y) / 2
                for offset in (-0.55, 0.55):
                    path.moveTo(mx - c.TRAFFIC_ARROW_SIZE * math.cos(angle + offset),
                                my - c.TRAFFIC_ARROW_SIZE * math.sin(angle + offset))
                    path.lineTo(mx, my)
        self.setPath(path)
        self.setZValue(5)
        self._label = QGraphicsSimpleTextItem(self)
        self._label.setPos((snap.x + snap.end_x) / 2 + 5, (snap.y + snap.end_y) / 2 + 5)
        self._label.setBrush(QColor(c.WARNING))
        self.apply_snapshot(snap)

    def apply_snapshot(self, snap: TrafficSnapshot) -> None:
        """점유·대기 변화를 반영한다.

        Args:
            snap: 새 통행 자원 상태.
        """
        signature = (snap.capacity, snap.occupied, snap.waiting, tuple(snap.queue), tuple(snap.occupants),
                     int(snap.longest_wait), snap.passable, snap.width, snap.bidirectional)
        if signature == self._signature:
            return
        self._signature = signature
        color = c.GRID
        if not snap.passable:
            color = c.CRITICAL
        elif snap.waiting:
            color = c.WARNING
        elif snap.occupied:
            color = c.SUCCESS
        width = c.TRAFFIC_BUSY_WIDTH if snap.waiting or snap.occupied or not snap.passable else c.TRAFFIC_IDLE_WIDTH
        self.setPen(QPen(QColor(color), width,
                        Qt.PenStyle.DashLine if not snap.passable else Qt.PenStyle.SolidLine))
        self._label.setText(f"대기 {snap.waiting}" if snap.waiting else "통행 불가" if not snap.passable else "")
        direction = "양방향" if snap.bidirectional else "시작 → 끝 일방통행"
        kind = "교차로" if snap.kind == "intersection" else f"통로 폭 {snap.width:g}m · {direction}"
        self.setToolTip(f"{snap.id} · {kind}\n확보 {snap.occupied}/{snap.capacity} (진입 예약 포함) · 대기 {snap.waiting}대\n"
                        f"확보 로봇: {', '.join(snap.occupants) or '-'}\n"
                        f"대기 순서(FIFO): {', '.join(snap.queue) or '-'}\n최장 대기 {snap.longest_wait:.1f}s\n"
                        "대기 위치: 진입 직전 노드. 통행 불가는 폭·장애물 조건을 확인하세요.")
