"""DB 생산 이벤트 내보내기와 RAG 검색 연결을 검증한다."""

from __future__ import annotations

import tempfile
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from agent.rag.documents import load_documents
from agent.rag.providers import HashEmbedder
from agent.rag.service import RagService
from agent.rag.store import ChromaStore
from database.db_manager import DBManager
from database.seed_data import build_default_config
from simulation.engine import SimulationEngine
from simulation.event_export import export_events
from utils.config_models import OrderRecord


class EventExportTests(unittest.TestCase):
    """이벤트 처리 상태를 건드리지 않고 출처를 보존하는지 검사한다."""

    def test_export_includes_processed_and_unprocessed_without_consuming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = DBManager(root / "factory.db")
            db.initialize()
            first = db.insert_event("equipment_failure", None, {"station_id": "WS-03", "text": "도장 설비 고장"})
            db.mark_events_processed([first])
            second = db.insert_event("critical_order", "ORD-001", {"text": "긴급 주문", "robot_id": "R-02"})
            db.close()
            output = root / "events.jsonl"
            report = export_events(root / "factory.db", output, "dataset-A", "simulation", batch_size=1)
            self.assertEqual(report.count, 2)
            self.assertEqual(report.last_event_id, second)
            documents = load_documents(output)
            self.assertEqual([item.metadata["event_id"] for item in documents], [first, second])
            self.assertTrue(all(item.metadata["dataset_id"] == "dataset-A" for item in documents))
            self.assertTrue(all(item.metadata["synthetic"] for item in documents))
            self.assertNotIn("run_id", documents[0].metadata)
            self.assertEqual(documents[1].metadata["robot_id"], "R-02")
            reader = DBManager(root / "factory.db", read_only=True)
            self.assertEqual([event["id"] for event in reader.fetch_unprocessed_events()], [second])
            reader.close()
            service = RagService(ChromaStore(root / "vectors", HashEmbedder()))
            service.ingest(documents)
            answer = service.ask("도장 설비 고장", where={"dataset_id": "dataset-A"}, top_k=1)
            self.assertEqual(answer.sources[0].chunk.metadata["event_id"], first)
            self.assertIn("dataset-A", answer.text)
            self.assertEqual(service.ask("도장 설비 고장", where={"dataset_id": "other"}).sources, [])

    def test_missing_db_is_not_created_and_existing_output_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = root / "missing.db"
            with self.assertRaises(FileNotFoundError):
                export_events(missing, root / "events.jsonl", "A", "simulation")
            self.assertFalse(missing.exists())
            db = DBManager(root / "factory.db")
            db.initialize()
            db.close()
            output = root / "events.jsonl"
            output.write_text("existing\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                export_events(root / "factory.db", output, "A", "simulation")
            self.assertEqual(output.read_text(), "existing\n")

    def test_id_range_and_invalid_export_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = DBManager(root / "factory.db")
            db.initialize()
            ids = [db.insert_event("equipment_failure", None, {"text": f"설비 고장 {i}"}) for i in range(3)]
            self.assertEqual([row["id"] for row in db.list_events(ids[0], ids[1], 1)], [ids[1]])
            db.close()
            report = export_events(root / "factory.db", root / "last.jsonl", "A", "simulation", after_id=ids[1])
            self.assertEqual(report.count, 1)
            for label, origin, batch in [("", "simulation", 1), ("A", "bad", 1), ("A", "simulation", 0)]:
                with self.assertRaises(ValueError):
                    export_events(root / "factory.db", root / "invalid.jsonl", label, origin, batch_size=batch)

    def test_read_only_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "factory.db"
            db = DBManager(path)
            db.initialize()
            db.close()
            reader = DBManager(path, read_only=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    reader.insert_event("equipment_failure", None, {})
                self.assertEqual(reader.latest_event_id(), 0)
            finally:
                reader.close()

    def test_simpy_alert_to_db_to_cli_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            epoch = datetime(2026, 10, 5, 9)
            engine = SimulationEngine(build_default_config(), "optimized", seed=42, epoch=epoch)
            order = OrderRecord("ORD-LOG-001", "제품B", 500, priority="critical",
                                deadline=epoch + timedelta(seconds=1), created_at=epoch)
            engine.sync_orders([order])
            engine.env.run(until=10)
            alerts = engine.drain_alerts()
            self.assertTrue(any(alert.event_type == "deadline_risk" for alert in alerts))
            db = DBManager(root / "factory.db")
            db.initialize()
            for alert in alerts:
                db.insert_event(alert.event_type, alert.order_id, alert.payload, changed_by="simulation")
            db.close()

            def run_cli(arguments: list[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, "-m", "agent.rag.cli", *arguments],
                                      cwd=Path(__file__).resolve().parents[1], capture_output=True,
                                      text=True, timeout=30, check=False,
                                      env={**os.environ, "CLOVER_RAG_PROVIDER": "local", "CLOVER_RAG_ALLOW_NETWORK": "0"})

            output = root / "simpy.jsonl"
            result = run_cli(["export-events", str(root / "factory.db"), str(output),
                              "--dataset-id", "simpy-check", "--origin", "simulation"])
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_cli(["--store", str(root / "vectors"), "ingest", str(output)])
            self.assertEqual(result.returncode, 0, result.stderr)
            result = run_cli(["--store", str(root / "vectors"), "ask", "납기 지연 위험",
                              "--dataset-id", "simpy-check", "--top-k", "1"])
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("deadline_risk", result.stdout)
            self.assertIn("합성 데이터", result.stdout)
            self.assertIn("simpy-check", result.stdout)

    def test_unknown_origin_does_not_claim_real_or_simulation_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = DBManager(root / "factory.db")
            db.initialize()
            db.insert_event("equipment_failure", None, {"text": "고장"})
            db.close()
            output = root / "unknown.jsonl"
            export_events(root / "factory.db", output, "A", "unknown")
            document = load_documents(output)[0]
            self.assertEqual(document.metadata["origin"], "unknown")
            self.assertNotIn("synthetic", document.metadata)


if __name__ == "__main__":
    unittest.main()
