"""알림 상태가 바뀌어도 메인 작업 영역과 도면 배율이 유지되는지 검증한다."""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QSplitter

import config
from agent.mock_agent import MockAgent
from gui import experiment_panel
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService
from simulation.period_experiment import PeriodResult
from utils.config_models import FactoryConfig, OrderRecord
from utils.event_types import Severity


def _wait(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _wait_until(predicate: Callable[[], bool], timeout: float = 5) -> None:
    """Process Qt events until the actual state is ready, with a bounded deadline."""
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        QTest.qWait(20)
    assert predicate(), "GUI state did not become ready before the deadline"


def test_alert_keeps_workspace_size_and_selected_floor(tmp_path: Path) -> None:
    """좁은 화면에서 긴 알림을 열고 닫아도 패널 배치와 도면이 움직이지 않는다."""
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    window = MainWindow(service, MockAgent())
    window.setStyleSheet(config.QSS_PATH.read_text(encoding="utf-8"))
    window.resize(1024, 700)
    window.show()
    try:
        _wait_until(lambda: window._worker._ready, timeout=30)
        _wait_until(lambda: window._state is not None and window._factory.stage._state is not None)
        assert window.width() == 1024
        splitter = window.findChild(QSplitter, "WorkspaceSplitter")
        assert window._workspace.currentWidget() is splitter
        assert not window._console.isVisible()
        assert window._workspace.tabText(2) == "기간 실험"
        stage = window._factory.stage
        stage.select_floor(2)
        _wait_until(lambda: stage.detail_mode and stage.current_floor == 2 and stage._progress == 1.0)
        assert stage.detail_mode and stage.current_floor == 2
        sizes = splitter.sizes()
        plan_geometry = stage.plan.geometry()
        scale = stage.plan.transform()

        window._banner.show_alert("긴급 주문 감지", "긴 주문 정보 " * 60, Severity.WARNING)
        _wait(50)
        assert window._banner.isVisible()
        assert splitter.sizes() == sizes
        assert stage.plan.geometry() == plan_geometry
        assert stage.plan.transform() == scale

        window._banner.findChild(QPushButton, "BannerView").click()
        _wait(50)
        assert not window._banner.isVisible()
        assert window._workspace.currentWidget() is window._console_page
        assert window._console.isVisible()
        window._workspace.setCurrentWidget(splitter)
        _wait(50)
        assert splitter.sizes() == sizes
        assert stage.plan.geometry() == plan_geometry
        assert stage.plan.transform() == scale
        assert stage.current_floor == 2
    finally:
        window.close()
        window._worker.wait(5000)
        window._agent_bridge.wait(5000)
        app.processEvents()


def test_period_tab_runs_in_worker_and_shows_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """기간 탭에서 실행해 결과 표와 고정된 기간 축을 확인한다."""

    def run_period(_factory: FactoryConfig, orders: list[OrderRecord], _epoch: datetime, task_rate: float, days: int,
                   *, progress: Callable[[int], None], cancelled: Callable[[], bool]) -> PeriodResult:
        assert not cancelled()
        values = {key: 1.0 for key, _, _ in experiment_panel.REPORT_METRICS}
        progress(50)
        return PeriodResult(days, len(orders), task_rate, 0.1, [0.0, float(days)],
                            {"baseline": [0.0, 1.0], "optimized": [0.0, 2.0]},
                            {"baseline": values, "optimized": values})

    monkeypatch.setattr(experiment_panel, "run_period", run_period)
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    window = MainWindow(service, MockAgent())
    window.show()
    try:
        panel = window._experiment
        window._workspace.setCurrentWidget(panel)
        panel._days.setValue(30)
        panel._run.click()
        assert window._period_worker is not None
        assert not panel._run.isEnabled()
        loop = QEventLoop()
        check = QTimer(window)
        check.setInterval(20)
        check.timeout.connect(lambda: loop.quit() if window._period_worker is None else None)
        timer = QTimer(window)
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(5000)
        check.start()
        loop.exec()
        check.stop()
        timer.stop()
        assert window._period_worker is None
        assert panel._run.isEnabled()
        assert "30일 실험 완료" in panel._status.text()
        assert panel._table.item(0, 1).text() == "1"
        assert panel._plot.getViewBox().viewRange()[0] == [0, 30]
    finally:
        window.close()
