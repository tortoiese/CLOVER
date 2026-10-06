"""자료 등록·질문·오프라인 데모 CLI. python -m agent.rag.cli --help"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent.rag.documents import Document, load_documents
from agent.rag.factory import build_service


def main() -> int:
    """CLI를 실행한다.

    Returns:
        성공 0, 입력/설정 오류 1.
    """
    parser = argparse.ArgumentParser(description="CLOVER RAG 자료 등록 및 출처 기반 질의")
    parser.add_argument("--store", type=Path, help="벡터 저장 폴더")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="자료 파일 또는 폴더 등록/갱신")
    ingest.add_argument("path", type=Path)
    ask = commands.add_parser("ask", help="자료 질의")
    ask.add_argument("question")
    ask.add_argument("--run-id", help="특정 시뮬레이션 실행으로 제한")
    ask.add_argument("--dataset-id", help="DB 내보내기 자료 묶음으로 제한")
    ask.add_argument("--top-k", type=int, default=4)
    commands.add_parser("demo", help="실제 기업 데이터가 아닌 합성 로그 데모")
    export = commands.add_parser("export-events", help="기존 DB의 생산 이벤트를 JSONL로 내보내기")
    export.add_argument("db", type=Path)
    export.add_argument("output", type=Path)
    export.add_argument("--dataset-id", required=True, help="자료 묶음 이름 (실제 실행 ID가 아님)")
    export.add_argument("--origin", required=True, choices=["simulation", "enterprise", "unknown"])
    export.add_argument("--after-id", type=int, default=0)
    args = parser.parse_args()
    try:
        if args.command == "export-events":
            from simulation.event_export import export_events

            report = export_events(args.db, args.output, args.dataset_id, args.origin, args.after_id)
            print(f"내보내기 완료: {report.count}개 이벤트 / 마지막 ID {report.last_event_id} / {report.output}")
            return 0
        service = build_service(args.store)
        if args.command == "ingest":
            count = service.ingest(load_documents(args.path))
            print(f"등록 완료: {count}개 청크 / 전체 {service.store.count()}개")
        elif args.command == "ask":
            filters = []
            if args.run_id:
                filters.append({"run_id": args.run_id})
            if args.dataset_id:
                filters.append({"dataset_id": args.dataset_id})
            where = {"$and": filters} if len(filters) > 1 else filters[0] if filters else None
            print(service.ask(args.question, where, args.top_k).text)
        else:
            service.ingest([
                Document("충전 대기 증가: 로봇 10대가 충전소 2개를 공유하고 피크 시간에 동시에 충전을 요청했다. "
                         "충전 슬롯 증설과 충전 시점 분산을 비교해야 한다. 이 기록은 검증용 합성 데이터다.",
                         "demo://charge-wait", "충전 병목 데모", {"run_id": "demo-001", "synthetic": True}),
                Document("생산설비 WS-03의 도장 처리시간 증가로 작업 대기열이 늘었다. "
                         "충전기 증설만으로 생산설비 병목은 해결되지 않는다. 검증용 합성 데이터다.",
                         "demo://station-queue", "설비 병목 데모", {"run_id": "demo-001", "synthetic": True}),
            ])
            print(service.ask("충전 대기 증가 원인", {"run_id": "demo-001"}, top_k=1).text)
    except Exception as exc:
        print(f"RAG 실행 실패 ({type(exc).__name__}): 입력·설정·연결 상태를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
