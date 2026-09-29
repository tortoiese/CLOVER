"""AgentBridge — BaseAgent를 별도 QThread에서 실행한다.

MockAgent는 즉시 응답하지만, LLM 기반 에이전트로 교체하면 수 초가 걸릴 수 있으므로 GUI 스레드와 분리한다.
GUI → 브리지: message_requested / event_requested 시그널 (Queued)
브리지 → GUI: response_ready(AgentResponse)
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot

from agent.base import BaseAgent
from agent.models import AgentResponse, ProductionEvent
from utils.event_types import Severity


class _AgentRunner(QObject):
    """에이전트 스레드 안에서 동작하는 실행기."""

    response_ready = Signal(object)

    def __init__(self, agent: BaseAgent) -> None:
        """실행기 생성.

        Args:
            agent: 에이전트
        """
        super().__init__()
        self._agent = agent
        self._loop = asyncio.new_event_loop()

    @Slot(str, object)
    def handle_message(self, text: str, context: Any) -> None:
        """채팅 메시지 처리.

        Args:
            text: 사용자 입력
            context: sim_context
        """
        self._run(self._agent.process_message(text, dict(context or {})))

    @Slot(object, object)
    def handle_event(self, event: Any, context: Any) -> None:
        """생산 이벤트 처리.

        Args:
            event: ProductionEventWatcher가 보낸 dict
            context: sim_context
        """
        self._run(self._agent.handle_production_event(ProductionEvent.from_dict(event), dict(context or {})))

    def _run(self, coroutine: Any) -> None:
        """코루틴 실행 후 결과 전달 (예외는 오류 응답으로)."""
        try:
            response = self._loop.run_until_complete(coroutine)
        except Exception as exc:  # noqa: BLE001 — 에이전트 오류가 스레드를 죽이지 않도록
            traceback.print_exc()
            response = AgentResponse(f"에이전트 처리 중 오류가 발생했습니다: {exc}", severity=Severity.WARNING)
        self.response_ready.emit(response)

    def close(self) -> None:
        """이벤트 루프 종료."""
        self._loop.close()


class AgentBridge(QThread):
    """에이전트 실행 스레드."""

    message_requested = Signal(str, object)
    event_requested = Signal(object, object)
    response_ready = Signal(object)

    def __init__(self, agent: BaseAgent, parent: QObject | None = None) -> None:
        """브리지 생성.

        Args:
            agent: 사용할 에이전트
            parent: Qt 부모
        """
        super().__init__(parent)
        self.agent = agent

    def run(self) -> None:
        """스레드 이벤트 루프."""
        runner = _AgentRunner(self.agent)
        self.message_requested.connect(runner.handle_message)
        self.event_requested.connect(runner.handle_event)
        runner.response_ready.connect(self.response_ready)
        self.exec()
        self.message_requested.disconnect(runner.handle_message)
        self.event_requested.disconnect(runner.handle_event)
        runner.close()
