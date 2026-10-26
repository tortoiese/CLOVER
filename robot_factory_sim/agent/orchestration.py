"""검증된 명령으로 조건 설정·비교 실행·근거 설명을 연결한다. 외부 API 없이 동작한다."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, replace
from typing import Any

from agent.models import AgentResponse
from agent.rag.service import RagService
from agent.rag_agent import RagAgent
from utils.comparison_models import ComparisonConditions

FIELD_ALIASES = {
    "기간": "duration", "반복": "repeats", "시드": "seed", "계획구간": "horizon", "재계획": "replan",
    "건당매출": "revenue_per_task", "전력단가": "electricity_per_kwh", "배터리Wh": "wh_per_battery_unit",
    "충전효율": "charge_efficiency", "이동단가": "movement_cost_per_m", "고정비": "fixed_cost", "AI비": "ai_cost",
}
COMPARISON_METRICS = (
    ("completed", "완료 공정 작업", "건"), ("throughput", "처리량", "건/분"),
    ("avg_wait", "작업 대기", "초"), ("charge_wait", "충전 대기", "초"),
    ("distance_m", "로봇 이동", "m"), ("energy_wh", "보충 전력 추정", "Wh"),
    ("total_cost", "투입비용", "원"), ("net_profit", "순이익", "원"),
    ("profit_per_task", "건당 순이익", "원/건"), ("roi", "운영비 기준 ROI", "%"),
    ("profit_per_wh", "전력 대비 순이익", "원/Wh"),
)
HELP_TEXT = (
    "운영안 비교 사용법\n"
    "1. 툴바 [운영안 비교]에서 기간·단가·배터리 Wh 환산값을 설정하세요.\n"
    "2. /최적화 또는 '비용 줄여줘' → 현재 GUI 구성으로 독립 비교 실험\n"
    "3. /비교 → 마지막 수치, /추천 → 추천 이유, /근거 → 운영 기준·ROI 출처\n"
    "채팅 설정 예: /조건 기간=120 반복=3 건당매출=1000 전력단가=150 배터리Wh=1 이동단가=1 고정비=100\n"
    "전력단가: 원/kWh, 배터리Wh: 상대 배터리 1단위당 Wh, 이동단가: 전력 제외 원/m.\n"
    "비용 값은 사용자가 정한 모델이며 실측 검증은 별도입니다. 위치·대수는 GUI 설정에서 변경하세요."
)


def is_comparison_request(message: str) -> bool:
    """GUI와 에이전트가 같은 명령 범위로 비교 기능을 구분한다."""
    message = message.strip()
    head = message.partition(" ")[0]
    if head in {"/검색", "/rag"} or message.startswith("문서:"):
        return False
    # 혼합된 최적화 요청을 기존 관제의 즉시 변경 명령으로 오해하지 않는다.
    optimization_intent = bool(re.search(r"최적화|비용.*(?:줄|절감)|ROI|추천|베이스라인.*비교", message))
    if "모드" in message and any(word in message for word in ("전환", "바꿔")):
        optimization_intent = False
    return (head in {"/조건", "/최적화", "/비교", "/추천", "/ROI", "/근거"}
            or message in {"최적화해줘", "비용 줄여줘", "운영안 비교해줘", "베이스라인 비교해줘",
                           "왜 추천했어?", "ROI 설명해줘", "운영안 도움말"}
            or optimization_intent)


def format_value(value: float | None, unit: str = "") -> str:
    """미측정 값은 숫자로 대체하지 않고 표시한다."""
    return "미산정" if value is None else f"{value:,.2f}{unit}"


def format_report(result: dict[str, Any]) -> str:
    """계산 결과를 변경하지 않고 채팅 비교표로 표시한다."""
    lines = [f"비교 실험 {result['experiment_id'][:8]} · {result['conditions']['repeats']}회 평균",
             f"비용 근거: {result['conditions']['cost_basis']}"]
    for key, label, unit in COMPARISON_METRICS:
        baseline, candidate = result["baseline"][key], result["candidate"][key]
        delta = candidate - baseline if baseline is not None and candidate is not None else None
        delta_unit = "%p" if key == "roi" else unit
        lines.append(f"{label}: {format_value(baseline, unit)} → {format_value(candidate, unit)} "
                     f"(차이 {format_value(delta, delta_unit)})")
    interval = result["intervals"].get("net_profit")
    if interval:
        lines.append(f"순이익 차이의 평균 95% 신뢰구간: {interval['low']:,.2f}~{interval['high']:,.2f}원")
    gap = result["solver"]["max_dispatch_gap"]
    lines.append(f"배정 격차(해당 시): {format_value(gap * 100 if gap is not None else None, '%')}")
    lines.extend([result["recommendation"], result["solver"]["scope"], *result["notes"]])
    return "\n".join(lines)


class OrchestrationAgent(RagAgent):
    """RAG와 계산 함수를 분리하고 검증된 비교 명령만 실행하는 에이전트."""

    name = "rag + 운영안 비교"
    supports_comparison = True

    def __init__(self, service: RagService | Callable[[], RagService] | None,
                 optimizer: Callable[[ComparisonConditions, dict[str, Any]], dict[str, Any]]) -> None:
        """비교 실행 함수를 주입한다. agent는 simulation을 직접 import하지 않는다."""
        super().__init__(service)
        self._optimizer = optimizer
        if service is None:
            self.name = "로컬 운영안 비교"
        self._conditions = ComparisonConditions()
        self._last_result: dict[str, Any] | None = None

    def _evidence(self, question: str) -> str:
        if self._service is None:
            return "로컬 비교 계산 결과입니다. RAG 근거 검색은 비활성 상태입니다."
        if callable(self._service):
            self._service = self._service()
        hits = self._service.store.search(question, 3, {"knowledge_type": "operating_policy"},
                                          self._service.min_score)
        if not hits:
            return "등록된 운영 근거를 찾지 못했습니다. 문서를 등록해 주세요."
        return "운영 근거 (계산 결과와 별개인 문서):\n" + "\n\n".join(
            f"[{i}] {hit.chunk.text}\n출처: {hit.chunk.metadata['source']} · 문자 {hit.chunk.metadata['offset']}"
            for i, hit in enumerate(hits, 1))

    def _update_conditions(self, text: str) -> None:
        values = {}
        for token in text.split():
            if "=" not in token or token.count("=") != 1:
                raise ValueError("조건은 '기간=120 전력단가=150' 형식으로 입력하세요.")
            key, value = token.split("=")
            field = FIELD_ALIASES.get(key)
            if field is None:
                raise ValueError(f"지원하지 않는 조건: {key}. /최적화 도움말을 확인하세요.")
            if field in values:
                raise ValueError(f"조건이 중복되었습니다: {key}")
            values[field] = float(value)
        conditions = replace(self._conditions, **values, cost_basis="사용자 입력 · 실측 미검증")
        conditions.validate()
        self._conditions = conditions

    async def process_message(self, user_msg: str, sim_context: dict[str, Any]) -> AgentResponse:
        """조건·비교 명령을 처리하고 나머지는 기존 검색/관제로 전달한다."""
        message = user_msg.strip()
        head, _, tail = message.partition(" ")
        if message in {"/최적화 도움말", "/비교 도움말", "운영안 도움말"}:
            return AgentResponse(HELP_TEXT, source="comparison")
        optimize = (head == "/최적화" or message in {"최적화해줘", "비용 줄여줘", "운영안 비교해줘"}
                    or bool(re.fullmatch(r"충전(?:기|소)\s*\d+\s*대(?:는)?\s*유지(?:하고)?\s*비용\s*줄여줘", message)))
        if head == "/조건" or optimize:
            try:
                if "comparison_conditions" in sim_context:
                    self._conditions = ComparisonConditions.from_dict(sim_context["comparison_conditions"])
                if head in {"/조건", "/최적화"} and tail:
                    self._update_conditions(tail)
                if head == "/조건":
                    return AgentResponse("비교 조건을 설정했습니다. /최적화로 실행하세요.\n" + str(asdict(self._conditions)),
                                         source="comparison", comparison_conditions=asdict(self._conditions))
                match = re.search(r"충전(?:기|소)\s*(\d+)\s*대", message)
                chargers = sim_context.get("factory_config", {}).get("chargers", [])
                if match and int(match[1]) != sum(bool(c.get("is_active", True)) for c in chargers):
                    raise ValueError("요청한 충전소 대수가 현재 구성과 다릅니다. 공장 설정에서 변경한 뒤 비교하세요.")
                result = self._optimizer(self._conditions, sim_context)
                self._last_result = result
                try:
                    evidence = self._evidence("ROI 비용 산식 최적성 격차")
                except Exception:  # noqa: BLE001 — preserve local computation when evidence retrieval fails
                    evidence = "운영 근거 검색에 실패했습니다. 계산 결과는 보존되며 /근거로 재조회할 수 있습니다."
                return AgentResponse(format_report(result) + "\n\n" + evidence, source="comparison", comparison=result,
                                     comparison_conditions=asdict(self._conditions))
            except (ValueError, TypeError, TimeoutError, InterruptedError) as exc:
                return AgentResponse(f"비교를 실행하지 못했습니다: {exc}", source="comparison")
        if message in {"/비교", "베이스라인 비교해줘", "/추천", "왜 추천했어?", "/ROI", "ROI 설명해줘", "/근거"}:
            if message == "/근거":
                return AgentResponse(self._evidence("ROI 산식 운영 기준 최적성 격차"), source="rag")
            if self._last_result is None:
                return AgentResponse("아직 비교 결과가 없습니다. /최적화로 실행하세요.\n" + HELP_TEXT,
                                     source="comparison")
            text = format_report(self._last_result)
            if message in {"/추천", "왜 추천했어?", "/ROI", "ROI 설명해줘"}:
                text += "\n\n" + self._evidence("ROI 순이익 비용 산식 운영 기준")
            return AgentResponse("마지막 실험의 저장된 결과입니다. 설정·주문이 바뀌면 다시 계산하세요.\n" + text,
                                 source="comparison", comparison=self._last_result)
        if is_comparison_request(message):
            return AgentResponse("지원하지 않는 비교 명령 형식입니다.\n" + HELP_TEXT, source="comparison")
        if self._service is None:
            from agent.mock_agent import MockAgent
            return await MockAgent().process_message(user_msg, sim_context)
        return await super().process_message(user_msg, sim_context)
