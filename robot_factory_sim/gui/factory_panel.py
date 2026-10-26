"""공장 시각화 패널 — 정보 칩 + FloorStage(3D 건물 ↔ 층별 도면) + 범례."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from gui import constants as c
from gui.floor_stage import FloorStage
from simulation.state import SimState
from utils.event_types import OrderStatus, SchedulerModeLabel


class FactoryPanel(QWidget):
    """공장 시각화 패널."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """패널을 생성한다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._title = QLabel("🏭 공장 시각화", self)
        self._title.setObjectName("PanelTitle")
        layout.addWidget(self._title)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._sim_time = self._chip("Sim T+00:00:00")
        self._wall = self._chip("경과 00:00")
        self._orders = self._chip("활성 주문 0")
        self._mode = self._chip("모드 -")
        self._pending = self._chip("대기 작업 0")
        for chip in (self._sim_time, self._wall, self._orders, self._pending, self._mode):
            chips.addWidget(chip)
        chips.addStretch(1)
        layout.addLayout(chips)

        self.stage = FloorStage(self)
        layout.addWidget(self.stage, 1)

        legend = QLabel(self._legend_text(), self)
        legend.setObjectName("Muted")
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        layout.addWidget(legend)

    def _chip(self, text: str) -> QLabel:
        """정보 칩 라벨."""
        label = QLabel(text, self)
        label.setObjectName("InfoChip")
        return label

    @staticmethod
    def _legend_text() -> str:
        """로봇 상태 색상 범례."""
        items = [("이동", "moving"), ("작업", "working"), ("충전", "charging"), ("대기", "idle"), ("고장", "fault")]
        parts = [f"<span style='color:{c.ROBOT_STATE_COLORS[key]}'>●</span> {name}" for name, key in items]
        parts.append(f"<span style='color:{c.EMERGENCY_COLOR}'>●</span> 긴급배정(깜빡임)")
        parts.append(f"<span style='color:{c.ELEVATOR}'>▮</span> 엘리베이터")
        return "&nbsp;&nbsp;".join(parts)

    def update_state(self, state: SimState) -> None:
        """스냅샷을 반영한다.

        Args:
            state: 시뮬레이션 상태
        """
        floors = state.layout.floor_count
        self._title.setText(f"🏭 공장 시각화 — {state.layout.profile.factory_name}"
                            + (f" ({floors}층)" if floors > 1 else ""))
        seconds = int(state.sim_time)
        self._sim_time.setText(f"Sim T+{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
        wall = int(state.wall_elapsed)
        self._wall.setText(f"경과 {wall // 60:02d}:{wall % 60:02d} · {state.speed}x")
        active = sum(1 for o in state.orders if o.status in OrderStatus.ACTIVE)
        self._orders.setText(f"활성 주문 {active}")
        self._pending.setText(f"대기 작업 {state.pending_tasks}")
        self._mode.setText(f"모드 {SchedulerModeLabel.LABELS.get(state.mode, state.mode)}")
        if state.mode in state.planning:
            planning = state.planning[state.mode]
            report = planning.get("report") or {}
            scope = "교통·캐빈 포함" if planning["scope"] == "traffic" else "작업장·충전기"
            self._mode.setText(f"{SchedulerModeLabel.LABELS[state.mode]} · {scope} · {planning['execution_status']}")
            self._mode.setToolTip(
                ("작업장·충전기·통로·교차점·단일 캐빈 계획\n" if planning["scope"] == "traffic" else
                 "작업장·충전기 계획 / 교통 자원은 실행 시 제어\n") +
                f"1단계 {report.get('phase1_status', '대기')} · 2단계 {report.get('phase2_status', '대기')}\n"
                f"계산 {report.get('seconds', 0):.3f}초 · 모델 {report.get('model_hash', '')[:12]}\n"
                f"채택 {planning['accepted']} · 대체 {planning['fallbacks']} · 지연 {planning['overruns']}")
        self.stage.update_state(state)
