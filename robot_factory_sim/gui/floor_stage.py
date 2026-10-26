"""FloorStage — 3D 건물 개요 ↔ 층별 도면 전환 무대.

처음에는 3D 건물이 무대 중앙을 크게 차지한다. 층을 클릭하면 건물이 왼쪽 위로 줄어들며 이동하고,
그 자리 아래와 오른쪽에 해당 층의 도면과 제어 헤더가 나타난다. 줄어든 건물에서 다른 층을 눌러도 도면이 바뀌고,
[전체 보기] 또는 Esc로 건물이 다시 중앙으로 커진다. 자식 위젯은 레이아웃 없이 직접 배치해서 애니메이션한다.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRect, QVariantAnimation, Qt
from PySide6.QtGui import QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui import constants as c
from gui.building_view import BuildingView
from gui.factory_view import FactoryView
from simulation.state import SimState
from utils.config_models import FactoryConfig

MINI_WIDTH_RATIO: float = 0.40
MINI_WIDTH_RANGE: tuple[int, int] = (190, 300)
MINI_ASPECT: float = 0.80
GAP: int = 8


def _lerp_rect(a: QRect, b: QRect, t: float) -> QRect:
    """두 사각형 사이를 보간한다."""
    return QRect(*(round(av + (bv - av) * t) for av, bv in
                   ((a.x(), b.x()), (a.y(), b.y()), (a.width(), b.width()), (a.height(), b.height()))))


class FloorHeader(QWidget):
    """도면 모드의 제어 헤더: 층 이름·층 버튼·축척·경로망·전체 보기."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """헤더를 만든다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setObjectName("FloorHeader")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 2, 4, 2)
        layout.setSpacing(5)

        top = QHBoxLayout()
        self.title = QLabel("", self)
        self.title.setObjectName("PanelTitle")
        top.addWidget(self.title, 1)
        self.back_button = QPushButton("↩ 전체 보기", self)
        self.back_button.setToolTip("3D 건물 전체 보기로 돌아갑니다 (Esc)")
        top.addWidget(self.back_button)
        layout.addLayout(top)

        self.stats = QLabel("", self)
        self.stats.setObjectName("Muted")
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)

        self.floor_row = QHBoxLayout()
        self.floor_row.setSpacing(4)
        self.floor_group = QButtonGroup(self)
        self.floor_group.setExclusive(True)
        self.floor_row.addStretch(1)
        layout.addLayout(self.floor_row)

        scale = QHBoxLayout()
        scale.setSpacing(4)
        self.zoom_out = QPushButton("－", self)
        self.zoom_out.setToolTip("축소 (Ctrl + 휠 아래로)")
        self.ratio_label = QLabel("축척 1 : -", self)
        self.ratio_label.setObjectName("InfoChip")
        self.ratio_label.setMinimumWidth(110)
        self.ratio_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in = QPushButton("＋", self)
        self.zoom_in.setToolTip("확대 (Ctrl + 휠 위로)")
        self.fit_button = QPushButton("맞춤", self)
        self.fit_button.setToolTip("도면 전체가 보이도록 맞춥니다 (도면 더블클릭)")
        for widget in (self.zoom_out, self.ratio_label, self.zoom_in, self.fit_button):
            scale.addWidget(widget)
        for button in (self.zoom_out, self.zoom_in):
            button.setFixedWidth(34)
        scale.addStretch(1)
        layout.addLayout(scale)

        self.cell_label = QLabel("", self)
        self.cell_label.setObjectName("Muted")
        layout.addWidget(self.cell_label)

        bottom = QHBoxLayout()
        self.network_toggle = QCheckBox("경로망", self)
        self.network_toggle.setChecked(True)
        bottom.addWidget(self.network_toggle)
        self.traffic_toggle = QCheckBox("통행·병목", self)
        self.traffic_toggle.setChecked(True)
        bottom.addWidget(self.traffic_toggle)
        hint = QLabel("Ctrl+휠: 축척 · 드래그: 이동", self)
        hint.setObjectName("Muted")
        bottom.addWidget(hint, 1)
        layout.addLayout(bottom)
        layout.addStretch(1)

    def rebuild_floor_buttons(self, count: int) -> None:
        """층 수에 맞게 층 버튼을 다시 만든다.

        Args:
            count: 층 수
        """
        for button in self.floor_group.buttons():
            self.floor_group.removeButton(button)
            self.floor_row.removeWidget(button)
            button.deleteLater()
        for floor in range(count, 0, -1):
            button = QPushButton(f"{floor}F", self)
            button.setCheckable(True)
            button.setFixedWidth(46)
            self.floor_group.addButton(button, floor)
            self.floor_row.insertWidget(self.floor_row.count() - 1, button)

    def set_floor(self, floor: int, title: str, stats: str) -> None:
        """현재 층 표시를 갱신한다.

        Args:
            floor: 층 번호
            title: 층 제목
            stats: 요약 문장
        """
        button = self.floor_group.button(floor)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self.title.setText(title)
        self.stats.setText(stats)

    def set_ratio(self, ratio: float, fit: bool, cell_text: str) -> None:
        """축척 표시를 갱신한다.

        Args:
            ratio: N (1 : N)
            fit: 맞춤 상태인지
            cell_text: 격자 한 칸 설명
        """
        self.ratio_label.setText(f"축척 1 : {round(ratio)}" + (" (맞춤)" if fit else ""))
        self.cell_label.setText(cell_text)


