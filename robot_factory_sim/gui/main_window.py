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
import time
import uuid
from collections import deque
from dataclasses import asdict
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
from agent.orchestration import is_comparison_request
from gui import constants as c
from gui.agent_bridge import AgentBridge
from gui.alert_banner import AlertBanner
from gui.config_editor import ConfigEditorDialog
from gui.console_panel import ConsolePanel
from gui.dashboard_panel import DashboardPanel
from gui.factory_panel import FactoryPanel
from gui.optimization_panel import OptimizationPanel
from gui.order_dialog import OrderDialog
from simulation.factory_service import FactoryService
from simulation.sim_worker import SimWorker
from simulation.state import SimState, state_to_context
from utils.event_types import (
    ActionKind,
    ChangeType,
    SchedulerModeLabel,
    Severity,
    Tables,
)

THREAD_STOP_TIMEOUT_MS: int = 3000
SHUTDOWN_POLL_INTERVAL_MS: int = 100
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
        self._closing = False
        self._comparison_enabled = bool(getattr(agent, "supports_comparison", False))
        self._comparison_pending = False
        self._comparison_request_id: str | None = None
        self.gui_response_ms: deque[float] = deque(maxlen=1000)
        self._last_response_seq = 0
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(SHUTDOWN_POLL_INTERVAL_MS)
        self._shutdown_timer.timeout.connect(self._finish_shutdown)

        self._build_toolbar()
        self._build_body()
        self._optimization = OptimizationPanel(self)
        self._optimization.run_requested.connect(lambda: self._on_user_message("/최적화"))

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
        if self._comparison_enabled:
            self._console.add_system_message("운영안 비교: [운영안 비교]에서 정책·조건 입력 → /최적화 → /추천. "
                                             "문서 질문은 /검색 ROI 산식. 결과는 자동 적용되지 않습니다.")

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
        self._mode.setToolTip("선택은 화면·조치 대상을 바꿉니다. 비활성 정책을 처음 선택하면 비교를 0초부터 다시 시작합니다.")
        self._mode.currentIndexChanged.connect(
            lambda _: self._worker.send_command(ActionKind.SET_MODE, {"mode": self._mode.currentData()}))
        toolbar.addWidget(self._mode)
        self._action(toolbar, "RH 설정", self._edit_planning, "계획 구간·격자·예산·후보·메모리 설정")
        toolbar.addSeparator()
        self._action(toolbar, "⚙ 공장 설정", self._open_config_editor)
        self._action(toolbar, "📋 주문 관리", self._open_order_dialog)
        self._action(toolbar, "🛗 엘리베이터 시연", self._start_elevator_demo,
                     "2층 이상 스테이션만 쓰는 주문을 넣고 1층 도크에서 다시 시작합니다")
        comparison_action = self._action(toolbar, "운영안 비교", lambda: self._optimization.show(),
                                         "현재 구성으로 베이스라인·MILP 배정을 비교합니다.")
        comparison_action.setEnabled(self._comparison_enabled)
        if not self._comparison_enabled:
            comparison_action.setToolTip("비교 계산 실행 함수가 연결되지 않았습니다.")

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

    def _start_elevator_demo(self) -> None:
        """엘리베이터 시연: 위층 스테이션 전용 주문을 넣고 로봇을 1층에서 다시 출발시킨다."""
        factory = self._service.load_factory_config()
        upper = [s.id for s in factory.stations if s.floor > 1 and s.is_active]
        if factory.floor_count < 2 or not factory.elevators or not upper:
            QMessageBox.information(self, "엘리베이터 시연",
                                    "2층 이상에 스테이션이 있고 엘리베이터가 있는 공장 구성이 필요합니다.\n"
                                    "공장 설정 > 프리셋 '3층 데모 공장'을 불러와 저장한 뒤 다시 시도하세요.")
            return
        now = self.sim_clock()
        self._service.create_order("시연-상층", c.DEMO_ORDER_QUANTITY, "high", now + timedelta(seconds=c.DEMO_DEADLINE_SEC),
                                   upper, now, "operator")
        self._console.add_system_message(f"🛗 엘리베이터 시연: {', '.join(upper)} 전용 주문 등록 — 로봇이 1층 도크에서 "
                                         "엘리베이터를 타고 이동합니다.")
        self._worker.send_command(ActionKind.SYNC_ORDERS)
        self._worker.send_command(ActionKind.RESET)
        self._start()

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
        sequence = state.diagnostics.get("command_seq", 0)
        if sequence and sequence != self._last_response_seq:
            self.gui_response_ms.append((time.monotonic() - state.diagnostics["command_sent"]) * 1000)
            self._last_response_seq = sequence

    def _edit_planning(self) -> None:
        from gui.planning_settings import PlanningSettingsDialog
        from simulation.planning.contracts import Settings
        values = (self._state.planning.get(self._state.mode, {}).get("settings") if self._state else None)
        dialog = PlanningSettingsDialog(values or asdict(Settings(scope="traffic")), self)
        if dialog.exec():
            self._worker.send_command(ActionKind.SET_PLANNING, dialog.values())

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
        self._optimization.mark_stale()
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
        if self._closing:
            return
        self._agent_bridge.event_requested.emit(event, state_to_context(self._state))

    # ------------------------------------------------------------------
    # 에이전트
    # ------------------------------------------------------------------
    def _on_user_message(self, text: str) -> None:
        """사용자 채팅 → 에이전트."""
        if self._closing:
            return
        context = state_to_context(self._state)
        if self._comparison_pending:
            self._console.add_system_message("비교 요청이 진행 중입니다. 완료 후 다시 요청하세요.")
            return
        if self._comparison_enabled and is_comparison_request(text):
            try:
                context["comparison_conditions"] = self._optimization.conditions()
                context["comparison_policies"] = self._optimization.policies()
                context["comparison_candidate"] = self._optimization.candidate_policy()
                context["comparison_scenario"] = self._optimization.scenario_id()
                context["factory_config"] = self._service.load_factory_config().to_dict()
                context["sim_datetime"] = self.sim_clock().isoformat()
                context["comparison_orders"] = []
                for record in self._service.list_orders(active_only=True):
                    values = asdict(record)
                    for key in ("created_at", "deadline"):
                        values[key] = values[key].isoformat() if values[key] else None
                    context["comparison_orders"].append(values)
            except (ValueError, TypeError) as exc:
                self._console.add_system_message(str(exc), Severity.WARNING)
                return
            self._comparison_pending = True
            self._comparison_request_id = uuid.uuid4().hex
            context["_request_id"] = self._comparison_request_id
            self._optimization.set_busy(True)
        self._agent_bridge.message_requested.emit(text, context)

    def _on_agent_response(self, response: AgentResponse) -> None:
        """에이전트 응답 표시 + 조치 실행 + 긴급 배너."""
        if self._closing:
            return
        if self._comparison_pending and response.request_id == self._comparison_request_id:
            self._comparison_pending = False
            self._optimization.set_busy(False)
        if response.comparison_conditions:
            self._optimization.set_conditions(response.comparison_conditions)
        if response.comparison:
            self._optimization.show_result(response.comparison)
        if "운영 근거 (계산 결과와 별개인 문서):" in response.text:
            self._optimization.show_evidence(response.text.split("운영 근거 (계산 결과와 별개인 문서):", 1)[1])
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
        if not self._watchers_started:
            # ConfigWatcher는 시작 시점 이후 변경만 감시하므로, 시작 전 변경은 워커에 직접 반영을 요청한다.
            orders = action.kind in (ActionKind.CREATE_ORDER, ActionKind.CANCEL_ORDER)
            self._worker.send_command(ActionKind.SYNC_ORDERS if orders else ActionKind.RELOAD_CONFIG)

    def _execute_db_action(self, action: AgentAction) -> str:
        """DB 변경형 조치. 변경은 ConfigWatcher를 거쳐 시뮬레이터에 반영된다."""
        p = action.params
        match action.kind:
            case ActionKind.ADD_ROBOTS:
                added = self._service.add_robots(int(p.get("count", 1)))
                return f"🤖 DB INSERT: {', '.join(added)}"
            case ActionKind.ADD_CHARGER:
                floor = int(p.get("floor", 1))
                charger_id = self._service.add_charger(float(p["x"]), float(p["y"]), int(p.get("capacity", 2)),
                                                       floor=floor)
                return f"⚡ DB INSERT: 충전소 {charger_id} ({floor}F, {p['x']:.0f}, {p['y']:.0f})"
            case ActionKind.MOVE_OBSTACLE:
                moved = self._service.move_obstacle(str(p["obstacle_type"]), tuple(p["coords"]))
                if moved is None:
                    raise ValueError(f"{p['obstacle_type']} 항목을 찾을 수 없습니다.")
                return f"🚪 DB UPDATE: {moved.label or moved.obstacle_type} 위치 변경"
            case ActionKind.MOVE_STATION:
                floor = int(p["floor"]) if p.get("floor") is not None else None
                if not self._service.move_station(str(p["station_id"]), float(p["x"]), float(p["y"]), floor=floor):
                    raise ValueError(f"{p['station_id']} 스테이션을 찾을 수 없습니다.")
                return f"📐 DB UPDATE: {p['station_id']} 위치 변경"
            case ActionKind.SET_STATION_ACTIVE:
                changed = self._service.set_station_active(str(p["station_id"]), bool(p["active"]))
                state = "가동" if p["active"] else "비활성(고장)"
                if not changed:
                    return f"{p['station_id']}는 이미 {state} 상태입니다."
                return f"DB UPDATE: {p['station_id']} → {state}"
            case ActionKind.CREATE_ORDER:
                now = self.sim_clock()
                seconds = p.get("deadline_seconds")
                deadline = now + timedelta(seconds=float(seconds)) if seconds and not math.isnan(seconds) else None
                order = self._service.create_order(p["product_type"], int(p["quantity"]), p.get("priority", "normal"),
                                                   deadline, list(p.get("required_stations") or []), now, "ai_agent")
                return f"📋 DB INSERT: {order.order_id} ({order.priority})"
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
        self._optimization.mark_stale()
        self._console.add_system_message(f"💾 공장 설정 저장: {changes}건 변경")
        if not self._watchers_started:
            self._worker.send_command(ActionKind.RELOAD_CONFIG)

    def _open_order_dialog(self) -> None:
        """주문 관리."""
        dialog = OrderDialog(self._service, self.sim_clock, self)
        dialog.exec()
        self._optimization.mark_stale()
        if not self._watchers_started:
            self._worker.send_command(ActionKind.SYNC_ORDERS)

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        """스레드 종료를 요청하고 모두 종료되기 전에는 창을 유지한다."""
        threads = (self._config_watcher, self._event_watcher, self._worker, self._agent_bridge)
        if not self._closing:
            self._closing = True
            self.setEnabled(False)
            self._reload_timer.stop()
            for watcher in (self._config_watcher, self._event_watcher):
                watcher.stop()
            for thread in (self._worker, self._agent_bridge):
                thread.requestInterruption()
                thread.quit()
            for thread in threads:
                thread.wait(THREAD_STOP_TIMEOUT_MS)
        if any(thread.isRunning() for thread in threads):
            event.ignore()
            self._shutdown_timer.start()
            return
        self._shutdown_timer.stop()
        super().closeEvent(event)

    def _finish_shutdown(self) -> None:
        """진행 중인 요청이 끝난 뒤 메인 스레드에서 창을 닫는다."""
        threads = (self._config_watcher, self._event_watcher, self._worker, self._agent_bridge)
        if not any(thread.isRunning() for thread in threads):
            self._shutdown_timer.stop()
            self.close()
