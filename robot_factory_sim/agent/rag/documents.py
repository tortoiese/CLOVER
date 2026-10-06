"""문서·로그 입력과 출처를 보존하는 청크 분할."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Scalar = str | int | float | bool
SUPPORTED_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".jsonl"})


@dataclass(frozen=True)
class Document:
    """입력 자료 한 단위. 로그는 한 행, PDF는 한 페이지다."""

    text: str
    source: str
    title: str
    metadata: dict[str, Scalar] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    """검색에 사용하는 문서 조각과 원문 위치."""

    id: str
    text: str
    metadata: dict[str, Scalar]


def chunk_document(document: Document, size: int = 800, overlap: int = 120) -> list[Chunk]:
    """문서를 문자 단위로 분할하고 위치와 출처를 보존한다.

    Args:
        document: 원문 자료.
        size: 최대 문자 수.
        overlap: 인접 조각의 중복 문자 수.

    Returns:
        빈 원문은 빈 목록, 그 외 청크 목록.
    """
    if size <= 0 or not 0 <= overlap < size:
        raise ValueError("청크 크기는 양수, 겹침은 0 이상 청크 크기 미만이어야 합니다.")
    chunks = []
    for offset in range(0, len(document.text), size - overlap):
        text = document.text[offset:offset + size].strip()
        if text:
            metadata = {**document.metadata, "source": document.source, "title": document.title, "offset": offset}
            identity = json.dumps(metadata, sort_keys=True, ensure_ascii=False) + "\n" + text
            chunks.append(Chunk(hashlib.sha256(identity.encode()).hexdigest(), text, metadata))
        if offset + size >= len(document.text):
            break
    return chunks


def _log_document(row: dict[str, Any], source: str, line: int) -> Document:
    metadata = {key: value for key, value in row.items()
                if key in {"run_id", "robot_id", "event_type", "timestamp", "synthetic", "scenario",
                           "dataset_id", "event_id", "origin", "db_source", "station_id", "time_basis"}
                and isinstance(value, (str, int, float, bool))}
    metadata["line"] = line
    text = json.dumps(row, ensure_ascii=False, sort_keys=True)
    if isinstance(row.get("text"), str) and row["text"].strip():
        identifiers = {key: row[key] for key in ("event_type", "robot_id", "station_id", "order_id")
                       if row.get(key) is not None}
        text = row["text"] + "\n" + json.dumps(identifiers, ensure_ascii=False)
    return Document(text, source,
                    str(row.get("event_type", "운영 로그")), metadata)


def load_documents(path: Path) -> list[Document]:
    """파일 또는 폴더의 지원 자료를 읽는다. PDF OCR은 지원하지 않는다.

    Args:
        path: Markdown, TXT, 텍스트 PDF, JSONL 또는 자료 폴더.

    Returns:
        출처와 페이지/행 정보를 가진 자료 목록.
    """
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"자료 경로가 없습니다: {path}")
    if path.is_dir():
        documents = []
        for file in sorted(path.rglob("*")):
            if file.is_file() and file.suffix.lower() in SUPPORTED_SUFFIXES and not file.is_symlink():
                documents.extend(load_documents(file))
        return documents
    source = str(path)
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        return [Document(path.read_text(encoding="utf-8"), source, path.stem)]
    if suffix == ".pdf":
        from pypdf import PdfReader

        documents = [Document(page.extract_text() or "", source, path.stem, {"page": number})
                     for number, page in enumerate(PdfReader(path).pages, 1)]
        if not any(document.text.strip() for document in documents):
            raise ValueError(f"텍스트가 없는 PDF입니다. OCR 후 입력하세요: {path.name}")
        return documents
    if suffix == ".jsonl":
        documents = []
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL {path.name} {number}행 형식 오류") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL {path.name} {number}행은 객체여야 합니다.")
            documents.append(_log_document(row, source, number))
        return documents
    raise ValueError(f"지원하지 않는 파일: {path.name}")
