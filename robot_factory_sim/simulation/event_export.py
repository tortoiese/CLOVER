"""공장 DB의 생산 이벤트를 RAG 입력용 JSONL로 내보낸다."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from database.db_manager import DBManager

EVENT_LABELS = {
    "critical_order": "긴급 주문 등록",
    "deadline_risk": "납기 지연 위험",
    "demand_surge": "생산 요청 급증",
    "equipment_failure": "설비 고장",
    "equipment_restored": "설비 복구",
    "order_cancelled": "주문 취소",
    "order_reduced": "주문 수량 감소",
    "order_completed": "주문 완료",
}


@dataclass(frozen=True)
class ExportReport:
    """내보내기 결과와 증분 조회를 위한 마지막 이벤트 ID."""

    count: int
    last_event_id: int
    upper_event_id: int
    output: Path


def _event_row(event: dict[str, Any], dataset_id: str, origin: str, db_path: Path) -> dict[str, Any]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {"raw": payload}
    event_type = str(event["event_type"])
    row = {
        "event_id": int(event["id"]), "event_type": event_type, "dataset_id": dataset_id,
        "timestamp": str(event["created_at"]), "time_basis": "db_created_at",
        "origin": origin, "db_source": str(db_path), "order_id": event.get("order_id"),
        "payload": payload, "is_processed": bool(event.get("is_processed")),
        "text": EVENT_LABELS.get(event_type, event_type) + " · " + json.dumps(payload, ensure_ascii=False),
    }
    if origin != "unknown":
        row["synthetic"] = origin == "simulation"
    for key in ("robot_id", "station_id", "scenario"):
        if isinstance(payload.get(key), (str, int, float, bool)):
            row[key] = payload[key]
    return row


def export_events(db_path: Path, output: Path, dataset_id: str, origin: str,
                  after_id: int = 0, batch_size: int = 500) -> ExportReport:
    """기존 DB의 이벤트를 새 JSONL 파일로 출력한다.

    Args:
        db_path: 존재하는 공장 DB.
        output: 새 출력 파일. 기존 파일은 덮어쓰지 않는다.
        dataset_id: 사용자가 지정하는 자료 묶음 이름. 실제 실행 ID가 아니다.
        origin: simulation, enterprise, unknown 중 사용자가 확인한 출처.
        after_id: 증분 조회의 제외할 마지막 이벤트 ID.
        batch_size: 한 번에 읽는 이벤트 수.

    Returns:
        출력 수와 마지막 이벤트 ID.
    """
    if not dataset_id.strip() or origin not in {"simulation", "enterprise", "unknown"}:
        raise ValueError("자료 묶음 이름과 유효한 origin이 필요합니다.")
    if after_id < 0 or batch_size < 1:
        raise ValueError("after_id는 0 이상, batch_size는 양수여야 합니다.")
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"DB 파일이 없습니다: {db_path}")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"기존 파일을 덮어쓰지 않습니다: {output}")
    db = DBManager(db_path, read_only=True)
    try:
        upper = db.latest_event_id()
        cursor, count = after_id, 0
        # DB schema 오류를 출력 파일 생성 전에 확인한다.
        events = db.list_events(cursor, upper, batch_size)
        with output.open("x", encoding="utf-8") as stream:
            while events:
                for event in events:
                    row = _event_row(event, dataset_id, origin, db_path)
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                count += len(events)
                cursor = int(events[-1]["id"])
                events = db.list_events(cursor, upper, batch_size)
        return ExportReport(count, cursor, upper, output)
    finally:
        db.close()
