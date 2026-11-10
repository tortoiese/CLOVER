"""GUI에서 실제 SDK와 로컬 HTTP 목 서버를 거치는 검색 흐름을 검증한다."""

from __future__ import annotations

from pathlib import Path
from secrets import token_hex
from threading import Thread

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QLabel, QLineEdit, QPushButton

import config
from agent.models import AgentResponse
from agent.rag.factory import create_agent
from agent.rag.mock_server import MOCK_ANSWER_PREFIX, MockRagServer
from agent.rag.providers import OpenAIEmbedder
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService
from utils.event_types import Severity


@pytest.mark.parametrize(("accepted_key", "delay"), [(True, 0.0), (False, 0.0), (True, 1.5)],
                         ids=["answer-with-source", "http-401", "response-timeout"])
def test_gui_uses_http_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted_key: bool, delay: float) -> None:
    """정상 답변과 출처, 인증 실패, 답변 타임아웃 뒤 원문 표시를 확인한다."""
    key = token_hex(16)
    document = tmp_path / "factory-manual.md"
    question = "파란 컨베이어 점검코드 일공일"
    document.write_text(f"{question}\n야간에는 이 점검 절차를 사용합니다.", encoding="utf-8")
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    with MockRagServer(0, key, delay) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            monkeypatch.setenv("OPENAI_BASE_URL", server.base_url)
            monkeypatch.setenv("NO_PROXY", "127.0.0.1")
            monkeypatch.setenv("no_proxy", "127.0.0.1")
            monkeypatch.setattr(config, "RAG_ENABLED", True)
            monkeypatch.setattr(config, "RAG_PROVIDER", "openai")
            monkeypatch.setattr(config, "RAG_ALLOW_NETWORK", True)
            monkeypatch.setattr(config, "RAG_API_KEY", key if accepted_key else token_hex(16))
            monkeypatch.setattr(config, "RAG_API_TIMEOUT", 0.5)
            monkeypatch.setattr(config, "RAG_EMBEDDING_MODEL", "clover-mock-embedding")
            monkeypatch.setattr(config, "RAG_CHAT_MODEL", "clover-mock-chat")
            monkeypatch.setattr(config, "RAG_DATA_PATH", tmp_path / "vectors")
            monkeypatch.setattr(config, "RAG_DOCUMENT_PATH", document)
            response, ui_ticks = _search_from_console(service, question)
        finally:
            server.shutdown()
            thread.join(timeout=5)

    assert response.source == "rag"
    assert response.actions == []
    assert key not in response.text
    if not accepted_key:
        assert response.severity == Severity.WARNING
        assert "API 인증에 실패" in response.text
        assert "출처:" not in response.text
    else:
        assert "출처:" in response.text and document.name in response.text
        if delay:
            assert "API 응답 시간이 초과" in response.text
            assert question in response.text and MOCK_ANSWER_PREFIX not in response.text
            assert ui_ticks > 3
        else:
            assert response.text.startswith(MOCK_ANSWER_PREFIX)
            assert question in response.text and "[1]" in response.text


def _search_from_console(service: FactoryService, question: str) -> tuple[AgentResponse, int]:
    window = MainWindow(service, create_agent())
    window.show()
    responses: list[AgentResponse] = []
    ticks: list[None] = []
    loop = QEventLoop()
    heartbeat = QTimer(window)
    heartbeat.setInterval(40)
    heartbeat.timeout.connect(lambda: ticks.append(None))

    def send() -> None:
        heartbeat.start()
        window._console.findChild(QLineEdit).setText(f"/검색 {question}")
        window._console.findChild(QPushButton, "Primary").click()

    def receive(response: AgentResponse) -> None:
        responses.append(response)
        loop.quit()

    timeout = QTimer(window)
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(10_000)
    window._agent_bridge.response_ready.connect(receive)
    QTimer.singleShot(150, send)
    try:
        loop.exec()
        assert len(responses) == 1
        bubbles = window._console.findChildren(QLabel, "BubbleText")
        assert bubbles[-1].text() == responses[0].text
        return responses[0], len(ticks)
    finally:
        heartbeat.stop()
        timeout.stop()
        window.close()
        window._agent_bridge.wait(5000)
        window._worker.wait(5000)


def test_embedding_namespaces_different_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """모델 ID가 같아도 목 서버와 기본 공급자는 컬렉션을 공유하지 않는다."""
    key = token_hex(16)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    normal = OpenAIEmbedder(key, "same-model", allow_network=True)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:8765/v1")
    mock = OpenAIEmbedder(key, "same-model", allow_network=True)
    assert normal.fingerprint == "openai-same-model"
    assert mock.fingerprint != normal.fingerprint
