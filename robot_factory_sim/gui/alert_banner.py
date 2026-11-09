"""고정된 상태 영역에서 표시하는 긴급 생산 알림 배너."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from gui import constants as c
from gui.widgets.elided_label import ElidedLabel
from utils.event_types import Severity


class AlertBanner(QFrame):
    """하단 알림 배너."""

    clicked = Signal()
    active_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """배너를 생성한다 (처음엔 접힌 상태).

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("AlertBanner")
        self.setProperty("severity", Severity.CRITICAL)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._pending = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 8, 6)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self._title = ElidedLabel("", self)
        self._title.setObjectName("BannerTitle")
        self._text = ElidedLabel("", self)
        self._text.setObjectName("BannerText")
        texts.addWidget(self._title)
        texts.addWidget(self._text)
        layout.addLayout(texts, 1)
        view = QPushButton("대응 보기", self)
        view.setObjectName("BannerView")
        view.clicked.connect(self._open_report)
        layout.addWidget(view)
        close = QPushButton("✕", self)
        close.setObjectName("BannerClose")
        close.setAccessibleName("긴급 알림 닫기")
        close.setToolTip("알림 닫기")
        close.clicked.connect(self.hide_banner)
        layout.addWidget(close)

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.setInterval(c.BANNER_AUTO_HIDE_MS)
        self._auto_hide.timeout.connect(self.hide_banner)

    def show_alert(self, title: str, message: str, severity: str = Severity.CRITICAL) -> None:
        """배너를 띄운다. 이미 떠 있으면 내용만 교체한다.

        Args:
            title: 제목
            message: 요약 문장
            severity: critical(빨강) / warning(주황)
        """
        self._pending += 1
        suffix = f"  (+{self._pending - 1}건)" if self._pending > 1 else ""
        self._title.setText(title + suffix)
        self._text.setText(message)
        self.setProperty("severity", severity)
        self.style().unpolish(self)
        self.style().polish(self)
        self.active_changed.emit(True)
        self._auto_hide.start()

    def hide_banner(self) -> None:
        """배너를 접는다."""
        self._pending = 0
        self._auto_hide.stop()
        self.active_changed.emit(False)

    def _open_report(self) -> None:
        self.clicked.emit()
        self.hide_banner()
