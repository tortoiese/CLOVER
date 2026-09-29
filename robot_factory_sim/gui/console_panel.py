"""AI 관제 콘솔 (Interactive Control Console).

상단: 타이틀 + 알림 뱃지 / 중앙: 말풍선 채팅 / 하단: 입력창 + 빠른 명령.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.widgets.chat_bubble import BubbleRow, ChatBubble
from utils.event_types import Severity

WELCOME_TEXT = ("안녕하세요, AI 관제 에이전트입니다.\n자연어로 시뮬레이션을 제어할 수 있습니다. '도움말'을 입력하면 명령 목록을 보여드립니다.\n"
                "긴급 주문·납기 임박·설비 고장 등은 자동으로 감지해 대응합니다.")


class ConsolePanel(QWidget):
    """관제 콘솔 패널."""

    message_submitted = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._unread = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(c.CONSOLE_TITLE, self)
        title.setObjectName("PanelTitle")
        self._badge = QLabel("", self)
        self._badge.setObjectName("Badge")
        self._badge.setVisible(False)
        self._agent_label = QLabel("", self)
        self._agent_label.setObjectName("Muted")
        header.addWidget(title)
        header.addWidget(self._badge)
        header.addStretch(1)
        header.addWidget(self._agent_label)
        layout.addLayout(header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chat = QWidget(self._scroll)
        self._chat.setObjectName("ChatArea")
        self._chat_layout = QVBoxLayout(self._chat)
        self._chat_layout.setContentsMargins(2, 2, 6, 2)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch(1)
        self._scroll.setWidget(self._chat)
        layout.addWidget(self._scroll, 1)

        quick = QHBoxLayout()
        quick.setSpacing(4)
        for label, command in c.QUICK_COMMANDS:
            button = QPushButton(label, self)
            button.setObjectName("QuickCommand")
            button.setToolTip(command)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, text=command: self._submit(text))
            quick.addWidget(button)
        quick.addStretch(1)
        layout.addLayout(quick)

        input_row = QHBoxLayout()
        self._input = QLineEdit(self)
        self._input.setPlaceholderText("명령을 입력하세요 (예: 로봇 3대 추가해줘, R-02 충전해, 배속 5배로)")
        self._send = QPushButton("전송", self)
        self._send.setObjectName("Primary")
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send)
        layout.addLayout(input_row)

        self._input.returnPressed.connect(lambda: self._submit(self._input.text()))
        self._send.clicked.connect(lambda: self._submit(self._input.text()))
        self.add_message(WELCOME_TEXT, "agent")

    def set_agent_name(self, name: str) -> None:
        """헤더에 에이전트 종류를 표시한다.

        Args:
            name: 에이전트 이름
        """
        self._agent_label.setText(f"agent: {name}")

    def _submit(self, text: str) -> None:
        """입력 전송."""
        text = text.strip()
        if not text:
            return
        self._input.clear()
        self.clear_badge()
        self.add_message(text, "user")
        self.message_submitted.emit(text)

    def add_message(self, text: str, role: str, severity: str = Severity.INFO) -> None:
        """말풍선을 추가하고 맨 아래로 스크롤한다.

        Args:
            text: 본문
            role: user / agent / system
            severity: 심각도
        """
        row = BubbleRow(ChatBubble(text, role, severity), role, self._chat)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, row)
        while self._chat_layout.count() - 1 > c.CHAT_MAX_MESSAGES:
            old = self._chat_layout.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        if role != "user" and severity in (Severity.WARNING, Severity.CRITICAL) and not self._input.hasFocus():
            self._unread += 1
            self._badge.setText(str(self._unread))
            self._badge.setVisible(True)
        QTimer.singleShot(0, self._scroll_to_bottom)

    def add_system_message(self, text: str, severity: str = Severity.INFO) -> None:
        """시스템 알림을 추가한다.

        Args:
            text: 본문
            severity: 심각도
        """
        self.add_message(text, "system", severity)

    def _scroll_to_bottom(self) -> None:
        """스크롤을 맨 아래로."""
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def clear_badge(self) -> None:
        """알림 뱃지를 지운다."""
        self._unread = 0
        self._badge.setVisible(False)

    def focus_console(self) -> None:
        """입력창에 포커스를 주고 최신 메시지로 스크롤한다."""
        self.clear_badge()
        self._input.setFocus()
        self._scroll_to_bottom()
