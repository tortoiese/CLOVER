"""SimWorker — SimPy 엔진을 별도 QThread에서 실행한다.

- Baseline / 최적화 엔진을 같은 시드로 나란히 돌려 KPI를 동시 비교한다. ``mode``는 화면에 표시·제어할 엔진이다.
- GUI → 워커: ``send_command()`` (내부적으로 command_requested 시그널, Queued 연결)
- 워커 → GUI: state_ready(SimState), notice(severity, message), running_changed(bool)
- 엔진이 감지한 납기 임박/생산량 급증/주문 완료/설비 고장은 production_events에 기록되어
  ProductionEventWatcher → AI 관제로 전달된다.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

import config
from database.db_manager import DBManager
from database.order_manager import OrderManager
from simulation.engine import SimulationEngine
from simulation.state import SimState
from utils.config_models import FactoryConfig, OrderRecord
from utils.event_types import (
    ActionKind,
    OrderStatus,
    ProductionEventType,
    SchedulerModeLabel,
    Severity,
)

DEDUPED_EVENTS: frozenset[str] = frozenset({
    ProductionEventType.CRITICAL_ORDER, ProductionEventType.DEADLINE_RISK, ProductionEventType.ORDER_COMPLETED,
})
TASK_RATE_SCALE_UP: float = 1.5


class _SimController(QObject):
    """워커 스레드 안에서 생성되어 엔진을 구동하는 객체."""

    state_ready = Signal(object)
    notice = Signal(str, str)
    running_changed = Signal(bool)

    def __init__(self, db_path: str) -> None:
        """컨트롤러를 생성한다 (반드시 워커 스레드에서 호출).

        Args:
            db_path: DB 경로
        """
        super().__init__()
        self._db = DBManager(db_path)
        self._orders = OrderManager(self._db)
        self._speed = config.DEFAULT_SPEED
        self._running = False
        self._mode = config.DEFAULT_SCHEDULER_MODE
        self._task_rate = config.DEFAULT_TASK_RATE
        self._config_dirty = False
        self._progress_cache: dict[str, tuple[int, str]] = {}
        self._cached_orders: list[OrderRecord] = []
        self._wall_elapsed = 0.0
        self._engines: dict[str, SimulationEngine] = {}
        self._active_policies = list(config.DEFAULT_ACTIVE_POLICIES)
        from simulation.planning.contracts import Settings
        self._planning_settings = Settings(scope="traffic")
        self._command_sequence = 0
        self._command_sent = 0.0
        self._command_latency: deque[float] = deque(maxlen=1000)
        self._build_engines()
        self._timer = QTimer(self)
        self._timer.setInterval(config.WORKER_LOOP_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._last_wall = time.monotonic()
        self._last_emit = 0.0
        self._last_flush = 0.0

    # ------------------------------------------------------------------
    # 수명주기
    # ------------------------------------------------------------------
    def start_loop(self) -> None:
        """루프 타이머를 시작하고 초기 상태를 내보낸다."""
        self._last_wall = time.monotonic()
        self._timer.start()
        self._emit_state()

    def shutdown(self) -> None:
        """타이머를 멈추고 진행률을 저장한 뒤 DB 연결을 닫는다."""
        self._timer.stop()
        for engine in self._engines.values():
            if hasattr(engine.scheduler, "close"):
                engine.scheduler.close()
        try:
            self._flush_progress()
        except Exception:
            logging.getLogger(__name__).warning("종료 중 진행률 저장 실패", exc_info=True)
        self._db.close()

    @property
    def primary(self) -> SimulationEngine:
        """화면에 표시·제어되는 엔진."""
        return self._engines[self._mode]

    def _build_engines(self, factory: FactoryConfig | None = None, records: list[OrderRecord] | None = None) -> None:
        """DB 구성으로 두 엔진을 새로 만든다 (sim-time 0)."""
        for engine in self._engines.values():
            if hasattr(engine.scheduler, "close"):
                engine.scheduler.close()
        factory = self._db.load_factory_config() if factory is None else copy.deepcopy(factory)
        epoch = datetime.now().replace(microsecond=0)
        self._engines = {
            mode: SimulationEngine(factory, mode, config.RANDOM_SEED, epoch, {"task_rate": self._task_rate},
                                   comparison_seed=config.RANDOM_SEED)
            for mode in self._active_policies
        }
        from simulation.planning.controller import RollingHorizonScheduler
        from simulation.planning.process import SolverProcess
        for engine in self._engines.values():
            if isinstance(engine.scheduler, RollingHorizonScheduler):
                engine.scheduler.settings = self._planning_settings
                engine.scheduler.process = SolverProcess(engine.scheduler.backend)
        self._progress_cache.clear()
        self._wall_elapsed = 0.0
        self._sync_orders(records)
        self._progress_cache = {r.order_id: (r.completed, r.status) for r in self._cached_orders}
        for engine in self._engines.values():
            engine.drain_alerts()
            engine.drain_notices()

    # ------------------------------------------------------------------
    # 루프
    # ------------------------------------------------------------------
    @Slot()
    def _tick(self) -> None:
        """타이머 1회: 설정 반영 → 시간 진행 → 주문 동기화 → 상태 emit."""
        now = time.monotonic()
        dt = min(now - self._last_wall, config.MAX_WALL_STEP)
        self._last_wall = now
        try:
            if self._config_dirty:
                self._reload_config()
            if self._running and dt > 0:
                self._wall_elapsed += dt
                target = self.primary.env.now + dt * self._speed
                for engine in self._engines.values():
                    engine.env.run(until=target)
            if now - self._last_flush >= config.ORDER_FLUSH_INTERVAL:
                self._last_flush = now
                self._sync_orders()
                self._flush_progress()
                self._publish_alerts()
            self._publish_notices()
            if now - self._last_emit >= config.STATE_EMIT_INTERVAL:
                self._last_emit = now
                self._emit_state()
        except Exception as exc:  # noqa: BLE001 — 시뮬레이션 오류 시 정지하고 알린다
            traceback.print_exc()
            self._set_running(False)
            self.notice.emit(Severity.CRITICAL, f"시뮬레이션 오류로 일시정지: {exc!r}")

    def _set_running(self, running: bool) -> None:
        """실행 상태 변경."""
        if running != self._running:
            self._running = running
            self.running_changed.emit(running)

    def _reload_config(self) -> None:
        """DB 구성을 다시 읽어 두 엔진에 핫리로드한다."""
        self._config_dirty = False
        factory = self._db.load_factory_config()
        summaries = {mode: engine.apply_config(factory) for mode, engine in self._engines.items()}
        summary = summaries[self._mode]
        if summary:
            self.notice.emit(Severity.INFO, "⚙ 설정 핫리로드 완료: " + ", ".join(summary))

    def _sync_orders(self, records: list[OrderRecord] | None = None) -> None:
        """DB 주문을 두 엔진에 반영한다."""
        records = self._db.list_orders() if records is None else records
        self._cached_orders = list(records)
        for engine in self._engines.values():
            engine.sync_orders(records)

    def _flush_progress(self) -> None:
        """표시 엔진의 주문 진행률을 DB에 기록한다."""
        for order in self.primary.orders.values():
            if order.status == OrderStatus.CANCELLED:
                continue
            key = (order.completed, order.status)
            if self._progress_cache.get(order.order_id) != key:
                self._orders.update_progress(order.order_id, order.completed, order.status)
                self._progress_cache[order.order_id] = key

    def _publish_alerts(self) -> None:
        """표시 엔진이 감지한 이벤트를 production_events에 기록한다 (비교 엔진 이벤트는 버림)."""
        for mode, engine in self._engines.items():
            alerts = engine.drain_alerts()
            if mode != self._mode:
                continue
            for alert in alerts:
                if alert.order_id and alert.event_type in DEDUPED_EVENTS and \
                        self._orders.has_event(alert.event_type, alert.order_id):
                    continue
                self._orders.record_event(alert.event_type, alert.order_id, alert.payload)

    def _publish_notices(self) -> None:
        """표시 엔진의 시스템 알림을 GUI로 보낸다."""
        for mode, engine in self._engines.items():
            notices = engine.drain_notices()
            if mode == self._mode:
                for severity, message in notices:
                    self.notice.emit(severity, message)

    def _emit_state(self) -> None:
        """현재 상태 스냅샷을 내보낸다."""
        engine = self.primary
        params = dict(engine.params)
        params["speed"] = float(self._speed)
        state = SimState(
            sim_time=engine.now,
            sim_datetime=engine.sim_datetime(),
            wall_elapsed=self._wall_elapsed,
            speed=self._speed,
            running=self._running,
            mode=self._mode,
            layout_version=engine.layout_version,
            layout=engine.factory,
            robots=engine.robot_snapshots(),
            stations=engine.station_snapshots(),
            chargers=engine.charger_snapshots(),
            orders=engine.order_snapshots(),
            pending_tasks=len(engine.pending_tasks),
            avg_task_cycle=engine.metrics.avg_cycle_time(),
            kpis=engine.kpis(),
            engine_kpis={mode: e.kpis() for mode, e in self._engines.items()},
            params=params,
            events=list(engine.timeline),
            elevators=engine.elevator_snapshots(),
            planning={mode: e.scheduler.planning_state() for mode, e in self._engines.items()
                      if hasattr(e.scheduler, "planning_state")},
            active_policies=tuple(self._active_policies),
            diagnostics={"command_seq": self._command_sequence, "command_sent": self._command_sent,
                         "worker_command_ms": self._command_latency[-1] if self._command_latency else 0},
        )
        self.state_ready.emit(state)

    # ------------------------------------------------------------------
    # 명령
    # ------------------------------------------------------------------
    @Slot(str, object)
    def handle_command(self, kind: str, params: Any) -> None:
        """GUI/에이전트 명령을 처리한다.

        Args:
            kind: ActionKind.* (SIM_COMMANDS)
            params: 명령 파라미터 dict
        """
        params = dict(params or {})
        sent = params.pop("_sent_monotonic", None)
        if sent is not None:
            self._command_sent = float(sent)
            self._command_sequence += 1
            self._command_latency.append((time.monotonic() - self._command_sent) * 1000)
        try:
            self._dispatch_command(kind, params)
        except Exception as exc:  # noqa: BLE001 — 잘못된 명령이 워커를 죽이지 않도록
            traceback.print_exc()
            self.notice.emit(Severity.WARNING, f"명령 실패 ({kind}): {exc}")
        self._emit_state()

    def _dispatch_command(self, kind: str, params: dict[str, Any]) -> None:
        """명령 종류별 처리."""
        engine = self.primary
        match kind:
            case ActionKind.START:
                self._last_wall = time.monotonic()
                self._set_running(True)
            case ActionKind.PAUSE:
                self._set_running(False)
            case ActionKind.STOP:
                self._set_running(False)
                self._build_engines()
                self.notice.emit(Severity.INFO, "⏹ 시뮬레이션 정지 — 초기 상태로 되돌렸습니다.")
            case ActionKind.RESET:
                self._build_engines()
                self.notice.emit(Severity.INFO, "↺ 시뮬레이션 리셋 (DB 최신 구성으로 재구성)")
            case ActionKind.SET_SPEED:
                value = float(params.get("speed", config.DEFAULT_SPEED))
                self._speed = min(config.SPEED_OPTIONS, key=lambda s: abs(s - value))
            case ActionKind.SET_MODE:
                mode = str(params.get("mode", config.DEFAULT_SCHEDULER_MODE))
                if mode not in config.SCHEDULER_MODES:
                    raise ValueError(f"알 수 없는 모드: {mode}")
                if mode not in self._engines:
                    # Activation starts a new paired comparison from time zero.
                    factory = None if self._config_dirty else self.primary.factory
                    records = None if self._config_dirty else self._cached_orders
                    self._active_policies = list(config.DEFAULT_ACTIVE_POLICIES) + [mode]
                    self._build_engines(factory, records)
                    self.notice.emit(Severity.INFO, "비교 정책 활성화 — 모든 활성 정책을 0초부터 다시 시작합니다.")
                self._mode = mode
                self._progress_cache.clear()
                self.notice.emit(Severity.INFO, f"스케줄링 모드 → {SchedulerModeLabel.LABELS.get(mode, mode)}")
            case ActionKind.SET_PLANNING:
                from simulation.planning.contracts import Settings
                settings = Settings(**params)
                settings.validate()
                self._planning_settings = settings
                for candidate in self._engines.values():
                    if hasattr(candidate.scheduler, "settings"):
                        candidate.scheduler.notify_event("settings")
                        candidate.scheduler.settings = settings
                self.notice.emit(Severity.INFO, "RH 계획 설정을 적용했습니다.")
            case ActionKind.SET_TASK_RATE | ActionKind.SCALE_TASK_RATE:
                if kind == ActionKind.SET_TASK_RATE:
                    rate = float(params["rate"])
                else:
                    rate = self._task_rate * float(params.get("factor", TASK_RATE_SCALE_UP))
                for e in self._engines.values():
                    self._task_rate = e.update_param("task_rate", rate)
                self.notice.emit(Severity.INFO, f"작업 생성 빈도 λ = {self._task_rate:.3f} /s")
            case ActionKind.FORCE_CHARGE:
                robot = engine.robots.get(str(params.get("robot_id")))
                if robot is None:
                    raise KeyError(f"로봇 {params.get('robot_id')} 없음")
                sent = engine.send_to_charge(robot)
                self.notice.emit(Severity.INFO, f"🔋 {robot.robot_id} 강제 충전 " + ("이동 시작" if sent else "(이미 충전 중)"))
            case ActionKind.EMERGENCY_PLAN:
                self._sync_orders()
                results = engine.apply_emergency_plan(params)
                if results:
                    self.notice.emit(Severity.INFO, "✅ 자동 조치 완료: " + " / ".join(results))
            case ActionKind.RECALL_ROBOTS:
                recalled = [rid for rid in params.get("robot_ids", [])
                            if rid in engine.robots and engine.recall(engine.robots[rid])]
                if recalled:
                    self.notice.emit(Severity.INFO, f"✅ 긴급 복귀: {', '.join(recalled)}")
            case ActionKind.PRECHARGE:
                sent = engine.precharge()
                self.notice.emit(Severity.INFO, "🔋 선충전: " + (", ".join(sent) if sent else "대상 로봇 없음"))
            case ActionKind.SET_MIN_CHARGE_MODE:
                engine.update_param("min_charge_mode", float(bool(params.get("enabled", True))))
            case ActionKind.RELOAD_CONFIG:
                self._config_dirty = True
            case ActionKind.SYNC_ORDERS:
                self._sync_orders()
            case _:
                raise ValueError(f"알 수 없는 명령: {kind}")


class SimWorker(QThread):
    """SimPy 실행 스레드."""

    state_ready = Signal(object)          # SimState
    notice = Signal(str, str)             # (severity, message)
    running_changed = Signal(bool)
    command_requested = Signal(str, object)

    def __init__(self, db_path: str, parent: QObject | None = None) -> None:
        """워커를 생성한다.

        Args:
            db_path: DB 경로
            parent: Qt 부모
        """
        super().__init__(parent)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._ready = False
        self._backlog: list[tuple[str, dict[str, Any]]] = []

    def send_command(self, kind: str, params: dict[str, Any] | None = None) -> None:
        """워커에 명령을 보낸다. 스레드 준비 전이면 준비 후 실행된다.

        Args:
            kind: ActionKind.*
            params: 파라미터
        """
        payload = dict(params or {})
        payload["_sent_monotonic"] = time.monotonic()
        with self._lock:
            if not self._ready:
                self._backlog.append((kind, payload))
                return
        self.command_requested.emit(kind, payload)

    def run(self) -> None:
        """워커 스레드 이벤트 루프."""
        controller = _SimController(self._db_path)
        self.command_requested.connect(controller.handle_command)
        controller.state_ready.connect(self.state_ready)
        controller.notice.connect(self.notice)
        controller.running_changed.connect(self.running_changed)
        controller.start_loop()
        with self._lock:
            self._ready = True
            backlog, self._backlog = self._backlog, []
        for kind, params in backlog:
            controller.handle_command(kind, params)
        self.exec()
        with self._lock:
            self._ready = False
        self.command_requested.disconnect(controller.handle_command)
        controller.shutdown()
