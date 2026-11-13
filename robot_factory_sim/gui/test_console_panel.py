"""빠른 명령의 전송 방식과 관제 콘솔의 스크롤 동작을 검증한다."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QScrollArea, QToolButton

from gui.console_panel import ConsolePanel


def _actions(panel: ConsolePanel) -> dict[str, QAction]:
    menu = panel.findChild(QToolButton, "QuickCommand").menu()
    return {action.text(): action for action in menu.actions() if not action.isSeparator()}


def test_quick_commands_keep_drafts_and_match_available_features() -> None:
    """조회 명령은 즉시 전송하고 검색·긴급 주문 예시는 입력창에만 채운다."""
    app = QApplication.instance() or QApplication([])
    basic = ConsolePanel()
    search_only = ConsolePanel(search_enabled=True)
    console = ConsolePanel(search_enabled=True, comparison_enabled=True)
    submitted: list[str] = []
    console.message_submitted.connect(submitted.append)
    input_box = console.findChild(QLineEdit)
    send = console.findChild(QPushButton, "Primary")
    actions = _actions(console)

    assert "문서 검색" not in _actions(basic)
    assert "문서 검색" in _actions(search_only)
    assert "추천 이유 보기" not in _actions(search_only)
    assert "추천 이유 보기" in actions
    input_box.setText("  ")
    assert not send.isEnabled()

    input_box.setText("작성 중인 명령")
    actions["현재 상태"].trigger()
    assert submitted == ["현재 상태 알려줘"]
    assert input_box.text() == "작성 중인 명령"

    actions["문서 검색"].trigger()
    assert input_box.text() == "/검색 "
    input_box.undo()
    assert input_box.text() == "작성 중인 명령"
    actions["긴급 주문 예시"].trigger()
    draft = input_box.text()
    assert draft.startswith("긴급 주문 넣어줘")
    assert submitted == ["현재 상태 알려줘"]
    assert send.isEnabled()
    send.click()
    assert submitted == ["현재 상태 알려줘", draft]
    assert not send.isEnabled()

    console.close()
    search_only.close()
    basic.close()
    app.processEvents()


def test_incoming_messages_preserve_history_scroll_and_plain_text() -> None:
    """긴 응답이 도착해도 이전 내용을 읽던 위치와 콘솔 폭을 유지한다."""
    app = QApplication.instance() or QApplication([])
    console = ConsolePanel()
    console.resize(300, 330)
    console.show()
    chat = console.findChild(QScrollArea)
    scroll = chat.verticalScrollBar()
    latest = console.findChild(QPushButton, "LatestMessages")
    for index in range(20):
        console.add_message(f"기존 관제 기록 {index}", "agent")
    QTest.qWait(20)
    assert scroll.maximum() > 0
    assert scroll.value() == scroll.maximum()
    width = console.width()

    scroll.setValue(scroll.maximum() // 2)
    history_position = scroll.value()
    content = "<b>문서 근거</b> " + "a" * 500
    console.add_message(content, "agent")
    QTest.qWait(20)
    assert scroll.value() == history_position
    assert console.width() == width
    assert chat.widget().width() == chat.viewport().width()
    body = console.findChildren(QLabel, "BubbleText")[-1]
    assert body.text() == content
    assert body.textFormat() == Qt.TextFormat.PlainText
    assert body.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse

    assert latest.isEnabled()
    latest.click()
    assert scroll.value() == scroll.maximum()
    console.add_message("새 관제 기록", "agent")
    QTest.qWait(20)
    assert scroll.value() == scroll.maximum()
    assert not latest.isEnabled()
    console.close()
    app.processEvents()
