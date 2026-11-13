"""GUI 상수 (색상 / 크기 / 문구). 위젯 코드에 하드코딩하지 않고 여기서 관리한다."""

from __future__ import annotations

from typing import Final

from utils.event_types import RobotState, Severity, TimelineKind

# 통행 상태는 기본 경로망 위에 표시하며, 실시간 오버레이 갱신을 제한한다.
TRAFFIC_REFRESH_SECONDS: float = 0.2
TRAFFIC_NODE_RADIUS: float = 9.0
TRAFFIC_ARROW_SIZE: float = 11.0
TRAFFIC_BUSY_WIDTH: float = 3.2
TRAFFIC_IDLE_WIDTH: float = 1.0

# ---------------------------------------------------------------------------
# 테마 색상
# ---------------------------------------------------------------------------
BG: Final[str] = "#151a1e"
PANEL: Final[str] = "#20272b"
ACCENT: Final[str] = "#293632"
HIGHLIGHT: Final[str] = "#73b8a3"
TEXT: Final[str] = "#eef2f1"
TEXT_MUTED: Final[str] = "#acb9b8"
GRID: Final[str] = "#343e43"
FLOOR: Final[str] = "#171d20"
CRITICAL: Final[str] = "#e74c3c"
WARNING: Final[str] = "#f39c12"
SUCCESS: Final[str] = "#8bcda3"
INFO: Final[str] = "#3498db"
ELEVATOR: Final[str] = "#5dade2"

SEVERITY_COLORS: Final[dict[str, str]] = {
    Severity.INFO: INFO,
    Severity.WARNING: WARNING,
    Severity.CRITICAL: CRITICAL,
}

ROBOT_STATE_COLORS: Final[dict[str, str]] = {
    RobotState.MOVING: "#3498db",
    RobotState.WORKING: "#8bcda3",
    RobotState.WAITING: "#27ae60",
    RobotState.CHARGING: "#f1c40f",
    RobotState.CHARGE_WAIT: "#d4ac0d",
    RobotState.IDLE: "#95a5a6",
    RobotState.FAULT: "#e74c3c",
}
EMERGENCY_COLOR: Final[str] = "#f39c12"

ROBOT_PALETTE: Final[tuple[str, ...]] = (
    "#ff6b6b", "#4ecdc4", "#ffd93d", "#6c5ce7", "#a8e6cf", "#ff8b94", "#74b9ff", "#fdcb6e",
    "#e17055", "#00cec9", "#fd79a8", "#55efc4", "#b2bec3", "#fab1a0", "#81ecec", "#dfe6e9",
)

TASK_TYPE_COLORS: Final[dict[str, str]] = {
    "assembly": "#3d7ea6",
    "welding": "#c0392b",
    "painting": "#8e44ad",
    "inspection": "#16a085",
    "packaging": "#d35400",
    "machining": "#7f8c8d",
}
DEFAULT_TASK_COLOR: Final[str] = "#34495e"

OBSTACLE_COLORS: Final[dict[str, str]] = {
    "wall": "#5d6d7e",
    "door": "#48c9b0",
    "obstacle": "#7b6d5d",
}

# ---------------------------------------------------------------------------
# 3D 건물 뷰 (BuildingView)
# ---------------------------------------------------------------------------
BUILDING_SLAB: Final[str] = "#2e4144"          # 층 바닥판
BUILDING_SLAB_EDGE: Final[str] = "#527075"
BUILDING_SLAB_SIDE: Final[str] = "#18282c"
BUILDING_BG_TOP: Final[str] = "#263035"
BUILDING_BG_BOTTOM: Final[str] = "#192126"
BUILDING_WALL_HEIGHT: Final[float] = 26.0
BUILDING_STATION_HEIGHT: Final[float] = 24.0
BUILDING_FLOOR_GAP: Final[float] = 340.0       # 층 간격 (월드 단위)
BUILDING_SLAB_THICKNESS: Final[float] = 10.0
BUILDING_YAW_DEG: Final[float] = 32.0
BUILDING_PITCH_DEG: Final[float] = 22.0
BUILDING_PITCH_RANGE_DEG: Final[tuple[float, float]] = (12.0, 62.0)
BUILDING_WALL_STEP: Final[float] = 40.0        # 긴 벽을 깊이 정렬용으로 쪼개는 길이
BUILDING_ANIMATION_MS: Final[int] = 560
BUILDING_DRAG_THRESHOLD: Final[int] = 5

# ---------------------------------------------------------------------------
# 도면 축척 (1 : N). 격자 1칸이 종이 위에서 딱 떨어지는 값만 쓴다.
# ---------------------------------------------------------------------------
SCALE_RATIOS: Final[tuple[int, ...]] = (25, 50, 100, 200, 250, 500, 1000)
MM_PER_INCH: Final[float] = 25.4

