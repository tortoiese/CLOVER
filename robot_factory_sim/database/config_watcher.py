"""ConfigWatcher — config_changelog 폴링으로 DB 설정 변경을 감지한다.

외부 시스템(업체 MES 등)이 DB를 직접 수정하더라도 changelog 행만 남기면 감지된다.
"""

from __future__ import annotations

import time
from typing import Any

from PySide6.QtCore import QThread, Signal

import config
from database.db_manager import ChangeRecord, DBManager


class ConfigWatcher(QThread):
    """DB의 config_changelog 테이블을 폴링하여 변경을 감지한다."""

    config_changed = Signal(str, object)  # (table_name, change dict)
    db_status_changed = Signal(bool)

    def __init__(
        self,
        db_path: str,
        poll_interval: float = config.POLL_INTERVAL,
        ignored_actors: frozenset[str] = config.WATCHER_IGNORED_ACTORS,
        ignored_tables: frozenset[str] = config.WATCHER_IGNORED_TABLES,
        parent: Any = None,
    ) -> None:
        """워처를 생성한다.

        Args:
            db_path: DB 경로
            poll_interval: 폴링 간격 (sec, 기본 1초)
            ignored_actors: 무시할 changed_by 값 (시뮬레이션 자체 기록 등)
            ignored_tables: 무시할 테이블
            parent: Qt 부모 객체
        """
        super().__init__(parent)
        self._db_path = db_path
        self.poll_interval = poll_interval
        self._ignored_actors = ignored_actors
        self._ignored_tables = ignored_tables
        self._running = False

    def stop(self) -> None:
        """폴링을 중지한다."""
        self._running = False

    def run(self) -> None:
        """폴링 루프 (워커 스레드)."""
        db = DBManager(self._db_path)
        self._running = True
        last_check_id = self._get_last_changelog_id(db)
        last_ok: bool | None = None
        while self._running:
            try:
                for change in self._poll_changelog(db, since_id=last_check_id):
                    last_check_id = change.id
                    if change.changed_by in self._ignored_actors or change.table_name in self._ignored_tables:
                        continue
                    self.config_changed.emit(change.table_name, change.to_dict())
                ok = True
            except Exception:  # noqa: BLE001 — DB 장애 시에도 스레드는 유지
                ok = False
            if ok != last_ok:
                self.db_status_changed.emit(ok)
                last_ok = ok
            self._sleep_interruptible()
        db.close()

    @staticmethod
    def _get_last_changelog_id(db: DBManager) -> int:
        """시작 시점의 마지막 changelog ID."""
        try:
            return db.get_last_changelog_id()
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _poll_changelog(db: DBManager, since_id: int) -> list[ChangeRecord]:
        """since_id 이후 변경 이력을 조회한다."""
        return db.get_changelog_since(since_id)

    def _sleep_interruptible(self) -> None:
        """stop() 요청에 빠르게 반응하도록 짧게 나눠 잔다."""
        deadline = time.monotonic() + self.poll_interval
        while self._running and time.monotonic() < deadline:
            time.sleep(0.05)
