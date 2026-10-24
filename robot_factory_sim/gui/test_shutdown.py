"""요청 처리 중 창 종료가 QThread 종료까지 보류되는지 검증한다."""

import asyncio
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from agent.mock_agent import MockAgent
from agent.models import AgentAction, AgentResponse
from gui import main_window
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService
from utils.event_types import ActionKind


class SlowAgent(MockAgent):
    """종료 요청보다 늦게 응답하는 테스트 에이전트."""

    async def process_message(self, user_msg: str, sim_context: dict[str, Any]) -> AgentResponse:
        """진행 중인 요청의 종료 지연과 조치 응답을 재현한다."""
        await asyncio.sleep(0.4)
        return AgentResponse("late", actions=[AgentAction(ActionKind.CREATE_ORDER, {})])


def test_close_waits_for_agent_and_discards_late_actions(tmp_path: Path, monkeypatch: Any) -> None:
    """타임아웃 뒤 창을 보존하고 최종 응답의 DB 조치를 실행하지 않는다."""
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    monkeypatch.setattr(main_window, "THREAD_STOP_TIMEOUT_MS", 10)
    window = MainWindow(service, SlowAgent())
    window.show()
    loop = QEventLoop()
    checks: list[bool] = []
    actions: list[AgentAction] = []
    monkeypatch.setattr(window, "_execute_action", actions.append)

    def request() -> None:
        window._agent_bridge.message_requested.emit("test", {})

    def close_during_request() -> None:
        checks.append(not window.close())
        checks.append(window.isVisible() and window._agent_bridge.isRunning())

    QTimer.singleShot(100, request)
    QTimer.singleShot(200, close_during_request)
    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(1200)
    try:
        loop.exec()
        assert checks == [True, True]
        assert not window._agent_bridge.isRunning()
        assert not window._worker.isRunning()
        assert not window.isVisible()
        assert not actions
    finally:
        timer.stop()
        window.close()
        window._agent_bridge.wait(5000)
        window._worker.wait(5000)
        app.processEvents()
