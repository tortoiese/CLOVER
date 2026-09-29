"""주문 관리 다이얼로그 — 주문 목록 / 추가 / 취소 / 수량 변경.

주문은 FactoryService를 통해 DB에 기록되고, 시뮬레이터는 1초 주기로 DB 주문을 동기화한다.
critical 주문은 production_events에 긴급 이벤트가 함께 기록되어 AI 관제가 자동 대응한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from gui import constants as c
from simulation.factory_service import FactoryService
from utils.config_models import OrderRecord
from utils.event_types import OrderStatus

COLUMNS: tuple[str, ...] = ("주문 ID", "제품", "수량", "완료", "진행률", "우선순위", "납기", "스테이션", "상태")
PRIORITY_COLORS: dict[str, str] = {"critical": c.CRITICAL, "high": c.WARNING, "normal": c.INFO, "low": c.TEXT_MUTED}
MAX_QUANTITY: int = 100000
MAX_DEADLINE_HOURS: float = 72.0


class OrderDialog(QDialog):
    """생산 주문 관리."""

    def __init__(self, service: FactoryService, sim_clock: Callable[[], datetime],
                 parent: QWidget | None = None) -> None:
        """다이얼로그를 생성한다.

        Args:
            service: 공장 서비스
            sim_clock: 현재 시뮬레이션 시각을 반환하는 함수 (납기 기준)
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setWindowTitle("📋 주문 관리")
        self.resize(980, 560)
        self._service = service
        self._sim_clock = sim_clock
        layout = QVBoxLayout(self)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setHorizontalHeaderLabels(list(COLUMNS))
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        actions = QHBoxLayout()
        self._quantity = QSpinBox(self)
        self._quantity.setRange(1, MAX_QUANTITY)
        update_qty = QPushButton("선택 주문 수량 변경", self)
        cancel = QPushButton("선택 주문 취소", self)
        cancel.setObjectName("Danger")
        refresh = QPushButton("새로고침", self)
        actions.addWidget(self._quantity)
        actions.addWidget(update_qty)
        actions.addWidget(cancel)
        actions.addStretch(1)
        actions.addWidget(refresh)
        layout.addLayout(actions)

        form_box = QGroupBox("새 주문", self)
        form_row = QHBoxLayout(form_box)
        form = QFormLayout()
        self._product = QLineEdit("제품A", form_box)
        self._new_quantity = QSpinBox(form_box)
        self._new_quantity.setRange(1, MAX_QUANTITY)
        self._new_quantity.setValue(100)
        self._priority = QComboBox(form_box)
        self._priority.addItems(list(config.ORDER_PRIORITIES))
        self._priority.setCurrentText("normal")
        self._deadline = QDoubleSpinBox(form_box)
        self._deadline.setRange(0.0, MAX_DEADLINE_HOURS)
        self._deadline.setSingleStep(0.25)
        self._deadline.setSuffix(" 시간 (0=없음)")
        self._deadline.setValue(1.0)
        form.addRow("제품", self._product)
        form.addRow("수량", self._new_quantity)
        form.addRow("우선순위", self._priority)
        form.addRow("납기 (sim 기준)", self._deadline)
        form_row.addLayout(form, 1)
        self._stations = QListWidget(form_box)
        self._stations.setToolTip("체크한 스테이션만 사용 (비우면 전체)")
        form_row.addWidget(self._stations, 1)
        add = QPushButton("주문 추가", form_box)
        add.setObjectName("Primary")
        form_row.addWidget(add, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(form_box)

        add.clicked.connect(self._add_order)
        cancel.clicked.connect(self._cancel_selected)
        update_qty.clicked.connect(self._update_quantity)
        refresh.clicked.connect(self.refresh)
        self._table.itemSelectionChanged.connect(self._sync_quantity)

        self._timer = QTimer(self)
        self._timer.setInterval(c.ORDER_DIALOG_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self._load_stations()
        self.refresh()

    def _load_stations(self) -> None:
        """스테이션 체크 리스트."""
        self._stations.clear()
        for station_id in self._service.station_ids():
            item = QListWidgetItem(station_id, self._stations)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)

    def refresh(self) -> None:
        """주문 목록을 다시 읽는다 (선택 유지)."""
        selected = self._selected_order_id()
        orders = self._service.list_orders()
        orders.sort(key=lambda o: (o.status not in OrderStatus.ACTIVE, config.PRIORITY_LEVELS.get(o.priority, 9)))
        self._table.setRowCount(len(orders))
        now = self._sim_clock()
        for row, order in enumerate(orders):
            self._fill_row(row, order, now)
            if order.order_id == selected:
                self._table.selectRow(row)

    def _fill_row(self, row: int, order: OrderRecord, now: datetime) -> None:
        """표 한 행."""
        pct = order.completed / max(1, order.quantity) * 100.0
        if order.deadline is None:
            deadline = "-"
        else:
            left = (order.deadline - now).total_seconds() / 60.0
            deadline = f"{order.deadline:%H:%M:%S} ({left:+.0f}분)"
        values = (order.order_id, order.product_type, str(order.quantity), str(order.completed), f"{pct:.0f}%",
                  order.priority, deadline, ", ".join(order.required_stations) or "전체", order.status)
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            if column == 5:
                item.setForeground(QColor(PRIORITY_COLORS.get(order.priority, c.TEXT)))
            if order.status not in OrderStatus.ACTIVE:
                item.setForeground(QColor(c.TEXT_MUTED))
            self._table.setItem(row, column, item)

    def _selected_order_id(self) -> str | None:
        """선택된 주문 ID."""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._table.item(rows[0].row(), 0)
        return item.text() if item else None

    def _sync_quantity(self) -> None:
        """선택 주문의 수량을 스핀박스에 표시."""
        rows = self._table.selectionModel().selectedRows()
        if rows:
            item = self._table.item(rows[0].row(), 2)
            if item:
                self._quantity.setValue(int(item.text()))

    def _add_order(self) -> None:
        """새 주문 등록."""
        product = self._product.text().strip()
        if not product:
            QMessageBox.warning(self, "주문 추가", "제품명을 입력하세요.")
            return
        now = self._sim_clock()
        hours = self._deadline.value()
        stations = [self._stations.item(i).text() for i in range(self._stations.count())
                    if self._stations.item(i).checkState() == Qt.CheckState.Checked]
        try:
            order = self._service.create_order(
                product, self._new_quantity.value(), self._priority.currentText(),
                now + timedelta(hours=hours) if hours > 0 else None, stations, now,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "주문 추가", str(exc))
            return
        self.refresh()
        if order.priority == "critical":
            QMessageBox.information(self, "긴급 주문", f"{order.order_id} 긴급 주문이 등록되었습니다.\nAI 관제가 자동 대응을 시작합니다.")

    def _cancel_selected(self) -> None:
        """선택 주문 취소."""
        order_id = self._selected_order_id()
        if order_id is None:
            return
        if QMessageBox.question(self, "주문 취소", f"{order_id} 주문을 취소할까요?") != QMessageBox.StandardButton.Yes:
            return
        if not self._service.cancel_order(order_id):
            QMessageBox.information(self, "주문 취소", "진행 중인 주문만 취소할 수 있습니다.")
        self.refresh()

    def _update_quantity(self) -> None:
        """선택 주문 수량 변경."""
        order_id = self._selected_order_id()
        if order_id is None:
            return
        if not self._service.update_order_quantity(order_id, self._quantity.value()):
            QMessageBox.information(self, "수량 변경", "변경되지 않았습니다 (진행 중 주문만, 기존과 다른 값).")
        self.refresh()
