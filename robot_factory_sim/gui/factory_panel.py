"""공장 시각화 패널 — 정보 칩 + FloorStage(3D 건물 ↔ 층별 도면) + 범례."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QLabel, QVBoxLayout, QWidget

from gui import constants as c
from gui.floor_stage import FloorStage
from gui.widgets.elided_label import ElidedLabel
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

        self._title = ElidedLabel("공장 보기", self)
        self._title.setObjectName("PanelTitle")
        layout.addWidget(self._title)

        chips = QGridLayout()
        chips.setHorizontalSpacing(6)
        chips.setVerticalSpacing(4)
        self._sim_time = self._chip("가상 00:00:00")
        self._wall = self._chip("경과 00:00")
        self._orders = self._chip("활성 주문 0")
        self._mode = self._chip("모드 -")
        self._pending = self._chip("대기 작업 0")
        for column, chip in enumerate((self._sim_time, self._wall, self._mode)):
            chips.addWidget(chip, 0, column)
            chips.setColumnStretch(column, 1)
        chips.addWidget(self._orders, 1, 0)
        chips.addWidget(self._pending, 1, 1)
        layout.addLayout(chips)

        self.stage = FloorStage(self)
        layout.addWidget(self.stage, 1)

        legend = QLabel(self._legend_text(), self)
        legend.setObjectName("Muted")
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        layout.addWidget(legend)

    def _chip(self, text: str) -> ElidedLabel:
        """정보 칩 라벨."""
        label = ElidedLabel(text, self)
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
        self._title.setText(f"공장 보기 · {state.layout.profile.factory_name}"
                            + (f" ({floors}층)" if floors > 1 else ""))
        seconds = int(state.sim_time)
        self._sim_time.setText(f"가상 {seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}")
        wall = int(state.wall_elapsed)
        self._wall.setText(f"경과 {wall // 60:02d}:{wall % 60:02d} · {state.speed}x")
        active = sum(1 for o in state.orders if o.status in OrderStatus.ACTIVE)
        self._orders.setText(f"활성 주문 {active}")
        self._pending.setText(f"대기 작업 {state.pending_tasks}")
        self._mode.setText(f"모드 {SchedulerModeLabel.LABELS.get(state.mode, state.mode)}")
        self.stage.update_state(state)
