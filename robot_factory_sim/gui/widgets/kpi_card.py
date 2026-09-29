"""KpiCard — 현재 값 + 비교 엔진 대비 증감률 카드."""

from __future__ import annotations

import math

from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from utils.metrics import KpiDefinition

FLAT_THRESHOLD: float = 0.5  # 이 비율(%) 미만 변화는 보합


class KpiCard(QFrame):
    """KPI 카드 1개."""

    def __init__(self, definition: KpiDefinition, parent: QWidget | None = None) -> None:
        """카드를 생성한다.

        Args:
            definition: KPI 정의
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.definition = definition
        self.setObjectName("KpiCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)

        self._title = QLabel(definition.label, self)
        self._title.setObjectName("KpiTitle")
        value_row = QHBoxLayout()
        value_row.setSpacing(4)
        self._value = QLabel("—", self)
        self._value.setObjectName("KpiValue")
        self._unit = QLabel(definition.unit, self)
        self._unit.setObjectName("KpiUnit")
        value_row.addWidget(self._value)
        value_row.addWidget(self._unit)
        value_row.addStretch(1)
        self._delta = QLabel("—", self)
        self._delta.setObjectName("KpiDelta")
        self._delta.setProperty("trend", "flat")

        layout.addWidget(self._title)
        layout.addLayout(value_row)
        layout.addWidget(self._delta)

    def set_values(self, value: float, reference: float, reference_label: str) -> None:
        """값과 비교 대상 대비 증감률을 표시한다.

        Args:
            value: 현재(표시 엔진) 값
            reference: 비교 엔진 값
            reference_label: 비교 대상 이름 (예: "Baseline")
        """
        d = self.definition
        self._value.setText("—" if math.isnan(value) else f"{value:.{d.decimals}f}")
        trend = "flat"
        if math.isnan(value) or math.isnan(reference) or abs(reference) < 1e-9:
            text = f"vs {reference_label} —"
        else:
            change = (value - reference) / abs(reference) * 100.0
            arrow = "▲" if change > 0 else "▼" if change < 0 else "■"
            if abs(change) >= FLAT_THRESHOLD:
                trend = "better" if (change > 0) == d.higher_is_better else "worse"
            text = f"{arrow} {abs(change):.1f}% vs {reference_label}"
        self._delta.setText(text)
        self._delta.setToolTip(f"{reference_label}: " + ("—" if math.isnan(reference) else f"{reference:.{d.decimals}f}"
                                                         f"{d.unit}"))
        if self._delta.property("trend") != trend:
            self._delta.setProperty("trend", trend)
            self._delta.style().unpolish(self._delta)
            self._delta.style().polish(self._delta)
