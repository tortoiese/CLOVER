"""공장 설정 에디터 (Factory Configurator).

- 팔레트 → 공장 바닥 드래그 앤 드롭 배치, 아이템 드래그 이동 (격자 스냅)
- 속성 패널: 선택한 설비의 좌표/크기/처리시간/충전 슬롯 등 편집
- 경로 편집 모드: 빈 곳 클릭 = 노드 추가, 노드 두 개 연속 클릭 = 엣지 연결, 우클릭 = 삭제
- 로봇 관리 테이블, 프리셋 저장/불러오기
- [저장] → FactoryService로 DB 반영 → ConfigWatcher가 감지 → 시뮬레이터 핫리로드
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QMimeData, QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QKeyEvent, QMouseEvent, \
    QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import config
from gui import constants as c
from gui.factory_view import draw_grid
from gui.widgets.obstacle_item import ObstacleItem
from gui.widgets.station_item import ChargerItem, StationItem
from simulation.factory_service import FactoryService
from simulation.path_graph import edge_blocked
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    ObstacleConfig,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)
from utils.layout_tools import build_grid_network

COORD_MAX: float = 5000.0
DEFAULT_WALL_LENGTH: float = 100.0
DEFAULT_OBSTACLE_SIZE: float = 40.0
GRID_NETWORK_MIN_SPACING: float = 50.0
ROBOT_COLUMNS: tuple[str, ...] = ("ID", "종류", "배터리 용량", "속도 (m/s)", "소모율 (/m)", "충전 임계치 (%)", "가동")
MODE_SELECT: str = "select"
MODE_PATH: str = "path"

Snapper = Callable[[QPointF], QPointF]
MoveCallback = Callable[[QGraphicsItem], None]


def _make_editable(item: QGraphicsItem) -> None:
    """아이템을 선택·이동 가능하게 만든다."""
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
    item.setCursor(Qt.CursorShape.SizeAllCursor)


class _EditableBehavior:
    """이동 시 스냅 + 모델 갱신 콜백 (아이템 클래스에 혼합)."""

    snapper: Snapper | None = None
    on_moved: MoveCallback | None = None

    def handle_change(self, item: QGraphicsItem, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        """itemChange 공통 처리.

        Args:
            item: 대상 아이템
            change: 변경 종류
            value: 변경 값

        Returns:
            조정된 값
        """
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange and self.snapper is not None:
            return self.snapper(value)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged and self.on_moved is not None:
            self.on_moved(item)
        return value


class EditableStation(StationItem, _EditableBehavior):
    """편집용 스테이션."""

    def __init__(self, cfg: WorkStationConfig) -> None:
        """아이템 생성.

        Args:
            cfg: 스테이션 설정 (에디터 모델 객체, in-place 수정됨)
        """
        super().__init__(cfg)
        _make_editable(self)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:  # noqa: N802
        """이동 처리."""
        return super().itemChange(change, self.handle_change(self, change, value))


class EditableCharger(ChargerItem, _EditableBehavior):
    """편집용 충전소."""

    def __init__(self, cfg: ChargingStationConfig) -> None:
        """아이템 생성.

        Args:
            cfg: 충전소 설정
        """
        super().__init__(cfg)
        _make_editable(self)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:  # noqa: N802
        """이동 처리."""
        return super().itemChange(change, self.handle_change(self, change, value))


class EditableObstacle(ObstacleItem, _EditableBehavior):
    """편집용 벽/문/장애물 (이동 시 두 끝점을 함께 평행이동)."""

    def __init__(self, cfg: ObstacleConfig) -> None:
        """아이템 생성.

        Args:
            cfg: 장애물 설정
        """
        super().__init__(cfg)
        _make_editable(self)

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:  # noqa: N802
        """이동 처리."""
        return super().itemChange(change, self.handle_change(self, change, value))


class NodeItem(QGraphicsEllipseItem, _EditableBehavior):
    """경로 노드."""

    def __init__(self, node: PathNodeConfig) -> None:
        """아이템 생성.

        Args:
            node: 노드 설정
        """
        r = c.EDITOR_NODE_RADIUS
        super().__init__(-r, -r, 2 * r, 2 * r)
        self.node = node
        self.setPos(node.x, node.y)
        self.setZValue(12)
        _make_editable(self)
        self.set_highlight(False)

    def set_highlight(self, on: bool) -> None:
        """엣지 연결 대기 표시.

        Args:
            on: 강조 여부
        """
        color = QColor(c.EMERGENCY_COLOR if on else (c.SUCCESS if self.node.linked_station_id else c.HIGHLIGHT))
        self.setBrush(QBrush(color))
        self.setPen(QPen(QColor(c.TEXT), 1.5 if on else 0.8))
        self.setToolTip(f"노드 {self.node.id} ({self.node.x:.0f}, {self.node.y:.0f})"
                        + (f"\n연결 설비: {self.node.linked_station_id}" if self.node.linked_station_id else ""))

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:  # noqa: N802
        """이동 처리."""
        return super().itemChange(change, self.handle_change(self, change, value))


class EdgeItem(QGraphicsLineItem):
    """경로 엣지."""

    def __init__(self, edge: PathEdgeConfig) -> None:
        """아이템 생성.

        Args:
            edge: 엣지 설정
        """
        super().__init__()
        self.edge = edge
        self.setZValue(11)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)

    def set_geometry(self, a: QPointF, b: QPointF, blocked: bool) -> None:
        """위치와 차단 여부를 반영한다.

        Args:
            a: 시작 노드 좌표
            b: 끝 노드 좌표
            blocked: 장애물 차단 여부
        """
        self.setLine(a.x(), a.y(), b.x(), b.y())
        color = QColor(c.CRITICAL if blocked else c.HIGHLIGHT)
        color.setAlpha(200 if blocked else 150)
        self.setPen(QPen(color, 2.0, Qt.PenStyle.DashLine if blocked else Qt.PenStyle.SolidLine))
        self.setToolTip("장애물로 차단된 엣지" if blocked else f"엣지 {self.edge.from_node} ↔ {self.edge.to_node}")


class PaletteList(QListWidget):
    """드래그 가능한 설비 팔레트."""

    def __init__(self, parent: QWidget | None = None) -> None:
        """팔레트 생성.

        Args:
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        for kind, label in c.PALETTE_ITEMS:
            item = QListWidgetItem(label, self)
            item.setData(Qt.ItemDataRole.UserRole, kind)
            item.setToolTip("공장 바닥으로 끌어다 놓으세요")

    def mimeData(self, items: list[QListWidgetItem]) -> QMimeData:  # noqa: N802 — Qt override
        """드래그 데이터: 설비 종류."""
        data = QMimeData()
        if items:
            data.setData(c.PALETTE_MIME, str(items[0].data(Qt.ItemDataRole.UserRole)).encode("utf-8"))
        return data


