"""BaseAgent — AI 관제 에이전트 확장 포인트.

MockAgent(규칙 기반)를 RAG + LLM 오케스트레이션 에이전트로 교체할 때 이 인터페이스를 구현한다.
에이전트는 시뮬레이션을 직접 조작하지 않고 AgentAction 목록을 반환하며, 실행은 GUI(MainWindow)가 맡는다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent.models import AgentResponse, BottleneckReport, ProductionEvent


class BaseAgent(ABC):
    """관제 에이전트 추상 클래스."""

    name: str = "base"

    @abstractmethod
    async def process_message(self, user_msg: str, sim_context: dict[str, Any]) -> AgentResponse:
        """사용자 채팅 메시지를 처리한다.

        Args:
            user_msg: 사용자 입력
            sim_context: 시뮬레이션 상태 dict

        Returns:
            AgentResponse
        """

    @abstractmethod
    async def handle_production_event(self, event: ProductionEvent, sim_context: dict[str, Any]) -> AgentResponse:
        """생산 이벤트를 자율 처리한다.

        Args:
            event: 생산 이벤트
            sim_context: 시뮬레이션 상태 dict

        Returns:
            AgentResponse
        """

    @abstractmethod
    async def analyze_bottleneck(self, sim_context: dict[str, Any]) -> BottleneckReport:
        """병목 지점을 분석한다.

        Args:
            sim_context: 시뮬레이션 상태 dict

        Returns:
            BottleneckReport
        """
