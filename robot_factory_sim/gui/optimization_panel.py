"""실험 조건과 계산 결과를 표시하는 GUI. 저장·최적화는 명시적 요청으로만 수행한다."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from agent.orchestration import COMPARISON_METRICS, format_value
from utils.comparison_models import ComparisonConditions
from utils.event_types import SchedulerModeLabel

FIELDS = (
    ("duration", "평가 기간 (sim-sec)", 30, 600, 0),
    ("repeats", "반복 횟수 (쌍대 실험)", 2, 10, 0),
    ("seed", "시작 난수 시드", 0, 1000000, 0),
    ("horizon", "MILP 계획 구간 (sim-sec)", 1, 300, 1),
    ("replan", "재계획 주기 (sim-sec)", 1, 300, 1),
    ("revenue_per_task", "공정 작업 1건 매출 (원/건)", -1, 10000000, 2),
    ("electricity_per_kwh", "전력 단가 (원/kWh)", -1, 100000, 3),
    ("wh_per_battery_unit", "상대 배터리 1단위당 Wh", -1, 100000, 5),
    ("charge_efficiency", "충전 효율 (0 초과~1)", 0.001, 1, 3),
    ("movement_cost_per_m", "전력 제외 이동 운영비 (원/m)", 0, 100000, 3),
    ("fixed_cost", "평가 기간 고정비 (원)", 0, 100000000, 2),
    ("ai_cost", "선택 정책 추가 운영비 (원/평가)", 0, 10000000, 3),
)


class OptimizationPanel(QDialog):
    """독립 비교 실험의 설정·결과·근거를 보여주는 비모달 창."""

    run_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        """조건 양식과 비교표를 생성한다."""
        super().__init__(parent)
        self.setWindowTitle("운영안 비교 · 로컬 계산")
        self.resize(960, 760)
        self._result: dict[str, Any] | None = None
        layout = QVBoxLayout(self)
        self._status = QLabel("현재 GUI 구성·남은 주문을 복사해 기본 배치·만충전으로 재시작하는 독립 실험입니다.", self)
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs)
        form_widget = QWidget(self._tabs)
        form = QFormLayout(form_widget)
        self._scenario = QComboBox(form_widget)
        self._scenario.addItem("현재 GUI 공장·남은 주문", "current")
        self._scenario.addItem("교대 물류 배차 — 공동 배정 검증", "alternating_dispatch")
        self._scenario.addItem("단일 충전 슬롯 — 병목 검증", "single_charge_slot")
        self._scenario.addItem("기본 공장 — 일반 부하 대조", "reference_factory")
        form.addRow("독립 비교 시나리오", self._scenario)
        self._candidate = QComboBox(form_widget)
        for policy in ("assignment_milp_legacy", "rh_milp", "rh_cpsat", "weighted_heuristic"):
            alias = "optimized" if policy == "weighted_heuristic" else policy
            self._candidate.addItem(SchedulerModeLabel.LABELS[alias], policy)
        form.addRow("FIFO와 비교할 정책", self._candidate)
        self._all_policies = QCheckBox("다섯 정책 모두 평가 (계산 시간이 늘어납니다)", form_widget)
        form.addRow(self._all_policies)
        self._fields: dict[str, QDoubleSpinBox | QSpinBox] = {}
        for key, label, low, high, decimals in FIELDS:
            spin = QSpinBox(form_widget) if key in {"repeats", "seed"} else QDoubleSpinBox(form_widget)
            if isinstance(spin, QDoubleSpinBox):
                spin.setDecimals(decimals)
            spin.setRange(low, high)
            if low < 0:
                spin.setSpecialValueText("미입력")
            form.addRow(label, spin)
            self._fields[key] = spin
        self._basis = QComboBox(form_widget)
        self._basis.addItems(["사용자 입력 · 실측 미검증", "예시값 · 가상 비용 모델"])
        form.addRow("비용 근거", self._basis)
        self._robot_profiles = QLineEdit("{}", form_widget)
        self._robot_profiles.setPlaceholderText('{"R0": [Wh/단위, 충전효율]}')
        self._robot_profiles.setToolTip("선택 입력. 예: {\"R0\": [2.0, 0.9]}. 나머지는 공통 환산값을 사용합니다.")
        form.addRow("로봇별 전력 환산 JSON", self._robot_profiles)
        example = QPushButton("발표용 예시 단가 입력 (가상값)", form_widget)
        example.clicked.connect(self._fill_example)
        form.addRow(example)
        note = QLabel("ROI는 운영비 기준입니다. 완제품 매출·투자 ROI와 구분하세요.\n"
                      "Wh 환산값이나 매출이 미설정이면 비용·ROI 추천을 보류합니다.\n"
                      "로봇·충전소 대수와 위치는 공장 설정에서 변경합니다.", form_widget)
        note.setWordWrap(True)
        form.addRow(note)
        scroll = QScrollArea(self._tabs)
        scroll.setWidgetResizable(True)
        scroll.setWidget(form_widget)
        self._tabs.addTab(scroll, "비교 조건")
        result_widget = QWidget(self._tabs)
        result_layout = QVBoxLayout(result_widget)
        self._table = QTableWidget(len(COMPARISON_METRICS), 4, result_widget)
        self._table.setHorizontalHeaderLabels(["지표 (반복 평균)", "FIFO", "선택 정책", "차이 (선택−FIFO)"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        result_layout.addWidget(self._table, 2)
        self._details = QTextBrowser(result_widget)
        result_layout.addWidget(self._details, 1)
        self._tabs.addTab(result_widget, "결과·검증 범위")
        self._evidence = QTextBrowser(self._tabs)
        self._tabs.addTab(self._evidence, "운영 근거")
        buttons = QHBoxLayout()
        self._run = QPushButton("현재 GUI 구성으로 비교 실행", self)
        self._run.clicked.connect(self.run_requested)
        buttons.addWidget(self._run)
        self._save = QPushButton("실험 결과 JSON 저장", self)
        self._save.setEnabled(False)
        self._save.clicked.connect(self._save_result)
        buttons.addWidget(self._save)
        layout.addLayout(buttons)
        self.set_conditions(asdict(ComparisonConditions()))
        self._scenario.currentIndexChanged.connect(self._select_scenario)

    def _select_scenario(self) -> None:
        if self._scenario.currentData() == "current":
            return
        from simulation.scenario_suite import scenarios
        case = next(s for s in scenarios() if s.id == self._scenario.currentData())
        self._fields["duration"].setValue(case.duration)
        self._fields["horizon"].setValue(case.settings.horizon)
        self._fields["replan"].setValue(case.settings.interval)
        self._candidate.setCurrentIndex(self._candidate.findData("rh_milp"))
        self._status.setText("내장 공장과 고정 수요를 복사한 독립 비교입니다. 현재 공장 DB·주문은 변경하지 않습니다.")

    def scenario_id(self) -> str:
        """Return the explicitly selected independent workload identifier."""
        return self._scenario.currentData()

    def conditions(self) -> dict[str, Any]:
        """현재 양식의 검증된 조건을 반환한다."""
        values = {key: None if spin.value() < 0 else spin.value() for key, spin in self._fields.items()}
        values["cost_basis"] = self._basis.currentText()
        try:
            values["robot_energy_profiles"] = json.loads(self._robot_profiles.text())
        except (ValueError, TypeError) as exc:
            raise ValueError("로봇별 전력 환산 JSON을 확인하세요.") from exc
        return asdict(ComparisonConditions.from_dict(values))

    def policies(self) -> tuple[str, ...]:
        """Select measured policies independently of the live display selection."""
        if self._all_policies.isChecked():
            return ("baseline_fifo", "weighted_heuristic", "assignment_milp_legacy", "rh_milp", "rh_cpsat")
        return ("baseline_fifo", self._candidate.currentData())

    def candidate_policy(self) -> str:
        """Choose the focused report when measuring multiple policies."""
        return self._candidate.currentData()

    def set_conditions(self, values: dict[str, Any]) -> None:
        """채팅에서 변경한 조건을 GUI 양식에 반영한다."""
        for key, spin in self._fields.items():
            spin.setValue(-1 if values[key] is None else values[key])
        self._basis.setCurrentText(values["cost_basis"])
        profiles = values.get("robot_energy_profiles", ())
        mapping = profiles if isinstance(profiles, dict) else {row[0]: list(row[1:]) for row in profiles}
        self._robot_profiles.setText(json.dumps(mapping, ensure_ascii=False))

    def _fill_example(self) -> None:
        values = self.conditions()
        values.update(revenue_per_task=1000, electricity_per_kwh=150, wh_per_battery_unit=1,
                      movement_cost_per_m=1, fixed_cost=100, ai_cost=0, cost_basis="예시값 · 가상 비용 모델")
        self.set_conditions(values)

    def set_busy(self, busy: bool) -> None:
        """동시에 여러 실험을 요청하지 않도록 실행 버튼을 잠근다."""
        self._run.setEnabled(not busy)
        self._tabs.widget(0).setEnabled(not busy)
        self._status.setText("계산 중입니다. GUI는 계속 사용할 수 있습니다. 실험 한도는 120초입니다."
                             if busy else "독립 실험 결과입니다. 설정·주문을 변경하면 다시 계산하세요.")

    def mark_stale(self) -> None:
        """기존 결과가 현재 설정을 반영하지 않을 수 있음을 알린다."""
        if self._result:
            self._status.setText("설정·주문이 변경되었습니다. 표시된 결과는 이전 실험이며 재계산이 필요합니다.")

    def show_result(self, result: dict[str, Any]) -> None:
        """계산 수치와 검증 범위를 표에 그대로 표시한다."""
        self._result = result
        for row, (key, label, unit) in enumerate(COMPARISON_METRICS):
            baseline, candidate = result["baseline"][key], result["candidate"][key]
            delta = candidate - baseline if baseline is not None and candidate is not None else None
            values = (label, format_value(baseline, unit), format_value(candidate, unit),
                      format_value(delta, "%p" if key == "roi" else unit))
            for column, text in enumerate(values):
                self._table.setItem(row, column, QTableWidgetItem(text))
        solver = result["solver"]
        gap = solver["max_dispatch_gap"]
        lines = [f"실험 ID: {result['experiment_id']}", f"수요: {result['workload']}",
                 f"비용 근거: {result['conditions']['cost_basis']}", result["recommendation"],
                 f"계획 계산: {solver['optimal_calls']}/{solver['calls']}회 모델 내 최적해 증명",
                 f"개별 배정 최대 격차: {format_value(gap * 100 if gap is not None else None, '%')}", solver["scope"]]
        for key, label in (("net_profit", "순이익 차이(원)"), ("roi", "ROI 차이(%p)")):
            interval = result["intervals"].get(key)
            if interval:
                lines.append(f"{label} 평균 95% 신뢰구간: {interval['low']:,.2f}~{interval['high']:,.2f}")
        self._details.setPlainText("\n".join(lines + result["notes"]))
        if "experiment" in result:
            base, proposed = result["baseline"], result["candidate"]
            extra = ["", "미완료·대기 꼬리·납기 (반복 평균)"]
            for key, title in (("unfinished", "미완료 작업"), ("wait_p95", "관측 대기 P95(초)"),
                               ("on_time_completed", "납기 내 완료"), ("late_completed", "지각 완료")):
                extra.append(f"{title}: {format_value(base.get(key))} → {format_value(proposed.get(key))}")
            for role, title in (("baseline", "FIFO"), ("candidate", "선택 정책")):
                for row in result["samples"].get(role, []):
                    extra.append(f"{title} seed {row['seed']} 자원 대기 (평균 / P95 / 표본 수)")
                    for channel, values in row.get("wait_distributions", {}).items():
                        extra.append(f"  {channel}: {format_value(values['mean'])} / "
                                     f"{format_value(values['p95'])} / {values['sample_count']}")
            self._details.append("\n".join(extra))
        self._save.setEnabled(True)
        self._tabs.setCurrentIndex(1)
        self.show()

    def show_evidence(self, text: str) -> None:
        """RAG가 반환한 출처와 문서 내용을 표시한다."""
        self._evidence.setPlainText(text)

    def _save_result(self) -> None:
        if self._result is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "실험 결과 저장", "comparison.json", "JSON (*.json)")
        if filename:
            try:
                Path(filename).write_text(json.dumps(self._result, ensure_ascii=False, indent=2, allow_nan=False),
                                          encoding="utf-8")
            except OSError as exc:
                QMessageBox.warning(self, "저장 실패", str(exc))