TIMELINE_STYLE: Final[dict[str, tuple[int, str, str, str]]] = {
    # kind: (row, color, symbol, label)
    TimelineKind.EMERGENCY: (4, CRITICAL, "o", "🔴 긴급"),
    TimelineKind.CONFIG: (3, INFO, "s", "⚙ 설정"),
    TimelineKind.SUCCESS: (2, SUCCESS, "t1", "✅ 완료"),
    TimelineKind.FAILURE: (1, "#bdc3c7", "x", "⚫ 고장"),
    TimelineKind.INFO: (0, TEXT_MUTED, "d", "ℹ 정보"),
}

ENGINE_COLORS: Final[dict[str, str]] = {
    "optimized": HIGHLIGHT,
    "baseline": "#9baab5",
    "assignment_milp_legacy": "#f9b572",
    "rh_milp_grid": "#67d6e8",
    "rh_cpsat_linear": "#d890e8",
    "rh_cpsat_interval": "#b7a6ed",
    "rh_cpsat_native": "#f0a5c4",
}

# ---------------------------------------------------------------------------
# 크기
# ---------------------------------------------------------------------------
ROBOT_RADIUS: Final[float] = 11.0
BATTERY_BAR_WIDTH: Final[float] = 26.0
BATTERY_BAR_HEIGHT: Final[float] = 4.0
CHARGER_SIZE: Final[float] = 34.0
PATH_NODE_RADIUS: Final[float] = 3.0
EDITOR_NODE_RADIUS: Final[float] = 6.0
ROUTE_DASH: Final[tuple[float, float]] = (4.0, 4.0)

KPI_COLUMNS: Final[int] = 3
KPI_DETAILS_HEIGHT: Final[int] = 176
CHART_REFRESH_MS: Final[int] = 500
ANIMATION_INTERVAL_MS: Final[int] = 80
BLINK_INTERVAL_TICKS: Final[int] = 5
WINDOW_PREFERRED_SIZE: Final[tuple[int, int]] = (1680, 960)
WINDOW_FRAME_MARGIN: Final[tuple[int, int]] = (24, 48)
WORKSPACE_PANE_SIZES: Final[tuple[int, int]] = (760, 480)
WORKSPACE_MIN_FACTORY_WIDTH: Final[int] = 300
WORKSPACE_MIN_DASHBOARD_WIDTH: Final[int] = 360
WORKSPACE_HANDLE_WIDTH: Final[int] = 8
WORKSPACE_STATUS_HEIGHT: Final[int] = 48
CONSOLE_MAX_WIDTH: Final[int] = 900
BANNER_AUTO_HIDE_MS: Final[int] = 20000
CONFIG_RELOAD_DEBOUNCE_MS: Final[int] = 300
ORDER_DIALOG_REFRESH_MS: Final[int] = 2000
CHAT_MAX_MESSAGES: Final[int] = 300
AGENT_STACK_BYTES: Final[int] = 4 * 1024 * 1024
DEMO_ORDER_QUANTITY: Final[int] = 60       # 엘리베이터 시연 주문 수량
DEMO_DEADLINE_SEC: Final[float] = 1800.0   # 시연 주문 납기 (sim-sec)

HISTORY_MAX_POINTS: Final[int] = 1200
HISTORY_SAMPLE_INTERVAL: Final[float] = 2.0  # 히스토리 샘플 간격 (sim-sec)
THROUGHPUT_START_RANGE: Final[float] = 5.0  # 초기 축의 최소 범위 (분, 건/분)
HISTORY_START_MINUTES: Final[float] = 5.0

LOW_BATTERY: Final[float] = 30.0
MID_BATTERY: Final[float] = 60.0

# ---------------------------------------------------------------------------
# 문구
# ---------------------------------------------------------------------------
APP_TITLE: Final[str] = "Robot Factory Simulator — CLOVER"
SIMULATION_TAB_TITLE: Final[str] = "시뮬레이션"
CONSOLE_TITLE: Final[str] = "관제 콘솔"
CONSOLE_START_HINT: Final[str] = "상단의 [시작]을 누르면 시뮬레이션과 주문 감시를 시작합니다."
QUICK_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("현재 상태", "현재 상태 알려줘"),
    ("효율 분석", "현재 효율 분석해줘"),
    ("병목", "지금 가장 병목인 구간이 어디야?"),
    ("주문 현황", "현재 주문 현황 보여줘"),
    ("납기 위험", "납기 위험 주문 있어?"),
    ("긴급 주문", "긴급 주문 넣어줘: 제품B 150개, 1시간 내"),
)
CHART_TABS: Final[tuple[str, ...]] = (
    "처리량", "로봇 타임라인", "대기열", "배터리", "KPI 레이더", "주문 진행", "이벤트",
)
PALETTE_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("station", "🏭 작업 스테이션"),
    ("charger", "⚡ 충전소"),
    ("elevator", "🛗 엘리베이터"),
    ("wall", "▬ 벽"),
    ("door", "🚪 문"),
    ("obstacle", "▦ 장애물"),
    ("node", "• 경로 노드"),
)
PALETTE_MIME: Final[str] = "application/x-factory-palette"
