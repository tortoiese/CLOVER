"""에이전트 입출력 데이터 모델.

sim_context(dict) 구조는 ``simulation.state.state_to_context()`` 참고.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from utils.event_types import Severity


@dataclass
class AgentAction:
    """에이전트가 요청하는 동작 1개. kind는 utils.event_types.ActionKind."""

    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResponse:
    """에이전트 응답."""

    text: str
    actions: list[AgentAction] = field(default_factory=list)
    severity: str = Severity.INFO
    title: str | None = None          # 긴급 배너 제목 (이벤트 응답일 때)
    source: str = "chat"              # "chat" | "event"


@dataclass
class ProductionEvent:
    """production_events 한 행."""

    id: int
    event_type: str
    order_id: str | None
    payload: dict[str, Any]
    created_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProductionEvent:
        """ProductionEventWatcher가 보낸 dict로부터 생성한다.

        Args:
            data: 이벤트 dict

        Returns:
            ProductionEvent
        """
        payload = data.get("payload")
        return cls(
            id=int(data.get("id", 0)),
            event_type=str(data.get("event_type", "")),
            order_id=data.get("order_id"),
            payload=payload if isinstance(payload, dict) else {"raw": payload},
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class BottleneckReport:
    """병목 분석 결과."""

    station_id: str | None
    score: float
    summary: str
    details: list[str] = field(default_factory=list)
