"""메인 윈도우 — 툴바 + (공장 시각화 | 관제 콘솔 | 대시보드) + 긴급 알림 배너.

스레드 구성:
- 메인 스레드: Qt GUI
- SimWorker: SimPy 실행 (state_ready로 스냅샷 전달)
- ConfigWatcher / ProductionEventWatcher: DB 폴링 (1초)
- AgentBridge: AI 에이전트 실행
스레드 간 통신은 모두 Signal/Slot으로만 한다.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import config
from agent.base import BaseAgent
from agent.models import AgentAction, AgentResponse
from gui import constants as c
from gui.agent_bridge import AgentBridge
from gui.alert_banner import AlertBanner
from gui.config_editor import ConfigEditorDialog
from gui.console_panel import ConsolePanel
from gui.dashboard_panel import DashboardPanel
from gui.factory_view import FactoryPanel
from gui.order_dialog import OrderDialog
from simulation.factory_service import FactoryService
from simulation.sim_worker import SimWorker
from simulation.state import SimState, state_to_context
from utils.event_types import ActionKind, ChangeType, SchedulerModeLabel, Severity, Tables

THREAD_STOP_TIMEOUT_MS: int = 3000
SPLITTER_SIZES: tuple[int, int, int] = (620, 420, 560)


class MainWindow(QMainWindow):
    """애플리케이션 메인 윈도우."""

    def __init__(self, service: FactoryService, agent: BaseAgent, parent: QWidget | None = None) -> None:
        """윈도우를 구성하고 워커 스레드를 준비한다.

        Args:
            service: 공장 서비스 (DB 창구)
            agent: 관제 에이전트
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setWindowTitle(c.APP_TITLE)
        self.resize(1680, 960)
        self._service = service
        self._state: SimState | None = None
        self._running = False
        self._watchers_started = False
        self._pending_config_tables: set[str] = set()
        self._pending_config_by = "system"

        self._build_toolbar()
        self._build_body()

        self._worker = SimWorker(service.db_path, self)
        self._worker.state_ready.connect(self._on_state)
        self._worker.notice.connect(self._on_notice)
        self._worker.running_changed.connect(self._on_running_changed)

        self._config_watcher, self._event_watcher = service.create_watchers(self)
        self._config_watcher.config_changed.connect(self._on_config_changed)
        self._config_watcher.db_status_changed.connect(self._set_db_status)
        self._event_watcher.production_event.connect(self._on_production_event)
        self._event_watcher.db_status_changed.connect(self._set_db_status)

        self._agent_bridge = AgentBridge(agent, self)
        self._agent_bridge.response_ready.connect(self._on_agent_response)
        self._console.set_agent_name(agent.name)

        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(c.CONFIG_RELOAD_DEBOUNCE_MS)
        self._reload_timer.timeout.connect(self._flush_config_changes)

        self._set_db_status(service.ping())
        self._agent_bridge.start()
        self._worker.start()
        self._console.add_system_message(f"DB 연결: {service.db_path}\n[▶ 시작]을 누르면 시뮬레이션과 DB 감시가 시작됩니다.")

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_toolbar(self) -> None:
        """상단 툴바."""
        toolbar = QToolBar("시뮬레이션 제어", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._act_start = self._action(toolbar, "▶ 시작", self._start, "시뮬레이션 + DB 감시 시작")
        self._act_pause = self._action(toolbar, "⏸ 일시정지", lambda: self._worker.send_command(ActionKind.PAUSE))
        self._act_stop = self._action(toolbar, "⏹ 정지", self._stop, "정지 후 초기 상태로")
        toolbar.addWidget(QLabel(" ⏩ 배속 ", self))
        self._speed = QComboBox(self)
        for speed in config.SPEED_OPTIONS:
            self._speed.addItem(f"{speed}x", speed)
        self._speed.currentIndexChanged.connect(
            lambda _: self._worker.send_command(ActionKind.SET_SPEED, {"speed": self._speed.currentData()}))
        toolbar.addWidget(self._speed)
        self._action(toolbar, "↺ 리셋", lambda: self._worker.send_command(ActionKind.RESET), "DB 최신 구성으로 재구성")
        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" 스케줄러 ", self))
        self._mode = QComboBox(self)
        for mode in config.SCHEDULER_MODES:
            self._mode.addItem(SchedulerModeLabel.LABELS[mode], mode)
        self._mode.setCurrentIndex(config.SCHEDULER_MODES.index(config.DEFAULT_SCHEDULER_MODE))
        self._mode.setToolTip("두 스케줄러는 항상 병렬 실행되며, 선택한 쪽이 화면·AI 조치 대상이 됩니다.")
        self._mode.currentIndexChanged.connect(
            lambda _: self._worker.send_command(ActionKind.SET_MODE, {"mode": self._mode.currentData()}))
        toolbar.addWidget(self._mode)
        toolbar.addSeparator()
        self._action(toolbar, "⚙ 공장 설정", self._open_config_editor)
        self._action(toolbar, "📋 주문 관리", self._open_order_dialog)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self._db_status = QLabel("DB: ● 확인 중", self)
        self._db_status.setObjectName("DbStatus")
        toolbar.addWidget(self._db_status)
        self._act_pause.setEnabled(False)

    def _action(self, toolbar: QToolBar, text: str, slot: Any, tip: str = "") -> QAction:
        """툴바 액션 추가."""
        action = QAction(text, self)
        action.triggered.connect(slot)
        if tip:
            action.setToolTip(tip)
        toolbar.addAction(action)
        return action

    def _build_body(self) -> None:
        """본문: 3분할 스플리터 + 하단 배너."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 0)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        self._factory = FactoryPanel(splitter)
        self._console = ConsolePanel(splitter)
        self._dashboard = DashboardPanel(splitter)
        splitter.setSizes(list(SPLITTER_SIZES))
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter, 1)

        self._banner = AlertBanner(central)
        self._banner.clicked.connect(self._console.focus_console)
        layout.addWidget(self._banner)
        self._console.message_submitted.connect(self._on_user_message)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------
    # 시뮬레이션 제어
    # ------------------------------------------------------------------
    def _start(self) -> None:
        """시작: 워처 시작 + 시뮬레이션 실행."""
        if not self._watchers_started:
            self._config_watcher.start()
            self._event_watcher.start()
            self._watchers_started = True
            self._console.add_system_message("ConfigWatcher / ProductionEventWatcher 시작 (1초 폴링)")
        self._worker.send_command(ActionKind.START)

    def _stop(self) -> None:
        """정지 (초기 상태로)."""
        self._worker.send_command(ActionKind.STOP)

    def _on_running_changed(self, running: bool) -> None:
        """실행 상태 → 버튼 활성화."""
        self._running = running
        self._act_start.setEnabled(not running)
        self._act_pause.setEnabled(running)
        self._console.add_system_message("▶ 시뮬레이션 실행" if running else "⏸ 시뮬레이션 일시정지")

    def _on_state(self, state: SimState) -> None:
        """스냅샷 수신 → 각 패널 갱신."""
        self._state = state
        self._factory.update_state(state)
        self._dashboard.update_state(state)
        self._sync_combo(self._speed, state.speed)
        self._sync_combo(self._mode, state.mode)

    @staticmethod
    def _sync_combo(combo: QComboBox, value: Any) -> None:
        """AI 명령으로 바뀐 값을 콤보에 반영 (시그널 차단)."""
        index = combo.findData(value)
        if index >= 0 and index != combo.currentIndex():
            combo.blockSignals(True)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)

    def _on_notice(self, severity: str, message: str) -> None:
        """워커 시스템 알림 → 콘솔."""
        self._console.add_system_message(message, severity)

    def _set_db_status(self, connected: bool) -> None:
        """DB 연결 상태 표시."""
        self._db_status.setText("DB: ● 연결됨" if connected else "DB: ● 연결 끊김")
        self._db_status.setProperty("connected", "true" if connected else "false")
        self._db_status.style().unpolish(self._db_status)
        self._db_status.style().polish(self._db_status)

    def sim_clock(self) -> datetime:
        """현재 시뮬레이션 시각 (주문 납기 기준).

        Returns:
            시뮬레이션 시각 (상태 수신 전이면 현재 시각)
        """
        return self._state.sim_datetime if self._state else datetime.now()

    # ------------------------------------------------------------------
    # DB 변경 감지
    # ------------------------------------------------------------------
    def _on_config_changed(self, table: str, change: dict[str, Any]) -> None:
        """ConfigWatcher 변경 → 콘솔 알림 + 디바운스 후 핫리로드/주문 동기화."""
        if table == Tables.ORDERS:
            self._worker.send_command(ActionKind.SYNC_ORDERS)
            if change.get("change_type") == ChangeType.INSERT:
                values = change.get("new_values") or {}
                self._console.add_system_message(
                    f"📋 주문 등록 감지: {change.get('record_id')} {values.get('product_type', '')} "
                    f"{values.get('quantity', '')}개 [{values.get('priority', '')}] (by {change.get('changed_by')})")
            return
        if table not in Tables.CONFIG_TABLES:
            return
        self._pending_config_tables.add(table)
        self._pending_config_by = change.get("changed_by", "system")
        self._reload_timer.start()

    def _flush_config_changes(self) -> None:
        """모인 설정 변경을 한 번에 핫리로드."""
        tables = ", ".join(Tables.LABELS.get(t, t) for t in sorted(self._pending_config_tables))
        self._pending_config_tables.clear()
        self._console.add_system_message(f"⚙ DB 설정 변경 감지 ({tables}, by {self._pending_config_by}) → 핫리로드")
        self._worker.send_command(ActionKind.RELOAD_CONFIG)

    def _on_production_event(self, event: dict[str, Any]) -> None:
        """생산 이벤트 → AI 자율 대응."""
        self._agent_bridge.event_requested.emit(event, state_to_context(self._state))

    # ------------------------------------------------------------------
    # 에이전트
    # ------------------------------------------------------------------
    def _on_user_message(self, text: str) -> None:
        """사용자 채팅 → 에이전트."""
        self._agent_bridge.message_requested.emit(text, state_to_context(self._state))

    def _on_agent_response(self, response: AgentResponse) -> None:
        """에이전트 응답 표시 + 조치 실행 + 긴급 배너."""
        self._console.add_message(response.text, "agent", response.severity)
        if response.source == "event" and response.severity in (Severity.CRITICAL, Severity.WARNING):
            first_line = response.text.splitlines()[0] if response.text else ""
            self._banner.show_alert(response.title or first_line, "AI 관제가 자동 조치를 실행했습니다.",
                                    response.severity)
        for action in response.actions:
            self._execute_action(action)

    def _execute_action(self, action: AgentAction) -> None:
        """에이전트 조치 실행: 시뮬레이션 명령은 워커로, DB 변경은 FactoryService로."""
        if action.kind in ActionKind.SIM_COMMANDS:
            self._worker.send_command(action.kind, action.params)
            return
        try:
            message = self._execute_db_action(action)
        except (ValueError, KeyError) as exc:
            self._console.add_system_message(f"조치 실패: {exc}", Severity.WARNING)
            return
        if message:
            self._console.add_system_message(message)

    def _execute_db_action(self, action: AgentAction) -> str:
        """DB 변경형 조치. 변경은 ConfigWatcher를 거쳐 시뮬레이터에 반영된다."""
        p = action.params
        if not self._watchers_started:
            note = "\n(DB 감시가 아직 꺼져 있어 [▶ 시작] 후 반영됩니다)"
        else:
            note = ""
        match action.kind:
            case ActionKind.ADD_ROBOTS:
                added = self._service.add_robots(int(p.get("count", 1)))
                return f"🤖 DB INSERT: {', '.join(added)}{note}"
            case ActionKind.ADD_CHARGER:
                charger_id = self._service.add_charger(float(p["x"]), float(p["y"]), int(p.get("capacity", 2)))
                return f"⚡ DB INSERT: 충전소 {charger_id} ({p['x']:.0f}, {p['y']:.0f}){note}"
            case ActionKind.MOVE_OBSTACLE:
                moved = self._service.move_obstacle(str(p["obstacle_type"]), tuple(p["coords"]))
                if moved is None:
                    raise ValueError(f"{p['obstacle_type']} 항목을 찾을 수 없습니다.")
                return f"🚪 DB UPDATE: {moved.label or moved.obstacle_type} 위치 변경{note}"
            case ActionKind.MOVE_STATION:
                if not self._service.move_station(str(p["station_id"]), float(p["x"]), float(p["y"])):
                    raise ValueError(f"{p['station_id']} 스테이션을 찾을 수 없습니다.")
                return f"📐 DB UPDATE: {p['station_id']} 위치 변경{note}"
            case ActionKind.SET_STATION_ACTIVE:
                changed = self._service.set_station_active(str(p["station_id"]), bool(p["active"]))
                state = "가동" if p["active"] else "비활성(고장)"
                if not changed:
                    return f"{p['station_id']}는 이미 {state} 상태입니다."
                return f"DB UPDATE: {p['station_id']} → {state}{note}"
            case ActionKind.CREATE_ORDER:
                now = self.sim_clock()
                seconds = p.get("deadline_seconds")
                deadline = now + timedelta(seconds=float(seconds)) if seconds and not math.isnan(seconds) else None
                order = self._service.create_order(p["product_type"], int(p["quantity"]), p.get("priority", "normal"),
                                                   deadline, list(p.get("required_stations") or []), now, "ai_agent")
                return f"📋 DB INSERT: {order.order_id} ({order.priority}){note}"
            case ActionKind.CANCEL_ORDER:
                if not self._service.cancel_order(str(p["order_id"]), "ai_agent"):
                    raise ValueError(f"{p['order_id']}는 취소할 수 없는 주문입니다.")
                return f"🗑 DB UPDATE: {p['order_id']} 취소"
            case _:
                raise ValueError(f"알 수 없는 조치: {action.kind}")

    # ------------------------------------------------------------------
    # 다이얼로그
    # ------------------------------------------------------------------
    def _open_config_editor(self) -> None:
        """공장 설정 에디터."""
        dialog = ConfigEditorDialog(self._service, self)
        dialog.saved.connect(self._on_config_saved)
        dialog.exec()

    def _on_config_saved(self, changes: int) -> None:
        """에디터 저장 알림 (감시 전이면 직접 리로드)."""
        self._console.add_system_message(f"💾 공장 설정 저장: {changes}건 변경")
        if not self._watchers_started:
            self._worker.send_command(ActionKind.RELOAD_CONFIG)

    def _open_order_dialog(self) -> None:
        """주문 관리."""
        dialog = OrderDialog(self._service, self.sim_clock, self)
        dialog.exec()
        if not self._watchers_started:
            self._worker.send_command(ActionKind.SYNC_ORDERS)

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        """모든 스레드를 정리한다."""
        for watcher in (self._config_watcher, self._event_watcher):
            watcher.stop()
        for thread in (self._config_watcher, self._event_watcher):
            thread.wait(THREAD_STOP_TIMEOUT_MS)
        for thread in (self._worker, self._agent_bridge):
            thread.quit()
            if not thread.wait(THREAD_STOP_TIMEOUT_MS):
                QMessageBox.warning(self, "종료", "작업 스레드가 제때 종료되지 않았습니다.")
        super().closeEvent(event)
