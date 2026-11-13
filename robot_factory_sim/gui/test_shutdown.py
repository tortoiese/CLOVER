"""요청 처리 중 창 종료가 QThread 종료까지 보류되는지 검증한다."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from PySide6.QtCore import QMetaMethod
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication

from agent.mock_agent import MockAgent
from agent.models import AgentAction, AgentResponse
from gui import main_window
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService
from utils.event_types import ActionKind


class ControlledAgent(MockAgent):
    """요청 시작을 알리고 테스트가 허용할 때까지 응답을 보류한다."""

    def __init__(self) -> None:
        """시계 대신 요청 시작·응답 허용 이벤트로 실행 순서를 제어한다."""
        super().__init__()
        self.request_started = Event()
        self.release_response = Event()

    async def process_message(self, user_msg: str, sim_context: dict[str, Any]) -> AgentResponse:
        """진행 중인 요청의 종료 지연과 조치 응답을 재현한다."""
        self.request_started.set()
        # 테스트 실패 시에도 worker가 영원히 남지 않도록 상한만 둔다.
        if not self.release_response.wait(30):
            raise TimeoutError("test did not release the agent response")
        return AgentResponse("late", actions=[AgentAction(ActionKind.CREATE_ORDER, {})])


def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    """Qt 이벤트를 처리하면서 실제 상태 변화를 기다린다."""
    deadline = time.perf_counter() + timeout
    while not predicate() and time.perf_counter() < deadline:
        QTest.qWait(10)
    assert predicate(), "Qt shutdown condition did not become true before the watchdog"


@pytest.mark.parametrize("gui_delay_seconds", [0, 0.6], ids=["responsive", "delayed_gui"])
def test_close_waits_for_agent_and_discards_late_actions(
    tmp_path: Path, monkeypatch: Any, gui_delay_seconds: float,
) -> None:
    """타임아웃 뒤 창을 보존하고 최종 응답의 DB 조치를 실행하지 않는다."""
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    monkeypatch.setattr(main_window, "THREAD_STOP_TIMEOUT_MS", 10)
    agent = ControlledAgent()
    window = MainWindow(service, agent)
    window.show()
    actions: list[AgentAction] = []
    monkeypatch.setattr(window, "_execute_action", actions.append)
    responses = QSignalSpy(window._agent_bridge.response_ready)
    try:
        # isRunning/started만으로는 run()의 요청 슬롯 연결이 보장되지 않는다.
        request_signal = QMetaMethod.fromSignal(window._agent_bridge.message_requested)
        wait_until(lambda: window._agent_bridge.isSignalConnected(request_signal))
        window._agent_bridge.message_requested.emit("test", {})
        wait_until(agent.request_started.is_set)
        # CI에서 GUI 처리가 지연돼도 작업 중 종료라는 전제는 유지된다.
        time.sleep(gui_delay_seconds)
        assert responses.count() == 0
        assert not window.close()
        assert window._closing and not window.isEnabled()
        assert window.isVisible() and window._agent_bridge.isRunning()
        assert not window.close()
        assert window.isVisible() and window._agent_bridge.isRunning()

        agent.release_response.set()
        wait_until(lambda: responses.count() == 1)
        response = responses.at(0)[0]
        assert response.text == "late"
        assert [action.kind for action in response.actions] == [ActionKind.CREATE_ORDER]
        wait_until(lambda: not window.isVisible())
        assert not window._agent_bridge.isRunning()
        assert not window._worker.isRunning()
        assert not actions
    finally:
        agent.release_response.set()
        window.close()
        window._agent_bridge.wait(5000)
        window._worker.wait(5000)
        window.close()
        app.processEvents()
