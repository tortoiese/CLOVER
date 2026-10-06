"""환경 설정에 따른 RAG 서비스와 에이전트 생성."""

from __future__ import annotations

from pathlib import Path

import config
from agent.base import BaseAgent
from agent.mock_agent import MockAgent
from agent.rag.documents import load_documents
from agent.rag.providers import HashEmbedder, OpenAIAnswerer, OpenAIEmbedder
from agent.rag.service import RagService
from agent.rag.store import ChromaStore


def build_service(path: Path | None = None) -> RagService:
    """네트워크 설정과 임베딩 공간을 확인해 서비스를 생성한다.

    Args:
        path: 선택적 실행 데이터 위치.

    Returns:
        설정된 검색 서비스.
    """
    answerer = None
    if config.RAG_PROVIDER == "local":
        embedder = HashEmbedder()
    elif config.RAG_PROVIDER == "openai":
        embedder = OpenAIEmbedder(config.RAG_API_KEY, config.RAG_EMBEDDING_MODEL, config.RAG_ALLOW_NETWORK)
        answerer = OpenAIAnswerer(config.RAG_API_KEY, config.RAG_CHAT_MODEL, config.RAG_ALLOW_NETWORK)
    else:
        raise ValueError("CLOVER_RAG_PROVIDER는 local 또는 openai여야 합니다.")
    return RagService(ChromaStore(path or config.RAG_DATA_PATH, embedder), answerer, config.RAG_MIN_SCORE)


def _build_gui_service() -> RagService:
    service = build_service()
    if service.store.count() == 0:
        source = config.RAG_DOCUMENT_PATH or config.BASE_DIR.parent / "README.md"
        service.ingest(load_documents(source))
    return service


def create_agent() -> BaseAgent:
    """GUI용 에이전트를 생성한다. 무거운 RAG 초기화는 작업 스레드까지 지연한다.

    Returns:
        기본 MockAgent 또는 opt-in RagAgent.
    """
    if not config.RAG_ENABLED:
        return MockAgent()
    from agent.rag_agent import RagAgent

    return RagAgent(_build_gui_service)
