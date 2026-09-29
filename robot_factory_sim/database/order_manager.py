"""생산 주문 CRUD + ProductionEventWatcher.

주문 생성/취소/수량변경 시 AI 관제가 반응할 수 있도록 production_events 행을 함께 기록한다.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from PySide6.QtCore import QThread, Signal

import config
from database.db_manager import DBManager
from utils.config_models import OrderRecord
from utils.event_types import OrderStatus, ProductionEventType


class OrderManager:
    """생산 주문 비즈니스 로직. 실제 DB 접근은 DBManager에 위임한다."""

    def __init__(self, db: DBManager) -> None:
        """OrderManager를 생성한다.

        Args:
            db: DBManager 인스턴스
        """
        self._db = db

    def create_order(
        self,
        product_type: str,
        quantity: int,
        priority: str = "normal",
        deadline: datetime | None = None,
        required_stations: list[str] | None = None,
        created_at: datetime | None = None,
        changed_by: str = "operator",
    ) -> OrderRecord:
        """새 주문을 등록한다. critical 주문이면 critical_order 이벤트도 기록한다.

        Args:
            product_type: 제품명
            quantity: 수량
            priority: critical / high / normal / low
            deadline: 납기 (시뮬레이션 시계 기준 datetime)
            required_stations: 사용할 스테이션 ID 목록 (비우면 전체)
            created_at: 생성 시각 (시뮬레이션 시계 기준)
            changed_by: 변경 주체

        Returns:
            생성된 OrderRecord
        """
        if quantity <= 0:
            raise ValueError("수량은 1 이상이어야 합니다.")
        if priority not in config.PRIORITY_LEVELS:
            raise ValueError(f"알 수 없는 우선순위: {priority}")
        order = OrderRecord(
            order_id=self._db.next_order_id(),
            product_type=product_type,
            quantity=quantity,
            priority=priority,
            deadline=deadline,
            required_stations=list(required_stations or []),
            created_at=created_at or datetime.now(),
        )
        self._db.insert_order(order, changed_by)
        if priority == "critical":
            self.record_event(ProductionEventType.CRITICAL_ORDER, order.order_id, self._order_payload(order))
        return order

    def cancel_order(self, order_id: str, changed_by: str = "operator") -> bool:
        """주문을 취소한다.

        Args:
            order_id: 주문 ID
            changed_by: 변경 주체

        Returns:
            취소 여부
        """
        order = self._db.get_order(order_id)
        if order is None or not order.is_active:
            return False
        self._db.update_order(order_id, {"status": OrderStatus.CANCELLED}, changed_by)
        payload = self._order_payload(order)
        payload["released_units"] = order.remaining
        self.record_event(ProductionEventType.ORDER_CANCELLED, order_id, payload)
        return True

    def update_quantity(self, order_id: str, quantity: int, changed_by: str = "operator") -> bool:
        """주문 수량을 변경한다. 감소 시 order_reduced 이벤트를 기록한다.

        Args:
            order_id: 주문 ID
            quantity: 새 수량
            changed_by: 변경 주체

        Returns:
            변경 여부
        """
        order = self._db.get_order(order_id)
        if order is None or not order.is_active or quantity <= 0:
            return False
        changed = self._db.update_order(order_id, {"quantity": quantity}, changed_by)
        if changed and quantity < order.quantity:
            payload = self._order_payload(order)
            payload.update({"old_quantity": order.quantity, "new_quantity": quantity})
            self.record_event(ProductionEventType.ORDER_REDUCED, order_id, payload)
        return changed

    def update_progress(self, order_id: str, completed: int, status: str) -> bool:
        """시뮬레이션 진행률을 반영한다. (ConfigWatcher는 'simulation' 변경을 무시한다)

        Args:
            order_id: 주문 ID
            completed: 완료 수량
            status: 주문 상태

        Returns:
            변경 여부
        """
        return self._db.update_order(order_id, {"completed": completed, "status": status}, "simulation")

    def list_orders(self, active_only: bool = False) -> list[OrderRecord]:
        """주문 목록.

        Args:
            active_only: 진행 중 주문만

        Returns:
            OrderRecord 리스트
        """
        return self._db.list_orders(active_only)

    def record_event(self, event_type: str, order_id: str | None, payload: dict[str, Any]) -> int:
        """생산 이벤트를 기록한다.

        Args:
            event_type: ProductionEventType.*
            order_id: 관련 주문 ID
            payload: 상세 정보

        Returns:
            이벤트 ID
        """
        return self._db.insert_event(event_type, order_id, payload, changed_by="event_source")

    def has_event(self, event_type: str, order_id: str) -> bool:
        """이미 기록된 이벤트인지 확인한다.

        Args:
            event_type: 이벤트 종류
            order_id: 주문 ID

        Returns:
            존재 여부
        """
        return self._db.has_event(event_type, order_id)

    @staticmethod
    def _order_payload(order: OrderRecord) -> dict[str, Any]:
        """이벤트 payload용 주문 요약."""
        deadline_seconds = None
        if order.deadline and order.created_at:
            deadline_seconds = (order.deadline - order.created_at).total_seconds()
        return {
            "order_id": order.order_id,
            "product_type": order.product_type,
            "quantity": order.quantity,
            "completed": order.completed,
            "priority": order.priority,
            "deadline": order.deadline.isoformat(sep=" ", timespec="seconds") if order.deadline else None,
            "deadline_seconds": deadline_seconds,
            "required_stations": order.required_stations,
        }


class ProductionEventWatcher(QThread):
    """production_events 테이블을 폴링하여 신규 이벤트를 시그널로 전달한다."""

    production_event = Signal(object)  # dict: id, event_type, order_id, payload, created_at
    db_status_changed = Signal(bool)

    def __init__(self, db_path: str, poll_interval: float = config.POLL_INTERVAL, parent: Any = None) -> None:
        """워처를 생성한다.

        Args:
            db_path: DB 경로
            poll_interval: 폴링 간격 (sec)
            parent: Qt 부모 객체
        """
        super().__init__(parent)
        self._db_path = db_path
        self.poll_interval = poll_interval
        self._running = False

    def stop(self) -> None:
        """폴링을 중지한다."""
        self._running = False

    def run(self) -> None:
        """폴링 루프 (워커 스레드)."""
        db = DBManager(self._db_path)
        self._running = True
        last_ok: bool | None = None
        while self._running:
            try:
                events = db.fetch_unprocessed_events()
                if events:
                    db.mark_events_processed([int(e["id"]) for e in events])
                for event in events:
                    self.production_event.emit(event)
                ok = True
            except Exception:  # noqa: BLE001 — DB 장애 시에도 스레드는 유지
                ok = False
            if ok != last_ok:
                self.db_status_changed.emit(ok)
                last_ok = ok
            self._sleep_interruptible()
        db.close()

    def _sleep_interruptible(self) -> None:
        """stop() 요청에 빠르게 반응하도록 짧게 나눠 잔다."""
        deadline = time.monotonic() + self.poll_interval
        while self._running and time.monotonic() < deadline:
            time.sleep(0.05)


if __name__ == "__main__":
    manager_db = DBManager(":memory:")
    manager_db.initialize()
    manager = OrderManager(manager_db)
    created = manager.create_order("제품A", 100, "critical")
    print(created)
    print(manager_db.fetch_unprocessed_events())
