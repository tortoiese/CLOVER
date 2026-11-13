"""AI 관제 콘솔 (Interactive Control Console).

상단: 타이틀 + 알림 뱃지 / 중앙: 말풍선 채팅 / 하단: 입력창 + 빠른 명령.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.widgets.chat_bubble import BubbleRow, ChatBubble
from utils.event_types import Severity

WELCOME_TEXT = ("현재 상태를 조회하거나 시뮬레이션 명령을 입력할 수 있습니다.\n"
                "처음이라면 ‘빠른 명령’에서 예시와 도움말을 확인하세요.")
BASIC_PLACEHOLDER = "예: 현재 상태 알려줘"
SEARCH_PLACEHOLDER = "예: /검색 ROI 산식"
URGENT_ORDER_LABEL = "긴급 주문"
COMPARISON_SHORTCUTS = (
    ("비교 결과 보기", "/비교"),
    ("추천 이유 보기", "/추천"),
    ("운영안 비교 도움말", "/최적화 도움말"),
)


class ConsolePanel(QWidget):
    """관제 콘솔 패널."""

    message_submitted = Signal(str)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        search_enabled: bool = False,
        comparison_enabled: bool = False,
    ) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
            search_enabled: 문서 검색 명령 표시 여부
            comparison_enabled: 운영안 비교 명령 표시 여부
        """
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._unread = 0
        self._follow_latest = True
        self._search_enabled = search_enabled
        self._comparison_enabled = comparison_enabled
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
        self._chat_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self._chat_layout.setContentsMargins(2, 2, 6, 2)
        self._chat_layout.setSpacing(8)
        self._chat_layout.addStretch(1)
        self._scroll.setWidget(self._chat)
        self._scrollbar = self._scroll.verticalScrollBar()
        layout.addWidget(self._scroll, 1)

        quick = QHBoxLayout()
        quick.setSpacing(4)
        quick_commands = QToolButton(self)
        quick_commands.setObjectName("QuickCommand")
        quick_commands.setText("빠른 명령")
        quick_commands.setCursor(Qt.CursorShape.PointingHandCursor)
        quick_commands.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(quick_commands)
        menu.setToolTipsVisible(True)
        for label, command in c.QUICK_COMMANDS:
            if label == URGENT_ORDER_LABEL:
                self._add_quick_action(menu, f"{label} 예시", command, draft=True)
            else:
                self._add_quick_action(menu, label, command)
        if search_enabled or comparison_enabled:
            menu.addSeparator()
        if search_enabled:
            self._add_quick_action(menu, "문서 검색", "/검색 ", draft=True)
        if comparison_enabled:
            for label, command in COMPARISON_SHORTCUTS:
                self._add_quick_action(menu, label, command)
        menu.addSeparator()
        self._add_quick_action(menu, "명령 도움말", "도움말")
        quick_commands.setMenu(menu)
        quick.addWidget(quick_commands)
        quick.addStretch(1)
        self._latest = QPushButton("최신 보기", self)
        self._latest.setObjectName("LatestMessages")
        self._latest.setToolTip("대화의 최신 메시지로 이동합니다.")
        self._latest.setEnabled(False)
        self._latest.clicked.connect(self._scroll_to_bottom)
        quick.addWidget(self._latest)
        layout.addLayout(quick)

        input_row = QHBoxLayout()
        self._input = QLineEdit(self)
        self._input.setAccessibleName("관제 메시지")
        self._input.setPlaceholderText(SEARCH_PLACEHOLDER if search_enabled else BASIC_PLACEHOLDER)
        self._send = QPushButton("전송", self)
        self._send.setObjectName("Primary")
        self._send.setEnabled(False)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._send)
        layout.addLayout(input_row)

        self._input.textChanged.connect(lambda text: self._send.setEnabled(bool(text.strip())))
        self._input.returnPressed.connect(self._submit_input)
        self._send.clicked.connect(self._submit_input)
        self._scrollbar.valueChanged.connect(self._on_scroll_value_changed)
        self._scrollbar.rangeChanged.connect(self._on_scroll_range_changed)
        self.add_message(WELCOME_TEXT, "agent")

    def set_agent_name(self, name: str) -> None:
        """헤더에 사용 가능한 관제 기능과 연결 정보를 표시한다.

        Args:
            name: 에이전트 이름
        """
        if self._comparison_enabled:
            self._agent_label.setText("운영안 비교")
        elif self._search_enabled:
            self._agent_label.setText("문서 검색")
        else:
            self._agent_label.setText("기본 관제")
        self._agent_label.setToolTip(name)

    def _add_quick_action(self, menu: QMenu, label: str, command: str, *, draft: bool = False) -> None:
        action = menu.addAction(label)
        action.setToolTip(command)
        if draft:
            action.triggered.connect(lambda: self._fill_draft(command))
        else:
            action.triggered.connect(lambda: self._submit(command, clear_input=False))

    def _fill_draft(self, command: str) -> None:
        """명령 예시를 입력창에 채우고 이전 입력은 실행 취소로 복원할 수 있게 한다."""
        self._input.selectAll()
        self._input.insert(command)
        self._input.setFocus()

    def _submit_input(self) -> None:
        self._submit(self._input.text())

    def _submit(self, text: str, *, clear_input: bool = True) -> None:
        text = text.strip()
        if not text:
            return
        if clear_input:
            self._input.clear()
        self._scroll_to_bottom()
        self.add_message(text, "user")
        self.message_submitted.emit(text)
        self._input.setFocus()

    def add_message(self, text: str, role: str, severity: str = Severity.INFO) -> None:
        """말풍선을 추가하고 최신 메시지를 보는 경우에만 계속 따라간다.

        Args:
            text: 본문
            role: user / agent / system
            severity: 심각도
        """
        row = BubbleRow(ChatBubble(text, role, severity, self._chat), role, self._chat)
        self._chat_layout.insertWidget(self._chat_layout.count() - 1, row)
        while self._chat_layout.count() - 1 > c.CHAT_MAX_MESSAGES:
            old = self._chat_layout.takeAt(0).widget()
            if old is not None:
                old.deleteLater()
        if role != "user" and severity in (Severity.WARNING, Severity.CRITICAL) and not self._input.hasFocus():
            self._unread += 1
            self._badge.setText(str(self._unread))
            self._badge.setVisible(True)

    def add_system_message(self, text: str, severity: str = Severity.INFO) -> None:
        """시스템 알림을 추가한다.

        Args:
            text: 본문
            severity: 심각도
        """
        self.add_message(text, "system", severity)

    def _scroll_to_bottom(self) -> None:
        self._follow_latest = True
        self._scrollbar.setValue(self._scrollbar.maximum())
        self._latest.setEnabled(False)
        self.clear_badge()

    def _on_scroll_value_changed(self, value: int) -> None:
        self._follow_latest = value == self._scrollbar.maximum()
        self._latest.setEnabled(not self._follow_latest)

    def _on_scroll_range_changed(self, _minimum: int, maximum: int) -> None:
        if self._follow_latest:
            self._scrollbar.setValue(maximum)
        self._latest.setEnabled(self._scrollbar.value() < maximum)

    def clear_badge(self) -> None:
        """알림 뱃지를 지운다."""
        self._unread = 0
        self._badge.setVisible(False)

    def focus_console(self) -> None:
        """입력창에 포커스를 주고 최신 메시지로 스크롤한다."""
        self._input.setFocus()
        self._scroll_to_bottom()
