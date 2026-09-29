"""긴급 생산 알림 배너 — 긴급/경고 이벤트 시 화면 하단에서 슬라이드업, 클릭 시 관제 콘솔 포커스."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from gui import constants as c
from utils.event_types import Severity


class AlertBanner(QFrame):
    """하단 알림 배너."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        """배너를 생성한다 (처음엔 접힌 상태).

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("AlertBanner")
        self.setProperty("severity", Severity.CRITICAL)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMaximumHeight(0)
        self._pending = 0

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 8, 6)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self._title = QLabel("", self)
        self._title.setObjectName("BannerTitle")
        self._text = QLabel("", self)
        self._text.setObjectName("BannerText")
        texts.addWidget(self._title)
        texts.addWidget(self._text)
        layout.addLayout(texts, 1)
        hint = QLabel("클릭 → 관제 콘솔에서 AI 대응 보고 보기", self)
        hint.setObjectName("BannerText")
        layout.addWidget(hint)
        close = QPushButton("✕", self)
        close.setObjectName("BannerClose")
        close.clicked.connect(self.hide_banner)
        layout.addWidget(close)

        self._animation = QPropertyAnimation(self, b"maximumHeight", self)
        self._animation.setDuration(c.BANNER_ANIMATION_MS)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
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
        self._animate_to(c.BANNER_HEIGHT)
        self._auto_hide.start()

    def hide_banner(self) -> None:
        """배너를 접는다."""
        self._pending = 0
        self._auto_hide.stop()
        self._animate_to(0)

    def _animate_to(self, height: int) -> None:
        """높이 애니메이션."""
        self._animation.stop()
        self._animation.setStartValue(self.maximumHeight())
        self._animation.setEndValue(height)
        self._animation.start()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """클릭 시 콘솔 포커스 요청 후 접는다."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            self.hide_banner()
        super().mousePressEvent(event)
