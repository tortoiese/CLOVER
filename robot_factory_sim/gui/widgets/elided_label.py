"""길이가 변해도 작업 영역의 폭을 바꾸지 않는 상태 라벨."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """좁은 화면에서는 문구를 줄이고 툴팁으로 전체 내용을 보여준다."""

    def __init__(self, text: str, parent: QWidget) -> None:
        """텍스트와 부모 위젯을 설정한다."""
        super().__init__(parent)
        self._full_text = ""
        self._tooltip: str | None = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 — Qt API
        """전체 문구를 저장하고 현재 폭에 맞춰 표시한다."""
        self.set_content(text)

    def set_content(self, text: str, tooltip: str | None = None) -> None:
        """표시 문구와 선택적인 상세 툴팁을 갱신한다."""
        if (text, tooltip) == (self._full_text, self._tooltip):
            return
        self._full_text = text
        self._tooltip = tooltip
        self._update_display()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        """사용자가 폭을 변경하면 표시 길이를 다시 계산한다."""
        super().resizeEvent(event)
        self._update_display()

    def _update_display(self) -> None:
        width = max(0, self.contentsRect().width() - 2 * self.margin())
        shown = self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, width)
        if shown != self.text():
            super().setText(shown)
        detail = self._tooltip
        if detail is None:
            detail = self._full_text if shown != self._full_text else ""
        if detail != self.toolTip():
            self.setToolTip(detail)
