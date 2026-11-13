"""장기 실험의 기간 설정, 진행률, 결과 보고서를 표시한다."""

from __future__ import annotations

import math
from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import config
from gui import constants as c
from gui.dashboard_panel import make_plot
from simulation.period_experiment import MAX_EXPERIMENT_DAYS, PeriodResult, run_period
from utils.config_models import FactoryConfig, OrderRecord
from utils.event_types import SchedulerModeLabel

REPORT_METRICS: tuple[tuple[str, str, int], ...] = (
    ("completed_units", "완료 작업 (건)", 0),
    ("throughput", "기간 평균 처리량 (건/분)", 2),
    ("avg_wait", "평균 작업 대기 (초)", 1),
    ("charge_wait", "평균 충전 대기 (초)", 1),
    ("distance_m", "로봇 이동거리 (m)", 1),
    ("unfinished_orders", "미완료 주문 (건)", 0),
    ("background_arrivals", "일반 작업 요청 (건)", 0),
    ("background_rejected", "대기 한도 초과로 미수용 (건)", 0),
)


class PeriodWorker(QThread):
    """DB와 독립된 기간 실험을 백그라운드에서 실행한다."""

    progress_changed = Signal(int)
    result_ready = Signal(object)
    status_changed = Signal(str)

    def __init__(self, factory: FactoryConfig, orders: list[OrderRecord], epoch: datetime,
                 task_rate: float, days: int, parent: QWidget) -> None:
        """실행 시점의 설정과 주문을 보관한다."""
        super().__init__(parent)
        self._inputs = (factory, orders, epoch, task_rate, days)

    def run(self) -> None:
        """실험을 실행하고 GUI에 진행률과 결과를 전달한다."""
        try:
            result = run_period(*self._inputs, progress=self.progress_changed.emit,
                                cancelled=self.isInterruptionRequested)
        except InterruptedError:
            self.status_changed.emit("실험을 취소했습니다.")
        except Exception as exc:  # noqa: BLE001 — 백그라운드 오류를 결과 화면에 표시
            self.status_changed.emit(f"실험 실행 중 오류가 발생했습니다. {exc}")
        else:
            self.result_ready.emit(result)


class ExperimentPanel(QWidget):
    """지정 기간 실험을 시작하고 결과를 확인하는 탭."""

    run_requested = Signal(int)
    cancel_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        """기간 설정과 결과 표·그래프를 구성한다."""
        super().__init__(parent)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        label = QLabel("가상 운영 기간", self)
        self._days = QSpinBox(self)
        self._days.setRange(1, MAX_EXPERIMENT_DAYS)
        self._days.setSuffix("일")
        self._days.setAccessibleName(label.text())
        label.setBuddy(self._days)
        self._run = QPushButton("기간 실험 시작", self)
        self._run.setObjectName("Primary")
        self._run.clicked.connect(lambda: self.run_requested.emit(self._days.value()))
        self._cancel = QPushButton("취소", self)
        self._cancel.setEnabled(False)
        self._cancel.clicked.connect(self.cancel_requested.emit)
        for widget in (label, self._days, self._run, self._cancel):
            controls.addWidget(widget)
        controls.addStretch()
        layout.addLayout(controls)

        self._progress = QProgressBar(self)
        self._progress.setObjectName("PeriodProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)
        self._status = QLabel("현재 공장 구성과 남은 주문, 합성 일반 작업을 사용해 라운드로빈·가중합을 비교합니다.", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        self._table = QTableWidget(len(REPORT_METRICS), 3, self)
        self._table.setHorizontalHeaderLabels(("기간 전체 지표", "라운드로빈", "가중합"))
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().hide()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, (_, title, _) in enumerate(REPORT_METRICS):
            self._table.setItem(row, 0, QTableWidgetItem(title))
        layout.addWidget(self._table, 1)
        self._plot = make_plot(self, "가상 운영 기간 (일)", "구간 처리량 (건/분)")
        self._plot.addLegend(offset=(10, 10))
        self._series = {
            mode: self._plot.plot([], [], pen=pg.mkPen(c.ENGINE_COLORS[mode], width=2),
                                 name=SchedulerModeLabel.LABELS[mode])
            for mode in config.SCHEDULER_MODES
        }
        self._plot.setXRange(0, 1, padding=0)
        self._plot.setYRange(0, 5, padding=0)
        layout.addWidget(self._plot, 2)

    def start_progress(self) -> None:
        """입력을 잠그고 새 실험의 진행률을 초기화한다."""
        self._days.setEnabled(False)
        self._run.setEnabled(False)
        self._cancel.setEnabled(True)
        self._progress.setValue(0)
        for column in range(1, self._table.columnCount()):
            for row in range(self._table.rowCount()):
                self._table.takeItem(row, column)
        for series in self._series.values():
            series.setData([], [])
        self._plot.setXRange(0, self._days.value(), padding=0)
        self._plot.setYRange(0, 5, padding=0)
        self.set_status(f"{self._days.value()}일 실험을 실행하고 있습니다. 완료 후 결과를 갱신합니다.")

    def set_progress(self, value: int) -> None:
        """백그라운드에서 전달된 계산 진행률을 표시한다."""
        self._progress.setValue(value)

    def set_status(self, message: str) -> None:
        """진행 또는 종료 상태를 표시한다."""
        self._status.setText(message)

    def finish_progress(self) -> None:
        """다음 실험을 시작할 수 있도록 입력을 복구한다."""
        self._days.setEnabled(True)
        self._run.setEnabled(True)
        self._cancel.setEnabled(False)

    def show_result(self, result: PeriodResult) -> None:
        """완료된 실험의 기간 요약과 그래프를 한 번에 반영한다."""
        self.set_status(f"{result.days}일 실험 완료 · 입력 주문 {result.remaining_orders}건 · "
                        f"일반 작업 발생률 {result.task_rate:g}건/초 · "
                        f"공통 시드 {config.RANDOM_SEED} · 계산 {result.elapsed_seconds:.1f}초")
        for column, mode in enumerate(config.SCHEDULER_MODES, 1):
            for row, (key, _, decimals) in enumerate(REPORT_METRICS):
                value = result.metrics[mode][key]
                text = f"{value:,.{decimals}f}" if math.isfinite(value) else "—"
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self._table.setItem(row, column, item)
            self._series[mode].setData(result.times, result.throughput[mode])
        self._plot.setXRange(0, result.days, padding=0)
        peak = max(max(series) for series in result.throughput.values())
        self._plot.setYRange(0, max(5.0, math.ceil(peak / 5) * 5), padding=0)
