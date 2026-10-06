"""기존 관제 명령을 유지하는 검색 전용 RAG 에이전트."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent.base import BaseAgent
from agent.mock_agent import MockAgent
from agent.models import AgentResponse, BottleneckReport, ProductionEvent
from agent.rag.service import RagService


class RagAgent(BaseAgent):
    """명시적 문서 질문만 검색하며 검색 결과로 제어 동작을 만들지 않는다."""

    name = "rag"

    def __init__(self, service: RagService | Callable[[], RagService], fallback: BaseAgent | None = None) -> None:
        """기존 관제와 검색 서비스를 설정한다.

        Args:
            service: 서비스 또는 작업 스레드에서 실행할 지연 생성 함수.
            fallback: 현재 상태·운영 명령·생산 이벤트 처리 에이전트.
        """
        self._service = service
        self._fallback = fallback or MockAgent()

    async def process_message(self, user_msg: str, sim_context: dict[str, Any]) -> AgentResponse:
        """검색 접두사가 있으면 RAG, 그 외는 기존 관제로 처리한다.

        Args:
            user_msg: `/검색 질문` 또는 `문서: 질문` 등 사용자 입력.
            sim_context: 현재 시뮬레이션 상태.

        Returns:
            검색에는 actions가 없는 답변.
        """
        message = user_msg.strip()
        prefix = next((item for item in ("/검색", "/rag", "문서:")
                       if message == item or message.startswith(item + " ")
                       or (item == "문서:" and message.startswith(item))), None)
        if prefix is None:
            return await self._fallback.process_message(user_msg, sim_context)
        question = message[len(prefix):].strip()
        if not question:
            return AgentResponse("사용법: /검색 충전 대기시간을 줄이는 방법", source="rag")
        if callable(self._service):
            self._service = self._service()
        answer = self._service.ask(question)
        return AgentResponse(answer.text, source="rag")

    async def handle_production_event(self, event: ProductionEvent, sim_context: dict[str, Any]) -> AgentResponse:
        """생산 이벤트를 기존 관제에 전달한다.

        Args:
            event: 생산 이벤트.
            sim_context: 상태 스냅샷.

        Returns:
            기존 에이전트의 대응안.
        """
        return await self._fallback.handle_production_event(event, sim_context)

    async def analyze_bottleneck(self, sim_context: dict[str, Any]) -> BottleneckReport:
        """현재 병목 분석을 기존 관제에 전달한다.

        Args:
            sim_context: 상태 스냅샷.

        Returns:
            현재 상태 기반 병목 보고서.
        """
        return await self._fallback.analyze_bottleneck(sim_context)
