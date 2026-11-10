"""로컬 RAG 연동 검증용 HTTP 서버. ``python -m agent.rag.mock_server --help``."""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import math
import os
import struct
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from agent.rag.providers import EVIDENCE_SEPARATOR, HashEmbedder

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 2_000_000
MOCK_ANSWER_PREFIX = "[목 서버 응답]"


class MockRagServer(ThreadingHTTPServer):
    """임베딩과 근거 응답에 사용하는 요청·응답 형식을 재현한다."""

    def __init__(self, port: int, api_key: str, response_delay: float = 0.0) -> None:
        """루프백 주소에서만 실행하고 생성 응답 지연을 설정한다."""
        if not api_key or not math.isfinite(response_delay) or response_delay < 0:
            raise ValueError("목 서버 키와 0 이상의 응답 지연이 필요합니다.")
        super().__init__((LOOPBACK, port), _RagRequest)
        self.api_key = api_key
        self.response_delay = response_delay
        self.embedder = HashEmbedder()

    @property
    def base_url(self) -> str:
        """GUI 공급자 설정에 넣을 주소를 반환한다."""
        return f"http://{LOOPBACK}:{self.server_port}/v1"


class _RagRequest(BaseHTTPRequestHandler):
    """목 서버의 두 POST 요청을 처리한다."""

    server: MockRagServer

    def do_POST(self) -> None:  # noqa: N802 — HTTP handler API
        """인증 및 입력 검증 후 해당 경로의 응답을 보낸다."""
        if self.path not in {"/v1/embeddings", "/v1/responses"}:
            self._error(HTTPStatus.NOT_FOUND, "unsupported_endpoint", "지원 경로는 embeddings와 responses입니다.")
            return
        expected = f"Bearer {self.server.api_key}".encode()
        if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
            self._error(HTTPStatus.UNAUTHORIZED, "invalid_api_key", "목 서버 인증 키가 일치하지 않습니다.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError("요청 본문 크기가 허용 범위를 벗어났습니다.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict) or not isinstance(body.get("model"), str) or not body["model"].strip():
                raise ValueError("model이 포함된 JSON 객체가 필요합니다.")
            if self.path == "/v1/embeddings":
                result = self._embeddings(body)
            else:
                result = self._response(body)
                time.sleep(self.server.response_delay)
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        self._write(HTTPStatus.OK, result)

    def _embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        encoding = body.get("encoding_format", "float")
        if not isinstance(inputs, list) or not inputs or not all(isinstance(text, str) for text in inputs):
            raise ValueError("input에는 문자열 또는 비어 있지 않은 문자열 배열을 넣어야 합니다.")
        if encoding not in ("float", "base64"):
            raise ValueError("encoding_format은 float 또는 base64입니다.")
        data = []
        for index, vector in enumerate(self.server.embedder.embed(inputs)):
            embedding = (base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")
                         if encoding == "base64" else vector)
            data.append({"object": "embedding", "index": index, "embedding": embedding})
        return {"object": "list", "model": body["model"], "data": data,
                "usage": {"prompt_tokens": 0, "total_tokens": 0}}

    @staticmethod
    def _response(body: dict[str, Any]) -> dict[str, Any]:
        prompt = body.get("input")
        if not isinstance(prompt, str) or EVIDENCE_SEPARATOR not in prompt:
            raise ValueError("검색 근거가 포함된 요청이 필요합니다.")
        evidence = json.loads(prompt.rpartition(EVIDENCE_SEPARATOR)[2])
        first = evidence[0] if isinstance(evidence, list) and evidence else None
        if not isinstance(first, dict) or first.get("citation") != 1 or not isinstance(first.get("text"), str):
            raise ValueError("첫 번째 검색 근거와 인용 번호가 필요합니다.")
        excerpt = " ".join(first["text"].split())[:160]
        # 예시 응답에 원문 속 인용 표기가 섞이지 않도록 괄호를 바꾼다.
        excerpt = excerpt.replace("[", "(").replace("]", ")")
        content = {"type": "output_text", "text": f"{MOCK_ANSWER_PREFIX} {excerpt} [1]", "annotations": []}
        message = {"type": "message", "id": "msg_clover_mock", "role": "assistant", "status": "completed",
                   "content": [content]}
        return {"object": "response", "id": "resp_clover_mock", "created_at": 0, "model": body["model"],
                "status": "completed", "output": [message], "parallel_tool_calls": False,
                "tool_choice": "none", "tools": []}

    def _error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._write(status, {"error": {"type": "invalid_request_error", "code": code, "message": message}})

    def _write(self, status: HTTPStatus, result: dict[str, Any]) -> None:
        data = json.dumps(result, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError:
            # 타임아웃 시 클라이언트가 먼저 연결을 닫을 수 있다.
            return


def main() -> None:
    """명령줄에서 목 서버를 시작한다."""
    parser = argparse.ArgumentParser(
        description="OpenAI 호환 요청을 받는 CLOVER 검증용 목 서버입니다.",
        epilog="CLOVER_MOCK_API_KEY로 검증용 키를 지정하고, GUI의 OPENAI_BASE_URL에 출력된 주소를 설정하세요.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="루프백 포트")
    parser.add_argument("--response-delay", type=float, default=0, help="타임아웃 검증용 생성 응답 지연(초)")
    args = parser.parse_args()
    try:
        server = MockRagServer(args.port, os.environ.get("CLOVER_MOCK_API_KEY", ""), args.response_delay)
    except (OSError, ValueError, OverflowError) as exc:
        parser.error(f"CLOVER_MOCK_API_KEY와 서버 설정을 확인하세요. {exc}")
    with server:
        print(f"목 서버 주소: {server.base_url}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
