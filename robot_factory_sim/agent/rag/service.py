"""질의 → 검색 → 근거 답변과 출처를 구성한다."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from agent.rag.documents import Chunk, Document, chunk_document
from agent.rag.providers import Answerer
from agent.rag.store import ChromaStore, SearchHit


@dataclass(frozen=True)
class RagAnswer:
    """표시할 답변과 실제 검색된 근거 목록."""

    text: str
    sources: list[SearchHit]
    generated: bool = False


class RagService:
    """로더와 GUI에 독립적인 RAG 핵심 서비스."""

    def __init__(self, store: ChromaStore, answerer: Answerer | None = None, min_score: float = 0.15) -> None:
        """검색 및 답변 구성을 설정한다.

        Args:
            store: 벡터 저장소.
            answerer: 선택적 LLM 생성기. 없으면 원문 근거만 표시한다.
            min_score: 근거 채택 유사도 임계값. 데이터에 맞춰 평가 후 조정한다.
        """
        self.store = store
        self.answerer = answerer
        self.min_score = min_score

    def ingest(self, documents: list[Document]) -> int:
        """입력에 포함된 각 출처를 최신 버전으로 등록한다.

        Args:
            documents: 출처별 전체 문서. 같은 출처 일부만 입력하면 이전 부분은 제거된다.

        Returns:
            등록한 청크 수.
        """
        groups: dict[str, list[Chunk]] = defaultdict(list)
        for document in documents:
            groups[document.source].extend(chunk_document(document))
        for source, chunks in groups.items():
            self.store.replace_source(source, list({chunk.id: chunk for chunk in chunks}.values()))
        return sum(len(chunks) for chunks in groups.values())

    def ask(self, question: str, where: dict[str, Any] | None = None, top_k: int = 4) -> RagAnswer:
        """질문을 검색하고 답변과 출처를 반환한다.

        Args:
            question: 사용자 질문.
            where: 실행 ID 등 검색 범위.
            top_k: 검색 결과 수.

        Returns:
            근거가 없으면 명시적인 무응답, 그 외 근거 답변.
        """
        hits = self.store.search(question, top_k, where, self.min_score)
        if not hits:
            return RagAnswer("관련 근거를 찾지 못했습니다. 자료 등록 여부와 질문·실행 ID를 확인하세요.", [])
        entries = [{"citation": i, "text": hit.chunk.text, "metadata": hit.chunk.metadata}
                   for i, hit in enumerate(hits, 1)]
        citations = []
        for index, hit in enumerate(hits, 1):
            metadata = hit.chunk.metadata
            location = f" · p.{metadata['page']}" if "page" in metadata else ""
            location += f" · 행 {metadata['line']}" if "line" in metadata else ""
            location += f" · 실행 {metadata['run_id']}" if "run_id" in metadata else ""
            location += f" · 자료 {metadata['dataset_id']}" if "dataset_id" in metadata else ""
            location += f" · 이벤트 {metadata['event_id']}" if "event_id" in metadata else ""
            location += f" · 출처 유형 {metadata['origin']}" if "origin" in metadata else ""
            location += " · 합성 데이터" if metadata.get("synthetic") else ""
            citations.append(f"[{index}] {metadata['source']}{location} · 문자 {metadata['offset']}")
        text = "검색 근거 (LLM 생성 아님):\n" + "\n\n".join(
            f"[{index}] {hit.chunk.text}" for index, hit in enumerate(hits, 1))
        generated = False
        if self.answerer:
            try:
                candidate = self.answerer.generate(question, json.dumps(entries, ensure_ascii=False))
                cited = {int(number) for number in re.findall(r"\[(\d+)\]", candidate)}
                if candidate.strip() and cited and cited <= set(range(1, len(hits) + 1)):
                    text, generated = candidate, True
                else:
                    text = "생성 답변의 출처를 확인할 수 없어 검색 근거를 표시합니다.\n" + text
            except Exception:
                text = "답변 생성에 실패해 검색 근거를 표시합니다.\n" + text
        return RagAnswer(text + "\n\n출처:\n" + "\n".join(citations), hits, generated)
