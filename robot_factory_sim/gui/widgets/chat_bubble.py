"""ChatBubble — 관제 콘솔 말풍선."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from utils.event_types import Severity

ROLE_LABELS: dict[str, str] = {"user": "운영자", "agent": "AI 관제", "system": "시스템"}
MAX_BUBBLE_RATIO: float = 0.86


class ChatBubble(QFrame):
    """말풍선 1개. role: user | agent | system."""

    def __init__(self, text: str, role: str, severity: str = Severity.INFO, parent: QWidget | None = None) -> None:
        """말풍선을 생성한다.

        Args:
            text: 본문
            role: user / agent / system
            severity: info / warning / critical (agent·system)
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("Bubble")
        self.setProperty("role", role)
        self.setProperty("severity", severity)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(2)

        meta = QLabel(f"{ROLE_LABELS.get(role, role)} · {datetime.now():%H:%M:%S}", self)
        meta.setObjectName("BubbleMeta")
        body = QLabel(text, self)
        body.setObjectName("BubbleText")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(meta)
        layout.addWidget(body)


class BubbleRow(QWidget):
    """말풍선 정렬용 행 (사용자: 오른쪽, 에이전트: 왼쪽, 시스템: 가운데)."""

    def __init__(self, bubble: ChatBubble, role: str, parent: QWidget | None = None) -> None:
        """행을 생성한다.

        Args:
            bubble: 말풍선
            role: 역할
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.bubble = bubble
        bubble.setParent(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            layout.addStretch(1)
            layout.addWidget(bubble, 6)
        elif role == "system":
            layout.addStretch(1)
            layout.addWidget(bubble, 8)
            layout.addStretch(1)
        else:
            layout.addWidget(bubble, 6)
            layout.addStretch(1)
