"""Validated live RH control settings; edited values cross threads as plain data."""

from __future__ import annotations

from dataclasses import asdict

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QSpinBox,
    QWidget,
)

from simulation.planning.contracts import Settings


class PlanningSettingsDialog(QDialog):
    """Edit numerical planning limits without changing the current physical factory."""

    def __init__(self, values: dict, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("RH 계획 설정")
        self.resize(420, 420)
        self._original = values
        layout = QFormLayout(self)
        note = QLabel("설정 변경은 계산 중인 해를 폐기합니다. 이미 확정한 물리 행동은 안전 경계까지 유지합니다.", self)
        note.setWordWrap(True)
        layout.addRow(note)
        self.fields = {}
        for key, title, low, high in (("horizon", "계획 구간 (sim-sec)", .5, 300),
                                     ("interval", "재계획 간격 (sim-sec)", .5, 300),
                                     ("timeout", "계산 예산 (wall-sec)", .1, 20),
                                     ("sigma", "처리시간 여유 (표준편차 배수)", 0, 5)):
            spin = QDoubleSpinBox(self)
            spin.setRange(low, high)
            spin.setDecimals(2)
            spin.setValue(values[key])
            layout.addRow(title, spin)
            self.fields[key] = spin
        for key, title, low, high in (("max_tasks", "후보 작업 수", 1, 32),
                                     ("max_arcs", "행동 arc 상한", 100, 200000),
                                     ("memory_limit_mib", "솔버 RSS 상한 (MiB)", 16, 4096)):
            spin = QSpinBox(self)
            spin.setRange(low, high)
            spin.setValue(values[key])
            layout.addRow(title, spin)
            self.fields[key] = spin
        self.step = QComboBox(self)
        for value in (.5, 1, 2):
            self.step.addItem(str(value), value)
        self.step.setCurrentIndex(self.step.findData(values["step"]))
        layout.addRow("시간 격자 (초)", self.step)
        self.scope = QComboBox(self)
        self.scope.addItem("작업장·충전기·교통·캐빈", "traffic")
        self.scope.addItem("작업장·충전기 (교통은 실행 시 제어)", "station_charge")
        self.scope.setCurrentIndex(self.scope.findData(values["scope"]))
        layout.addRow("계획 범위", self.scope)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def values(self) -> dict:
        """Return a complete validated immutable-settings payload."""
        values = {**self._original, **{k: widget.value() for k, widget in self.fields.items()},
                  "step": self.step.currentData(), "scope": self.scope.currentData()}
        values["watchdog"] = max(3, values["timeout"] + 1)
        settings = Settings(**values)
        settings.validate()
        return asdict(settings)

    def _accept(self) -> None:
        try:
            self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "계획 설정 확인", str(exc))
            return
        self.accept()
