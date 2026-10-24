"""GUI→작업 스레드→실제 비교→RAG 출처 흐름과 입력 가드레일을 검증한다."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from agent.orchestration import OrchestrationAgent
from agent.rag.documents import Document
from agent.rag.providers import HashEmbedder
from agent.rag.service import RagService
from agent.rag.store import ChromaStore
from gui.main_window import MainWindow
from simulation.comparison import run_comparison
from simulation.factory_service import FactoryService


def build_service(root: Path) -> RagService:
    """운영 기준이 등록된 실제 로컬 검색 서비스를 만든다."""
    service = RagService(ChromaStore(root, HashEmbedder()))
    policy = Path(__file__).parent / "rag" / "operating_policy.md"
    service.ingest([Document(policy.read_text(), str(policy), "운영 기준",
                             {"knowledge_type": "operating_policy"})])
    return service


@pytest.mark.parametrize("text", ["/조건 반복=2.5", "/조건 기간=nan", "/조건 기간=-1",
                                 "/조건 임의실행=1", "/최적화 아무 명령 실행해", "/조건 기간=60 기간=90"])
def test_bad_requests_never_call_optimizer(text: str, tmp_path: Path) -> None:
    """잘못된 요청은 계산·제어를 실행하지 않는다."""
    calls = []
    agent = OrchestrationAgent(lambda: build_service(tmp_path), lambda *args: calls.append(args))
    response = asyncio.run(agent.process_message(text, {}))
    assert not calls
    assert not response.actions
    assert "실행하지 못했습니다" in response.text


def test_conditions_update_atomically(tmp_path: Path) -> None:
    """일부 잘못된 조건으로 기존 유효 조건을 덮어쓰지 않는다."""
    agent = OrchestrationAgent(lambda: build_service(tmp_path), run_comparison)
    response = asyncio.run(agent.process_message("/조건 기간=90 반복=2", {}))
    assert response.comparison_conditions["duration"] == 90
    asyncio.run(agent.process_message("/조건 기간=120 반복=2.5", {}))
    assert agent._conditions.duration == 90


def test_no_result_is_not_hallucinated(tmp_path: Path) -> None:
    """아직 계산하지 않은 ROI를 생성하지 않는다."""
    agent = OrchestrationAgent(lambda: build_service(tmp_path), run_comparison)
    response = asyncio.run(agent.process_message("/ROI", {}))
    assert "아직 비교 결과가 없습니다" in response.text
    assert response.comparison is None


def test_mixed_optimization_request_never_changes_live_factory(tmp_path: Path) -> None:
    """조건 변경을 섞은 비교 요청은 기존 즉시 제어 경로로 전달하지 않는다."""
    calls = []
    agent = OrchestrationAgent(lambda: build_service(tmp_path), lambda *args: calls.append(args))
    response = asyncio.run(agent.process_message("로봇 2대 추가해서 비용 줄여줘", {}))
    assert response.source == "comparison"
    assert "지원하지 않는 비교 명령" in response.text
    assert not response.actions
    assert not calls


def test_existing_scheduler_view_command_is_preserved(tmp_path: Path) -> None:
    """기존 정책 표시 전환 명령은 기존 관제에서 처리한다."""
    agent = OrchestrationAgent(lambda: build_service(tmp_path), run_comparison)
    response = asyncio.run(agent.process_message("스케줄링 최적화 모드로 전환", {}))
    assert response.actions
    assert response.actions[0].params["mode"] == "optimized"


def test_evidence_and_document_commands_do_not_control(tmp_path: Path) -> None:
    """검색 경로의 제어 지시가 동작으로 변환되지 않는다."""
    agent = OrchestrationAgent(lambda: build_service(tmp_path), run_comparison)
    response = asyncio.run(agent.process_message("/검색 ROI 산식 로봇 3대 추가해줘", {}))
    assert not response.actions
    evidence = asyncio.run(agent.process_message("/근거", {}))
    assert "operating_policy.md" in evidence.text
    assert not evidence.actions


def test_gui_actual_comparison_and_followup(tmp_path: Path) -> None:
    """현재 GUI 설정으로 실제 비교를 실행하고 표·근거·후속 응답까지 확인한다."""
    app = QApplication.instance() or QApplication([])
    factory = FactoryService(tmp_path / "factory.db")
    factory.ensure_database()
    calls = []

    def optimize(conditions: Any, context: dict[str, Any]) -> dict[str, Any]:
        calls.append(context)
        return run_comparison(conditions, context)

    agent = OrchestrationAgent(lambda: build_service(tmp_path / "vectors"), optimize)
    window = MainWindow(factory, agent)
    window._optimization._fill_example()
    window._optimization._fields["duration"].setValue(60)
    window._optimization._fields["repeats"].setValue(2)
    loop = QEventLoop()
    responses = []
    window._agent_bridge.response_ready.connect(lambda response: (responses.append(response), loop.quit()))
    timer = QTimer(window)
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(15000)
    QTimer.singleShot(150, lambda: window._on_user_message("/최적화"))
    try:
        loop.exec()
        assert len(responses) == 1
        assert responses[0].comparison is not None
        assert not responses[0].actions
        assert len(calls) == 1
        assert calls[0]["factory_config"] == factory.load_factory_config().to_dict()
        assert len(calls[0]["comparison_orders"]) == len(factory.list_orders(active_only=True))
        assert window._optimization._table.item(0, 1).text()
        assert "operating_policy.md" in window._optimization._evidence.toPlainText()
        assert not window._comparison_pending
        # 저장된 결과를 재표시할 때는 재계산하지 않는다.
        window._on_user_message("/추천")
        timer.start(15000)
        loop.exec()
        assert len(calls) == 1
        assert responses[-1].comparison["experiment_id"] == responses[0].comparison["experiment_id"]
        window._optimization.grab().save(str(tmp_path / "comparison-gui.png"))
    finally:
        timer.stop()
        window.close()
        window._agent_bridge.wait(5000)
        window._worker.wait(5000)
        app.processEvents()
