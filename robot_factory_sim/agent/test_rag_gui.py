"""실제 Qt 스레드에서 RAG를 실행하고 관제 창에 응답하는지 검증한다."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from agent.models import AgentResponse
from agent.rag.documents import Document
from agent.rag.providers import HashEmbedder
from agent.rag.service import RagService
from agent.rag.store import ChromaStore
from agent.rag_agent import RagAgent
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService


class RagGuiTests(unittest.TestCase):
    """실제 서비스와 GUI 연결을 검증한다."""

    def test_worker_response_reaches_gui_without_control_actions(self) -> None:
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = FactoryService(root / "factory.db")
            factory.ensure_database()

            def build_rag() -> RagService:
                service = RagService(ChromaStore(root / "vectors", HashEmbedder()))
                service.ingest([Document("충전 슬롯 부족으로 대기 증가", "manual.md", "충전")])
                return service

            window = MainWindow(factory, RagAgent(build_rag))
            loop = QEventLoop()
            responses: list[AgentResponse] = []

            def receive(response: AgentResponse) -> None:
                responses.append(response)
                loop.quit()

            window._agent_bridge.response_ready.connect(receive)
            QTimer.singleShot(200, lambda: window._agent_bridge.message_requested.emit("/검색 충전 슬롯", {}))
            timeout = QTimer(window)
            timeout.setSingleShot(True)
            timeout.timeout.connect(loop.quit)
            timeout.start(10000)
            try:
                loop.exec()
                self.assertEqual(len(responses), 1)
                self.assertIn("manual.md", responses[0].text)
                self.assertEqual(responses[0].actions, [])
                self.assertEqual(responses[0].source, "rag")
            finally:
                timeout.stop()
                window.close()
                app.processEvents()


if __name__ == "__main__":
    unittest.main()
