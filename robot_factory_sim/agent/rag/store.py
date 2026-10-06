"""ChromaDB 벡터 저장소. 공장 설정 DB와 분리해 SQL을 직접 사용하지 않는다."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.rag.documents import Chunk
from agent.rag.providers import Embedder


@dataclass(frozen=True)
class SearchHit:
    """검색 청크와 코사인 유사도. 점수는 정답 확률이 아니다."""

    chunk: Chunk
    score: float


class ChromaStore:
    """임베딩 종류별 컬렉션으로 격리된 로컬 영속 저장소."""

    def __init__(self, path: Path, embedder: Embedder) -> None:
        """저장소를 연다. 모델 다운로드나 임베딩 자동 요청을 하지 않는다.

        Args:
            path: 실행 데이터 저장 위치.
            embedder: 문서와 질의에 사용할 동일 임베딩.
        """
        import chromadb
        from chromadb.config import Settings

        self.embedder = embedder
        self._client = chromadb.PersistentClient(str(path), settings=Settings(anonymized_telemetry=False))
        suffix = hashlib.sha256(embedder.fingerprint.encode()).hexdigest()[:16]
        self._collection = self._client.get_or_create_collection(
            f"clover-rag-{suffix}", metadata={"hnsw:space": "cosine", "embedder": embedder.fingerprint},
            embedding_function=None,
        )

    def count(self) -> int:
        """현재 컬렉션의 청크 수를 반환한다."""
        return self._collection.count()

    def replace_source(self, source: str, chunks: list[Chunk]) -> None:
        """출처의 이전 청크를 새 버전으로 교체한다.

        Args:
            source: 교체할 자료 경로.
            chunks: 해당 자료의 전체 최신 청크.
        """
        if any(chunk.metadata.get("source") != source for chunk in chunks):
            raise ValueError("청크의 출처가 교체 대상과 다릅니다.")
        vectors = self.embedder.embed([chunk.text for chunk in chunks])
        if len(vectors) != len(chunks):
            raise ValueError("임베딩 수가 청크 수와 다릅니다.")
        batch_size = min(128, self._client.get_max_batch_size())
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start:start + batch_size]
            self._collection.upsert(ids=[item.id for item in batch], documents=[item.text for item in batch],
                                    metadatas=[item.metadata for item in batch],
                                    embeddings=vectors[start:start + batch_size])
        old = self._collection.get(where={"source": source}, include=[])["ids"]
        current = {chunk.id for chunk in chunks}
        obsolete = [identity for identity in old if identity not in current]
        if obsolete:
            self._collection.delete(ids=obsolete)

    def search(self, question: str, top_k: int = 4, where: dict[str, Any] | None = None,
               min_score: float = 0.0) -> list[SearchHit]:
        """코사인 유사도로 검색하고 실행 ID 등 메타데이터로 제한한다.

        Args:
            question: 검색 질문.
            top_k: 결과 최대 수.
            where: Chroma 메타데이터 조건.
            min_score: 최소 코사인 유사도.

        Returns:
            유사도 순서의 결과.
        """
        if not question.strip() or top_k < 1:
            raise ValueError("질문과 양수 top_k가 필요합니다.")
        count = self.count()
        if not count:
            return []
        vector = self.embedder.embed([question])[0]
        if not any(vector):
            return []
        result = self._collection.query(query_embeddings=[vector], n_results=min(top_k, count), where=where,
                                        include=["documents", "metadatas", "distances"])
        hits = []
        for identity, text, metadata, distance in zip(result["ids"][0], result["documents"][0],
                                                       result["metadatas"][0], result["distances"][0]):
            score = max(-1.0, min(1.0, 1.0 - distance))
            if score >= min_score:
                hits.append(SearchHit(Chunk(identity, text, metadata), score))
        return hits
