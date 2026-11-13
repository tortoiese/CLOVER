"""KpiCard — 현재 값 + 비교 엔진 대비 증감률 카드."""

from __future__ import annotations

import math

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gui.widgets.elided_label import ElidedLabel
from utils.metrics import KpiDefinition

FLAT_THRESHOLD: float = 0.5  # 이 비율(%) 미만 변화는 보합


class KpiCard(QFrame):
    """KPI 카드 1개."""

    def __init__(self, definition: KpiDefinition, parent: QWidget | None = None, *, featured: bool = False) -> None:
        """카드를 생성한다.

        Args:
            definition: KPI 정의
            parent: 부모 위젯
            featured: 큰 수치와 비교 대상 값을 함께 보여줄지 여부
        """
        super().__init__(parent)
        self.definition = definition
        self.setObjectName("KpiCard")
        self.setProperty("featured", featured)
        self._display_unit = "초" if definition.unit == "s" else definition.unit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(0)

        self._title = QLabel(definition.label, self)
        self._title.setObjectName("KpiTitle")
        value_row = QHBoxLayout()
        value_row.setSpacing(4)
        self._value = ElidedLabel("—", self)
        self._value.setObjectName("KpiValue")
        self._unit = QLabel(self._display_unit, self)
        self._unit.setObjectName("KpiUnit")
        value_row.addWidget(self._value, 1)
        value_row.addWidget(self._unit)
        self._delta = ElidedLabel("—", self)
        self._delta.setObjectName("KpiDelta")
        self._delta.setProperty("trend", "flat")

        layout.addWidget(self._title)
        layout.addLayout(value_row)
        self._reference = ElidedLabel("비교 값 —", self) if featured else None
        if self._reference is not None:
            self._reference.setObjectName("KpiReference")
            layout.addWidget(self._reference)
        layout.addWidget(self._delta)

    def set_values(self, value: float, reference: float, reference_label: str) -> None:
        """값과 비교 대상 대비 증감률을 표시한다.

        Args:
            value: 현재(표시 엔진) 값
            reference: 비교 엔진 값
            reference_label: 비교 대상 이름 (예: "Baseline")
        """
        d = self.definition
        value_text = "—" if math.isnan(value) else f"{value:.{d.decimals}f}"
        self._value.set_content(value_text, f"{value_text} {self._display_unit}")
        trend = "flat"
        if math.isnan(value) or math.isnan(reference) or abs(reference) < 1e-9:
            text = "—"
        else:
            change = (value - reference) / abs(reference) * 100.0
            arrow = "▲" if change > 0 else "▼" if change < 0 else "■"
            if abs(change) >= FLAT_THRESHOLD:
                trend = "better" if (change > 0) == d.higher_is_better else "worse"
            text = f"{arrow} {abs(change):.1f}%"
        reference_text = "—" if math.isnan(reference) else f"{reference:.{d.decimals}f} {self._display_unit}"
        if self._reference is not None:
            self._reference.setText(f"{reference_label} {reference_text}")
        self._delta.set_content(text, f"{text} · {reference_label}: {reference_text}")
        if self._delta.property("trend") != trend:
            self._delta.setProperty("trend", trend)
            self._delta.style().unpolish(self._delta)
            self._delta.style().polish(self._delta)