class EditorView(QGraphicsView):
    """편집 캔버스 (드롭 수신 + 경로 편집 클릭 처리)."""

    item_dropped = Signal(str, QPointF)      # (kind, scene pos)
    empty_clicked = Signal(QPointF)          # 경로 모드에서 빈 곳 클릭
    node_clicked = Signal(object)            # 경로 모드에서 노드 클릭 (NodeItem)
    delete_requested = Signal(object)        # 우클릭 / Delete 키 (아이템 목록)

    def __init__(self, scene: QGraphicsScene, parent: QWidget | None = None) -> None:
        """뷰 생성.

        Args:
            scene: 편집 씬
            parent: 부모 위젯
        """
        super().__init__(scene, parent)
        self.setAcceptDrops(True)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setBackgroundBrush(QBrush(QColor(c.BG)))
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.mode = MODE_SELECT

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 — Qt override
        """팔레트 드래그만 허용."""
        if event.mimeData().hasFormat(c.PALETTE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 — Qt override
        """드래그 이동."""
        if event.mimeData().hasFormat(c.PALETTE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 — Qt override
        """드롭 → item_dropped."""
        if event.mimeData().hasFormat(c.PALETTE_MIME):
            kind = bytes(event.mimeData().data(c.PALETTE_MIME).data()).decode("utf-8")
            self.item_dropped.emit(kind, self.mapToScene(event.position().toPoint()))
            event.acceptProposedAction()
        else:
            super().dropEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt override
        """경로 모드 클릭 / 우클릭 삭제."""
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.MouseButton.RightButton:
            if item is not None and isinstance(item, (NodeItem, EdgeItem, EditableStation, EditableCharger,
                                                      EditableObstacle)):
                self.delete_requested.emit([item])
            return
        if self.mode == MODE_PATH and event.button() == Qt.MouseButton.LeftButton:
            if isinstance(item, NodeItem):
                self.node_clicked.emit(item)
                return
            if not isinstance(item, (EdgeItem, EditableStation, EditableCharger, EditableObstacle)):
                self.empty_clicked.emit(self.mapToScene(event.position().toPoint()))
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 — Qt override
        """Delete 키로 선택 항목 삭제."""
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            selected = self.scene().selectedItems()
            if selected:
                self.delete_requested.emit(selected)
                return
        super().keyPressEvent(event)


def _spin(maximum: float = COORD_MAX, decimals: int = 1, minimum: float = 0.0, step: float = 5.0) -> QDoubleSpinBox:
    """실수 입력 스핀박스."""
    spin = QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(step)
    spin.setKeyboardTracking(False)
    return spin


class ConfigEditorDialog(QDialog):
    """공장 설정 에디터."""

    saved = Signal(int)  # 변경된 레코드 수

    def __init__(self, service: FactoryService, parent: QWidget | None = None) -> None:
        """에디터를 생성하고 DB 구성을 불러온다.

        Args:
            service: 공장 서비스
            parent: 부모 위젯
        """
        super().__init__(parent)
        self.setWindowTitle("⚙ 공장 설정 에디터")
        self.resize(1320, 820)
        self._service = service
        self._factory = FactoryConfig()
        self._station_items: dict[str, EditableStation] = {}
        self._charger_items: dict[str, EditableCharger] = {}
        self._obstacle_items: list[EditableObstacle] = []
        self._node_items: dict[int, NodeItem] = {}
        self._edge_items: list[EdgeItem] = []
        self._pending_node: NodeItem | None = None
        self._selected: QGraphicsItem | None = None
        self._loading = False
        self._dirty = False

        self._scene = QGraphicsScene(self)
        self._build_ui()
        self._load_factory(self._service.load_factory_config())
        self._refresh_presets()

    # ------------------------------------------------------------------
    # UI 구성
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """위젯 배치."""
        layout = QVBoxLayout(self)

        toolbar = QHBoxLayout()
        self._mode_group = QButtonGroup(self)
        for mode, label, tip in ((MODE_SELECT, "🖱 선택/이동", "설비를 드래그해 이동, Delete/우클릭으로 삭제"),
                                 (MODE_PATH, "🧭 경로 편집", "빈 곳 클릭 = 노드 추가, 노드 두 개 클릭 = 엣지 연결")):
            button = QPushButton(label, self)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setProperty("mode", mode)
            self._mode_group.addButton(button)
            toolbar.addWidget(button)
        self._mode_group.buttons()[0].setChecked(True)
        self._mode_group.buttonClicked.connect(lambda b: self._set_mode(str(b.property("mode"))))
        self._snap = QCheckBox("격자 스냅", self)
        self._snap.setChecked(True)
        toolbar.addWidget(self._snap)
        grid_button = QPushButton("격자 경로망 자동 생성", self)
        grid_button.clicked.connect(self._generate_grid_network)
        toolbar.addWidget(grid_button)
        delete_button = QPushButton("선택 삭제", self)
        delete_button.clicked.connect(lambda: self._delete_items(self._scene.selectedItems()))
        toolbar.addWidget(delete_button)
        toolbar.addStretch(1)
        toolbar.addWidget(QLabel("프리셋", self))
        self._presets = QComboBox(self)
        self._presets.setMinimumWidth(160)
        toolbar.addWidget(self._presets)
        for label, slot in (("불러오기", self._load_preset), ("현재 구성 저장", self._save_preset),
                            ("삭제", self._delete_preset)):
            button = QPushButton(label, self)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        self._tabs = QTabWidget(self)
        layout.addWidget(self._tabs, 1)
        self._tabs.addTab(self._build_layout_tab(), "🏭 레이아웃")
        self._tabs.addTab(self._build_robot_tab(), "🤖 로봇 관리")

        bottom = QHBoxLayout()
        self._status = QLabel("", self)
        self._status.setObjectName("Muted")
        bottom.addWidget(self._status, 1)
        revert = QPushButton("DB에서 다시 불러오기", self)
        revert.clicked.connect(self._revert)
        save = QPushButton("💾 저장 (DB 반영 → 핫리로드)", self)
        save.setObjectName("Primary")
        save.clicked.connect(self._save)
        close = QPushButton("닫기", self)
        close.clicked.connect(self.close)
        bottom.addWidget(revert)
        bottom.addWidget(save)
        bottom.addWidget(close)
        layout.addLayout(bottom)

    def _build_layout_tab(self) -> QWidget:
        """레이아웃 탭: 팔레트 | 캔버스 | 속성."""
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        palette_box = QWidget(splitter)
        palette_layout = QVBoxLayout(palette_box)
        palette_layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("팔레트", palette_box)
        title.setObjectName("PanelTitle")
        palette_layout.addWidget(title)
        palette_layout.addWidget(PaletteList(palette_box), 1)
        hint = QLabel("• 드래그 앤 드롭으로 배치\n• 경로 편집 모드: 클릭으로\n  노드/엣지 추가\n• 우클릭: 삭제\n"
                      "• 빨간 점선: 벽에 막힌 엣지", palette_box)
        hint.setObjectName("Muted")
        palette_layout.addWidget(hint)

        self._view = EditorView(self._scene, splitter)
        self._view.item_dropped.connect(self._on_drop)
        self._view.empty_clicked.connect(self._on_empty_click)
        self._view.node_clicked.connect(self._on_node_click)
        self._view.delete_requested.connect(self._delete_items)
        self._scene.selectionChanged.connect(self._on_selection_changed)

        splitter.addWidget(self._build_properties(splitter))
        splitter.setSizes([180, 820, 320])
        return splitter

    def _build_properties(self, parent: QWidget) -> QWidget:
        """속성 패널 (공장 정보 + 선택 항목)."""
        box = QWidget(parent)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)

        profile_group = QGroupBox("공장 정보", box)
        profile_form = QFormLayout(profile_group)
        self._p_name = QLineEdit(profile_group)
        self._p_width = _spin(step=50)
        self._p_height = _spin(step=50)
        self._p_grid = _spin(maximum=500, step=5)
        self._p_grid.setMinimum(10)
        profile_form.addRow("공장 이름", self._p_name)
        profile_form.addRow("바닥 너비 (px)", self._p_width)
        profile_form.addRow("바닥 높이 (px)", self._p_height)
        profile_form.addRow("격자 크기 (px)", self._p_grid)
        self._p_name.editingFinished.connect(self._on_profile_edited)
        for spin in (self._p_width, self._p_height, self._p_grid):
            spin.valueChanged.connect(self._on_profile_edited)
        layout.addWidget(profile_group)

        self._props = QStackedWidget(box)
        empty = QLabel("설비를 선택하면 속성을 편집할 수 있습니다.", self._props)
        empty.setWordWrap(True)
        empty.setObjectName("Muted")
        self._props.addWidget(empty)
        self._props.addWidget(self._build_station_form())
        self._props.addWidget(self._build_charger_form())
        self._props.addWidget(self._build_obstacle_form())
        self._props.addWidget(self._build_node_form())
        group = QGroupBox("선택 항목 속성", box)
        group_layout = QVBoxLayout(group)
        group_layout.addWidget(self._props)
        layout.addWidget(group, 1)
        return box

    def _build_station_form(self) -> QWidget:
        """스테이션 속성 폼."""
        page = QWidget(self)
        form = QFormLayout(page)
        self._s_id = QLabel(page)
        self._s_label = QLineEdit(page)
        self._s_x, self._s_y = _spin(), _spin()
        self._s_w, self._s_h = _spin(maximum=400), _spin(maximum=400)
        self._s_type = QComboBox(page)
        self._s_type.addItems(list(config.TASK_TYPES))
        self._s_time = _spin(maximum=600, decimals=2, step=0.5)
        self._s_std = _spin(maximum=120, decimals=2, step=0.1)
        self._s_active = QCheckBox("가동 (해제 = 고장/비활성)", page)
        for label, widget in (("ID", self._s_id), ("라벨", self._s_label), ("X (중심)", self._s_x),
                              ("Y (중심)", self._s_y), ("너비", self._s_w), ("높이", self._s_h), ("공정", self._s_type),
                              ("평균 처리시간 (s)", self._s_time), ("처리시간 표준편차", self._s_std), ("", self._s_active)):
            form.addRow(label, widget)
        self._s_label.editingFinished.connect(self._apply_station_form)
        for spin in (self._s_x, self._s_y, self._s_w, self._s_h, self._s_time, self._s_std):
            spin.valueChanged.connect(self._apply_station_form)
        self._s_type.currentTextChanged.connect(self._apply_station_form)
        self._s_active.toggled.connect(self._apply_station_form)
        return page

    def _build_charger_form(self) -> QWidget:
        """충전소 속성 폼."""
        page = QWidget(self)
        form = QFormLayout(page)
        self._c_id = QLabel(page)
        self._c_x, self._c_y = _spin(), _spin()
        self._c_capacity = QSpinBox(page)
        self._c_capacity.setRange(1, 20)
        self._c_capacity.setKeyboardTracking(False)
        self._c_rate = _spin(maximum=50, decimals=2, step=0.5)
        self._c_rate.setMinimum(0.1)
        self._c_active = QCheckBox("가동", page)
        for label, widget in (("ID", self._c_id), ("X (중심)", self._c_x), ("Y (중심)", self._c_y),
                              ("충전 슬롯 수", self._c_capacity), ("충전 속도 (/s)", self._c_rate), ("", self._c_active)):
            form.addRow(label, widget)
        for spin in (self._c_x, self._c_y, self._c_rate):
            spin.valueChanged.connect(self._apply_charger_form)
        self._c_capacity.valueChanged.connect(self._apply_charger_form)
        self._c_active.toggled.connect(self._apply_charger_form)
        return page

    def _build_obstacle_form(self) -> QWidget:
        """장애물 속성 폼."""
        page = QWidget(self)
        form = QFormLayout(page)
        self._o_type = QComboBox(page)
        self._o_type.addItems(list(config.OBSTACLE_TYPES))
        self._o_x1, self._o_y1, self._o_x2, self._o_y2 = _spin(), _spin(), _spin(), _spin()
        self._o_passable = QCheckBox("통과 가능 (문)", page)
        self._o_label = QLineEdit(page)
        for label, widget in (("종류", self._o_type), ("X1", self._o_x1), ("Y1", self._o_y1), ("X2", self._o_x2),
                              ("Y2", self._o_y2), ("", self._o_passable), ("라벨", self._o_label)):
            form.addRow(label, widget)
        self._o_type.currentTextChanged.connect(self._apply_obstacle_form)
        for spin in (self._o_x1, self._o_y1, self._o_x2, self._o_y2):
            spin.valueChanged.connect(self._apply_obstacle_form)
        self._o_passable.toggled.connect(self._apply_obstacle_form)
        self._o_label.editingFinished.connect(self._apply_obstacle_form)
        return page

    def _build_node_form(self) -> QWidget:
        """노드 속성 폼."""
        page = QWidget(self)
        form = QFormLayout(page)
        self._n_id = QLabel(page)
        self._n_x, self._n_y = _spin(), _spin()
        self._n_link = QLabel(page)
        form.addRow("노드 ID", self._n_id)
        form.addRow("X", self._n_x)
        form.addRow("Y", self._n_y)
        form.addRow("연결 설비", self._n_link)
        self._n_x.valueChanged.connect(self._apply_node_form)
        self._n_y.valueChanged.connect(self._apply_node_form)
        return page

    def _build_robot_tab(self) -> QWidget:
        """로봇 관리 탭."""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        self._robot_table = QTableWidget(0, len(ROBOT_COLUMNS), page)
        self._robot_table.setHorizontalHeaderLabels(list(ROBOT_COLUMNS))
        self._robot_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._robot_table.verticalHeader().setVisible(False)
        self._robot_table.setAlternatingRowColors(True)
        self._robot_table.itemChanged.connect(lambda _: self._mark_dirty())
        layout.addWidget(self._robot_table, 1)
        buttons = QHBoxLayout()
        add = QPushButton("로봇 추가", page)
        add.clicked.connect(self._add_robot_row)
        remove = QPushButton("선택 로봇 삭제", page)
        remove.setObjectName("Danger")
        remove.clicked.connect(self._remove_robot_rows)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        note = QLabel("가동 체크 해제 시 시뮬레이션에서 퇴역합니다. 저장 즉시 로봇 프로세스가 생성/종료됩니다.", page)
        note.setObjectName("Muted")
        buttons.addWidget(note)
        layout.addLayout(buttons)
        return page

    # ------------------------------------------------------------------
    # 모델 ↔ 씬
    # ------------------------------------------------------------------
    def _load_factory(self, factory: FactoryConfig) -> None:
        """구성을 에디터에 불러온다 (복사본을 편집)."""
        self._factory = copy.deepcopy(factory)
        self._loading = True
        profile = self._factory.profile
        self._p_name.setText(profile.factory_name)
        for spin, value in ((self._p_width, profile.floor_width), (self._p_height, profile.floor_height),
                            (self._p_grid, profile.grid_cell_size)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._fill_robot_table()
        self._loading = False
        self._rebuild_scene()
        self._dirty = False
        self._update_status()

    def _rebuild_scene(self) -> None:
        """모델 전체로 씬을 다시 그린다."""
        self._scene.blockSignals(True)
        self._scene.clear()
        self._scene.blockSignals(False)
        self._station_items.clear()
        self._charger_items.clear()
        self._obstacle_items.clear()
        self._node_items.clear()
        self._edge_items.clear()
        self._pending_node = None
        self._selected = None
        self._props.setCurrentIndex(0)

        draw_grid(self._scene, self._factory)
        for obstacle in self._factory.obstacles:
            self._add_obstacle_item(obstacle)
        for station in self._factory.stations:
            self._add_station_item(station)
        for charger in self._factory.chargers:
            self._add_charger_item(charger)
        for node in self._factory.nodes:
            self._add_node_item(node)
        self._rebuild_edges()
        profile = self._factory.profile
        margin = 40.0
        self._scene.setSceneRect(-margin, -margin, profile.floor_width + 2 * margin, profile.floor_height + 2 * margin)
        self._view.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _attach(self, item: Any) -> None:
        """편집 콜백 연결 후 씬에 추가."""
        item.snapper = self._snap_point
        item.on_moved = self._on_item_moved
        self._scene.addItem(item)

    def _add_station_item(self, cfg: WorkStationConfig) -> EditableStation:
        """스테이션 아이템 추가."""
        item = EditableStation(cfg)
        self._attach(item)
        self._station_items[cfg.id] = item
        return item

    def _add_charger_item(self, cfg: ChargingStationConfig) -> EditableCharger:
        """충전소 아이템 추가."""
        item = EditableCharger(cfg)
        self._attach(item)
        self._charger_items[cfg.id] = item
        return item

    def _add_obstacle_item(self, cfg: ObstacleConfig) -> EditableObstacle:
        """장애물 아이템 추가."""
        item = EditableObstacle(cfg)
        self._attach(item)
        self._obstacle_items.append(item)
        return item

    def _add_node_item(self, node: PathNodeConfig) -> NodeItem:
        """노드 아이템 추가."""
        item = NodeItem(node)
        self._attach(item)
        self._node_items[node.id] = item
        return item

    def _rebuild_edges(self) -> None:
        """엣지 아이템을 다시 만든다 (차단 여부 재계산)."""
        for item in self._edge_items:
            self._scene.removeItem(item)
        self._edge_items.clear()
        for edge in self._factory.edges:
            a, b = self._node_items.get(edge.from_node), self._node_items.get(edge.to_node)
            if a is None or b is None:
                continue
            item = EdgeItem(edge)
            blocked = edge_blocked((a.node.x, a.node.y), (b.node.x, b.node.y), self._factory.obstacles)
            item.set_geometry(a.pos(), b.pos(), blocked)
            self._scene.addItem(item)
            self._edge_items.append(item)

    def _snap_point(self, point: QPointF) -> QPointF:
        """격자 스냅 (격자 크기의 절반 단위)."""
        if not self._snap.isChecked():
            return point
        step = max(5.0, self._factory.profile.grid_cell_size / 2.0)
        return QPointF(round(point.x() / step) * step, round(point.y() / step) * step)

    def _next_temp_id(self, items: list[Any]) -> int:
        """신규 항목용 음수 임시 ID."""
        return min([int(i.id) for i in items] + [0]) - 1

    @staticmethod
    def _next_code(prefix: str, existing: set[str]) -> str:
        """PREFIX-NN 미사용 ID."""
        index = 1
        while f"{prefix}-{index:02d}" in existing:
            index += 1
        return f"{prefix}-{index:02d}"

    def _mark_dirty(self) -> None:
        """변경 표시."""
        if not self._loading:
            self._dirty = True
            self._update_status()

    def _update_status(self) -> None:
        """하단 상태 문구."""
        f = self._factory
        state = "● 저장되지 않은 변경 있음" if self._dirty else "DB와 동기화됨"
        self._status.setText(f"스테이션 {len(f.stations)} · 충전소 {len(f.chargers)} · 로봇 {self._robot_table.rowCount()} · "
                             f"장애물 {len(f.obstacles)} · 노드 {len(f.nodes)} · 엣지 {len(f.edges)}   |   {state}")

    # ------------------------------------------------------------------
    # 편집 이벤트
    # ------------------------------------------------------------------
    def _set_mode(self, mode: str) -> None:
        """편집 모드 전환."""
        self._view.mode = mode
        self._view.setDragMode(QGraphicsView.DragMode.RubberBandDrag if mode == MODE_SELECT
                               else QGraphicsView.DragMode.NoDrag)
        self._clear_pending()

    def _on_drop(self, kind: str, pos: QPointF) -> None:
        """팔레트 드롭 → 신규 설비."""
        pos = self._snap_point(pos)
        x, y = pos.x(), pos.y()
        f = self._factory
        item: QGraphicsItem
        match kind:
            case "station":
                station_id = self._next_code("WS", {s.id for s in f.stations})
                cfg = WorkStationConfig(station_id, f"스테이션 {station_id[-2:]}", x, y)
                f.stations.append(cfg)
                item = self._add_station_item(cfg)
            case "charger":
                charger_id = self._next_code("CS", {ch.id for ch in f.chargers})
                cfg_c = ChargingStationConfig(charger_id, x, y)
                f.chargers.append(cfg_c)
                item = self._add_charger_item(cfg_c)
            case "wall" | "door":
                half = DEFAULT_WALL_LENGTH / 2
                obstacle = ObstacleConfig(self._next_temp_id(f.obstacles), kind, x - half, y, x + half, y,
                                          kind == "door", "문" if kind == "door" else "벽")
                f.obstacles.append(obstacle)
                item = self._add_obstacle_item(obstacle)
                self._rebuild_edges()
            case "obstacle":
                half = DEFAULT_OBSTACLE_SIZE / 2
                obstacle = ObstacleConfig(self._next_temp_id(f.obstacles), "obstacle", x - half, y - half, x + half,
                                          y + half, False, "장애물")
                f.obstacles.append(obstacle)
                item = self._add_obstacle_item(obstacle)
                self._rebuild_edges()
            case "node":
                node = PathNodeConfig(self._next_temp_id(f.nodes), x, y)
                f.nodes.append(node)
                item = self._add_node_item(node)
            case _:
                return
        self._scene.clearSelection()
        item.setSelected(True)
        self._mark_dirty()

    def _on_empty_click(self, pos: QPointF) -> None:
        """경로 모드: 빈 곳 클릭 → 노드 추가."""
        pos = self._snap_point(pos)
        profile = self._factory.profile
        if not (0 <= pos.x() <= profile.floor_width and 0 <= pos.y() <= profile.floor_height):
            return
        node = PathNodeConfig(self._next_temp_id(self._factory.nodes), pos.x(), pos.y())
        self._factory.nodes.append(node)
        item = self._add_node_item(node)
        if self._pending_node is not None:
            self._connect_nodes(self._pending_node, item)
        self._set_pending(item)
        self._mark_dirty()

    def _on_node_click(self, item: NodeItem) -> None:
        """경로 모드: 노드 클릭 → 엣지 연결 대기/완료."""
        if self._pending_node is None:
            self._set_pending(item)
            return
        if self._pending_node is not item:
            self._connect_nodes(self._pending_node, item)
        self._set_pending(item)

    def _set_pending(self, item: NodeItem | None) -> None:
        """엣지 시작 노드 지정."""
        self._clear_pending()
        self._pending_node = item
        if item is not None:
            item.set_highlight(True)

    def _clear_pending(self) -> None:
        """엣지 연결 대기 해제."""
        if self._pending_node is not None and self._pending_node.scene() is self._scene:
            self._pending_node.set_highlight(False)
        self._pending_node = None

    def _connect_nodes(self, a: NodeItem, b: NodeItem) -> None:
        """두 노드를 엣지로 연결 (중복 무시)."""
        ids = {a.node.id, b.node.id}
        if any({e.from_node, e.to_node} == ids for e in self._factory.edges):
            return
        edge = PathEdgeConfig(self._next_temp_id(self._factory.edges), a.node.id, b.node.id)
        self._factory.edges.append(edge)
        self._rebuild_edges()
        self._mark_dirty()

    def _on_item_moved(self, item: QGraphicsItem) -> None:
        """아이템 드래그 이동 → 모델 좌표 갱신."""
        pos = item.pos()
        if isinstance(item, (EditableStation, EditableCharger)):
            item.cfg.x, item.cfg.y = round(pos.x(), 1), round(pos.y(), 1)
        elif isinstance(item, EditableObstacle):
            dx, dy = pos.x() - item.cfg.x1, pos.y() - item.cfg.y1
            item.cfg.x1 += dx
            item.cfg.y1 += dy
            item.cfg.x2 += dx
            item.cfg.y2 += dy
            self._rebuild_edges()
        elif isinstance(item, NodeItem):
            item.node.x, item.node.y = round(pos.x(), 1), round(pos.y(), 1)
            self._rebuild_edges()
        if item is self._selected:
            self._populate_form(item)
        self._mark_dirty()

    def _delete_items(self, items: list[QGraphicsItem]) -> None:
        """선택 항목 삭제."""
        f = self._factory
        removed_nodes: set[int] = set()
        changed = False
        for item in items:
            if isinstance(item, EditableStation):
                f.stations = [s for s in f.stations if s.id != item.cfg.id]
                self._station_items.pop(item.cfg.id, None)
            elif isinstance(item, EditableCharger):
                f.chargers = [ch for ch in f.chargers if ch.id != item.cfg.id]
                self._charger_items.pop(item.cfg.id, None)
            elif isinstance(item, EditableObstacle):
                f.obstacles = [o for o in f.obstacles if o is not item.cfg]
                self._obstacle_items = [o for o in self._obstacle_items if o is not item]
            elif isinstance(item, NodeItem):
                removed_nodes.add(item.node.id)
                f.nodes = [n for n in f.nodes if n.id != item.node.id]
                self._node_items.pop(item.node.id, None)
                if item is self._pending_node:
                    self._pending_node = None
            elif isinstance(item, EdgeItem):
                f.edges = [e for e in f.edges if e is not item.edge]
            else:
                continue
            if item.scene() is self._scene:
                self._scene.removeItem(item)
            changed = True
        if removed_nodes:
            f.edges = [e for e in f.edges if e.from_node not in removed_nodes and e.to_node not in removed_nodes]
        if changed:
            self._selected = None
            self._props.setCurrentIndex(0)
            self._rebuild_edges()
            self._mark_dirty()

    def _generate_grid_network(self) -> None:
        """격자 경로망을 자동 생성해 기존 노드/엣지를 교체."""
        answer = QMessageBox.question(self, "격자 경로망", "기존 경로 노드/엣지를 모두 지우고 격자 경로망을 생성할까요?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        f = self._factory
        spacing = max(GRID_NETWORK_MIN_SPACING, f.profile.grid_cell_size * 2)
        f.nodes, f.edges = build_grid_network(f.profile.floor_width, f.profile.floor_height, spacing, f.stations,
                                              f.chargers)
        self._rebuild_scene()
        self._mark_dirty()

    # ------------------------------------------------------------------
    # 속성 패널
    # ------------------------------------------------------------------
    def _on_selection_changed(self) -> None:
        """선택 변경 → 속성 폼 표시."""
        selected = [i for i in self._scene.selectedItems()
                    if isinstance(i, (EditableStation, EditableCharger, EditableObstacle, NodeItem))]
        self._selected = selected[0] if len(selected) == 1 else None
        if self._selected is None:
            self._props.setCurrentIndex(0)
            return
        self._populate_form(self._selected)

    def _populate_form(self, item: QGraphicsItem) -> None:
        """선택 항목 값을 폼에 채운다 (시그널 차단)."""
        self._loading = True
        try:
            if isinstance(item, EditableStation):
                cfg = item.cfg
                self._props.setCurrentIndex(1)
                self._s_id.setText(cfg.id)
                self._set_values([(self._s_label, cfg.label), (self._s_x, cfg.x), (self._s_y, cfg.y),
                                  (self._s_w, cfg.width), (self._s_h, cfg.height), (self._s_type, cfg.task_type),
                                  (self._s_time, cfg.avg_process_time), (self._s_std, cfg.process_time_std),
                                  (self._s_active, cfg.is_active)])
            elif isinstance(item, EditableCharger):
                cfg_c = item.cfg
                self._props.setCurrentIndex(2)
                self._c_id.setText(cfg_c.id)
                self._set_values([(self._c_x, cfg_c.x), (self._c_y, cfg_c.y), (self._c_capacity, cfg_c.capacity),
                                  (self._c_rate, cfg_c.charge_rate), (self._c_active, cfg_c.is_active)])
            elif isinstance(item, EditableObstacle):
                cfg_o = item.cfg
                self._props.setCurrentIndex(3)
                self._set_values([(self._o_type, cfg_o.obstacle_type), (self._o_x1, cfg_o.x1), (self._o_y1, cfg_o.y1),
                                  (self._o_x2, cfg_o.x2), (self._o_y2, cfg_o.y2), (self._o_passable, cfg_o.is_passable),
                                  (self._o_label, cfg_o.label or "")])
            elif isinstance(item, NodeItem):
                node = item.node
                self._props.setCurrentIndex(4)
                self._n_id.setText(str(node.id) + (" (저장 전)" if node.id < 0 else ""))
                self._n_link.setText(node.linked_station_id or "-")
                self._set_values([(self._n_x, node.x), (self._n_y, node.y)])
        finally:
            self._loading = False

    @staticmethod
    def _set_values(pairs: list[tuple[QWidget, Any]]) -> None:
        """위젯 값 설정 (시그널 차단)."""
        for widget, value in pairs:
            widget.blockSignals(True)
            if isinstance(widget, (QDoubleSpinBox, QSpinBox)):
                widget.setValue(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))
            widget.blockSignals(False)

    def _apply_station_form(self) -> None:
        """스테이션 폼 → 모델."""
        item = self._selected
        if self._loading or not isinstance(item, EditableStation):
            return
        cfg = item.cfg
        cfg.label = self._s_label.text().strip() or cfg.id
        cfg.x, cfg.y = self._s_x.value(), self._s_y.value()
        cfg.width, cfg.height = max(10.0, self._s_w.value()), max(10.0, self._s_h.value())
        cfg.task_type = self._s_type.currentText()
        cfg.avg_process_time = max(0.1, self._s_time.value())
        cfg.process_time_std = self._s_std.value()
        cfg.is_active = self._s_active.isChecked()
        self._apply_silently(item, lambda: item.apply_config(cfg))
        self._mark_dirty()

    def _apply_charger_form(self) -> None:
        """충전소 폼 → 모델."""
        item = self._selected
        if self._loading or not isinstance(item, EditableCharger):
            return
        cfg = item.cfg
        cfg.x, cfg.y = self._c_x.value(), self._c_y.value()
        cfg.capacity = self._c_capacity.value()
        cfg.charge_rate = self._c_rate.value()
        cfg.is_active = self._c_active.isChecked()
        self._apply_silently(item, lambda: item.apply_config(cfg))
        self._mark_dirty()

    def _apply_obstacle_form(self) -> None:
        """장애물 폼 → 모델."""
        item = self._selected
        if self._loading or not isinstance(item, EditableObstacle):
            return
        cfg = item.cfg
        cfg.obstacle_type = self._o_type.currentText()
        cfg.x1, cfg.y1, cfg.x2, cfg.y2 = self._o_x1.value(), self._o_y1.value(), self._o_x2.value(), self._o_y2.value()
        cfg.is_passable = self._o_passable.isChecked()
        cfg.label = self._o_label.text().strip()
        self._apply_silently(item, lambda: item.apply_config(cfg))
        self._rebuild_edges()
        self._mark_dirty()

    def _apply_node_form(self) -> None:
        """노드 폼 → 모델."""
        item = self._selected
        if self._loading or not isinstance(item, NodeItem):
            return
        item.node.x, item.node.y = self._n_x.value(), self._n_y.value()
        self._apply_silently(item, lambda: item.setPos(QPointF(item.node.x, item.node.y)))
        self._rebuild_edges()
        self._mark_dirty()

    @staticmethod
    def _apply_silently(item: Any, action: Callable[[], None]) -> None:
        """스냅/이동 콜백 없이 아이템을 갱신한다 (폼 입력값을 그대로 반영)."""
        snapper, on_moved = item.snapper, item.on_moved
        item.snapper, item.on_moved = None, None
        try:
            action()
        finally:
            item.snapper, item.on_moved = snapper, on_moved

    def _on_profile_edited(self) -> None:
        """공장 정보 변경 → 바닥 다시 그림."""
        if self._loading:
            return
        profile = self._factory.profile
        profile.factory_name = self._p_name.text().strip() or config.DEFAULT_FACTORY_NAME
        profile.floor_width = self._p_width.value()
        profile.floor_height = self._p_height.value()
        profile.grid_cell_size = self._p_grid.value()
        self._rebuild_scene()
        self._mark_dirty()

    # ------------------------------------------------------------------
    # 로봇 테이블
    # ------------------------------------------------------------------
    def _fill_robot_table(self) -> None:
        """모델 로봇 → 표."""
        table = self._robot_table
        table.blockSignals(True)
        table.setRowCount(0)
        for robot in self._factory.robots:
            self._append_robot_row(robot)
        table.blockSignals(False)

    def _append_robot_row(self, robot: RobotConfig) -> None:
        """표에 로봇 한 행 추가."""
        table = self._robot_table
        row = table.rowCount()
        table.insertRow(row)
        values = (robot.id, robot.robot_type, robot.battery_capacity, robot.move_speed, robot.battery_drain_rate,
                  robot.charge_threshold)
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if column == 0:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, column, item)
        active = QTableWidgetItem()
        active.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        active.setCheckState(Qt.CheckState.Checked if robot.is_active else Qt.CheckState.Unchecked)
        table.setItem(row, len(values), active)

    def _robot_ids_in_table(self) -> set[str]:
        """표의 로봇 ID."""
        return {self._robot_table.item(r, 0).text() for r in range(self._robot_table.rowCount())}

    def _add_robot_row(self) -> None:
        """로봇 추가 (첫 로봇 파라미터 복사)."""
        template = self._read_robot_table()[:1]
        base = template[0] if template else RobotConfig("R-00")
        robot_id = self._next_code("R", self._robot_ids_in_table())
        self._robot_table.blockSignals(True)
        self._append_robot_row(RobotConfig(robot_id, base.robot_type, base.battery_capacity, base.move_speed,
                                           base.battery_drain_rate, base.charge_threshold, True))
        self._robot_table.blockSignals(False)
        self._mark_dirty()

    def _remove_robot_rows(self) -> None:
        """선택 로봇 삭제."""
        rows = sorted({i.row() for i in self._robot_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self._robot_table.removeRow(row)
        if rows:
            self._mark_dirty()

    def _read_robot_table(self) -> list[RobotConfig]:
        """표 → 로봇 설정 (숫자 오류 시 ValueError)."""
        robots = []
        table = self._robot_table
        for row in range(table.rowCount()):
            cells = [table.item(row, col).text().strip() for col in range(len(ROBOT_COLUMNS) - 1)]
            try:
                robots.append(RobotConfig(
                    cells[0], cells[1] or config.DEFAULT_ROBOT_TYPE, float(cells[2]), float(cells[3]), float(cells[4]),
                    float(cells[5]), table.item(row, len(ROBOT_COLUMNS) - 1).checkState() == Qt.CheckState.Checked,
                ))
            except ValueError as exc:
                raise ValueError(f"{cells[0]} 로봇 파라미터가 숫자가 아닙니다: {exc}") from exc
        return robots

    # ------------------------------------------------------------------
    # 저장 / 프리셋
    # ------------------------------------------------------------------
    def _collect(self) -> FactoryConfig:
        """현재 편집 상태를 FactoryConfig로 만든다."""
        self._factory.robots = self._read_robot_table()
        return self._factory

    def _save(self) -> None:
        """DB 저장 → 핫리로드."""
        try:
            factory = self._collect()
            if not factory.stations:
                raise ValueError("작업 스테이션이 최소 1개 필요합니다.")
            if not factory.nodes:
                raise ValueError("경로 노드가 없습니다. '격자 경로망 자동 생성'을 사용해 보세요.")
            changes = self._service.save_factory_config(factory)
        except Exception as exc:  # noqa: BLE001 — 사용자에게 저장 실패 사유 표시
            QMessageBox.critical(self, "저장 실패", str(exc))
            return
        self._load_factory(self._service.load_factory_config())
        self._status.setText(self._status.text() + f"   |   ✅ 저장 완료: {changes}건 변경 → 시뮬레이터 핫리로드")
        self.saved.emit(changes)

    def _revert(self) -> None:
        """DB에서 다시 불러오기."""
        if self._dirty and QMessageBox.question(self, "되돌리기", "저장하지 않은 변경을 버릴까요?") != \
                QMessageBox.StandardButton.Yes:
            return
        self._load_factory(self._service.load_factory_config())

    def _refresh_presets(self) -> None:
        """프리셋 목록."""
        self._presets.clear()
        self._presets.addItems(self._service.list_presets())

    def _save_preset(self) -> None:
        """현재 구성을 프리셋으로 저장."""
        name, ok = QInputDialog.getText(self, "프리셋 저장", "프리셋 이름 (업체/공장명):", text=self._presets.currentText())
        if not ok or not name.strip():
            return
        try:
            self._service.save_preset(name.strip(), copy.deepcopy(self._collect()))
        except ValueError as exc:
            QMessageBox.warning(self, "프리셋 저장", str(exc))
            return
        self._refresh_presets()
        self._presets.setCurrentText(name.strip())

    def _load_preset(self) -> None:
        """프리셋을 에디터로 불러오기 (저장해야 DB 반영)."""
        name = self._presets.currentText()
        preset = self._service.load_preset(name) if name else None
        if preset is None:
            return
        self._load_factory(self._with_temp_ids(preset))
        self._dirty = True
        self._update_status()
        self._status.setText(self._status.text() + f"   |   프리셋 '{name}' 불러옴 — [저장]을 눌러 DB에 반영")

    @staticmethod
    def _with_temp_ids(factory: FactoryConfig) -> FactoryConfig:
        """프리셋의 자동증가 ID를 음수 임시 ID로 바꿔 DB의 기존 행과 충돌하지 않게 한다."""
        result = copy.deepcopy(factory)
        mapping: dict[int, int] = {}
        for index, node in enumerate(result.nodes, start=1):
            mapping[node.id] = -index
            node.id = -index
        for index, edge in enumerate(result.edges, start=1):
            edge.id = -index
            edge.from_node = mapping.get(edge.from_node, edge.from_node)
            edge.to_node = mapping.get(edge.to_node, edge.to_node)
        for index, obstacle in enumerate(result.obstacles, start=1):
            obstacle.id = -index
        return result

    def _delete_preset(self) -> None:
        """프리셋 삭제."""
        name = self._presets.currentText()
        if name and QMessageBox.question(self, "프리셋 삭제", f"'{name}' 프리셋을 삭제할까요?") == \
                QMessageBox.StandardButton.Yes:
            self._service.delete_preset(name)
            self._refresh_presets()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 — Qt override
        """저장하지 않은 변경 확인."""
        if self._dirty and QMessageBox.question(self, "닫기", "저장하지 않은 변경이 있습니다. 닫을까요?") != \
                QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        super().closeEvent(event)