class FloorStage(QWidget):
    """3D 건물 + 층별 도면을 전환하는 무대."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """무대를 만든다.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.plan = FactoryView(self)
        self.header = FloorHeader(self)
        self.building = BuildingView(self)
        self.plan.hide()
        self.header.hide()

        self._layout: FactoryConfig | None = None
        self._floor_count = 0
        self._floor = 1
        self._detail = False
        self._progress = 0.0
        self._state: SimState | None = None

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(c.BUILDING_ANIMATION_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(lambda value: self._set_progress(float(value)))
        self._anim.finished.connect(self._on_anim_finished)

        self.building.floor_clicked.connect(self.select_floor)
        self.header.back_button.clicked.connect(self.show_overview)
        self.header.floor_group.idClicked.connect(self.select_floor)
        self.header.zoom_in.clicked.connect(lambda: self.plan.zoom_step(1))
        self.header.zoom_out.clicked.connect(lambda: self.plan.zoom_step(-1))
        self.header.fit_button.clicked.connect(self.plan.fit_to_view)
        self.header.network_toggle.toggled.connect(self.plan.set_network_visible)
        self.header.traffic_toggle.toggled.connect(self.plan.set_traffic_visible)
        self.plan.scale_changed.connect(self._on_scale_changed)
        self._relayout()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    @property
    def detail_mode(self) -> bool:
        """층 도면이 열려 있는지 (건물이 작아진 상태)."""
        return self._detail

    @property
    def current_floor(self) -> int:
        """선택된 층."""
        return self._floor

    def select_floor(self, floor: int) -> None:
        """층을 선택해 도면을 연다 (개요 상태면 건물이 줄어들며 이동한다).

        Args:
            floor: 층 번호
        """
        if self._floor_count:
            floor = max(1, min(self._floor_count, floor))
        self._floor = floor
        self.plan.set_floor(floor)
        self.building.set_selected(floor)
        self._refresh_header()
        if not self._detail:
            self._detail = True
            self._animate_to(1.0)

    def show_overview(self) -> None:
        """도면을 닫고 3D 건물 전체 보기로 돌아간다."""
        if self._detail:
            self._detail = False
            self.building.set_selected(None)
            self._animate_to(0.0)

    def update_state(self, state: SimState) -> None:
        """시뮬레이션 상태를 두 뷰에 전달한다.

        Args:
            state: 시뮬레이션 상태
        """
        self._state = state
        count = state.layout.floor_count
        if state.layout is not self._layout or count != self._floor_count:
            self._on_layout(state.layout, count)
        self.building.set_state(state)
        self.plan.update_state(state)
        if self._detail:
            self._refresh_header()

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------
    def _on_layout(self, layout: FactoryConfig, count: int) -> None:
        """구성(층 수)이 바뀌었을 때 헤더와 모드를 맞춘다."""
        previous = self._floor_count
        self._layout = layout
        self._floor_count = count
        if count != previous:
            self.header.rebuild_floor_buttons(count)
            self.header.back_button.setVisible(count > 1)
            if count == 1:
                self._floor = 1
                self._jump(detail=True)
            elif previous <= 1:
                self._jump(detail=False)
            elif self._floor > count:
                self._floor = count
        self.plan.set_floor(self._floor)
        self.building.set_selected(self._floor if self._detail else None)

    def _jump(self, detail: bool) -> None:
        """애니메이션 없이 개요/도면 모드로 바로 바꾼다."""
        self._anim.stop()
        self._detail = detail
        self.plan.setVisible(detail)
        self.header.setVisible(detail)
        self._set_progress(1.0 if detail else 0.0)

    def _refresh_header(self) -> None:
        """헤더의 제목/요약을 현재 층 기준으로 갱신한다."""
        layout = self._layout
        if layout is None:
            return
        floor = self._floor
        names = " · ".join(s.label for s in layout.stations if s.floor == floor)
        title = f"{floor}F" + (f" — {names}" if names else "")
        robots = sum(1 for r in (self._state.robots if self._state else []) if round(r.floor_pos) == floor)
        stations = sum(1 for s in layout.stations if s.floor == floor)
        chargers = sum(1 for ch in layout.chargers if ch.floor == floor)
        stats = f"로봇 {robots}대 · 스테이션 {stations} · 충전소 {chargers} · 엘리베이터 {len(layout.elevators)}"
        self.header.set_floor(floor, title, stats)

    def _on_scale_changed(self, ratio: float, fit: bool) -> None:
        """도면 축척 표시 갱신."""
        self.header.set_ratio(ratio, fit, self.plan.cell_size_text())

    def _animate_to(self, target: float) -> None:
        """진행도(0=전체 보기, 1=도면)를 target까지 애니메이션한다."""
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self.plan.show()
        self.header.show()
        self._anim.start()

    def _set_progress(self, progress: float) -> None:
        """진행도에 맞춰 건물 위치를 정한다. 도면과 헤더는 건물 아래에 깔려 있다가 건물이 줄어들면서 드러난다."""
        self._progress = progress
        self._relayout()

    def _on_anim_finished(self) -> None:
        """애니메이션이 끝나면 숨겨야 할 위젯을 숨긴다."""
        if self._progress <= 0.0:
            self.plan.hide()
            self.header.hide()
        self._relayout()

    def _mini_rect(self) -> QRect:
        """줄어든 건물의 위치 (왼쪽 위)."""
        low, high = MINI_WIDTH_RANGE
        width = int(max(low, min(high, self.width() * MINI_WIDTH_RATIO)))
        return QRect(0, 0, width, int(width * MINI_ASPECT))

    def _relayout(self) -> None:
        """진행도에 따라 자식 위젯을 배치한다."""
        full = self.rect()
        mini = self._mini_rect()
        self.building.setGeometry(_lerp_rect(full, mini, self._progress))
        self.header.setGeometry(QRect(mini.width() + GAP, 0, max(0, full.width() - mini.width() - GAP), mini.height()))
        top = mini.height() + GAP
        self.plan.setGeometry(QRect(0, top, full.width(), max(0, full.height() - top)))
        self.building.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        """크기가 바뀌면 다시 배치한다."""
        super().resizeEvent(event)
        self._relayout()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt override
        """Esc: 전체 보기."""
        if event.key() == Qt.Key.Key_Escape and self._detail and self._floor_count > 1:
            self.show_overview()
            return
        super().keyPressEvent(event)
