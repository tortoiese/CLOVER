"""GUI 상수 (색상 / 크기 / 문구). 위젯 코드에 하드코딩하지 않고 여기서 관리한다."""

from __future__ import annotations

from typing import Final

from utils.event_types import RobotState, Severity, TimelineKind

# ---------------------------------------------------------------------------
# 테마 색상
# ---------------------------------------------------------------------------
BG: Final[str] = "#1a1a2e"
PANEL: Final[str] = "#16213e"
ACCENT: Final[str] = "#0f3460"
HIGHLIGHT: Final[str] = "#4f8cff"
TEXT: Final[str] = "#e6e9f5"
TEXT_MUTED: Final[str] = "#8b93b5"
GRID: Final[str] = "#22304f"
FLOOR: Final[str] = "#121a33"
CRITICAL: Final[str] = "#e74c3c"
WARNING: Final[str] = "#f39c12"
SUCCESS: Final[str] = "#2ecc71"
INFO: Final[str] = "#3498db"

SEVERITY_COLORS: Final[dict[str, str]] = {
    Severity.INFO: INFO,
    Severity.WARNING: WARNING,
    Severity.CRITICAL: CRITICAL,
}

ROBOT_STATE_COLORS: Final[dict[str, str]] = {
    RobotState.MOVING: "#3498db",
    RobotState.WORKING: "#2ecc71",
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

TIMELINE_STYLE: Final[dict[str, tuple[int, str, str, str]]] = {
    # kind: (row, color, symbol, label)
    TimelineKind.EMERGENCY: (4, CRITICAL, "o", "🔴 긴급"),
    TimelineKind.CONFIG: (3, INFO, "s", "⚙ 설정"),
    TimelineKind.SUCCESS: (2, SUCCESS, "t1", "✅ 완료"),
    TimelineKind.FAILURE: (1, "#bdc3c7", "x", "⚫ 고장"),
    TimelineKind.INFO: (0, TEXT_MUTED, "d", "ℹ 정보"),
}

ENGINE_COLORS: Final[dict[str, str]] = {"optimized": HIGHLIGHT, "baseline": "#9aa3c7"}

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

KPI_COLUMNS: Final[int] = 4
CHART_REFRESH_MS: Final[int] = 500
ANIMATION_INTERVAL_MS: Final[int] = 80
BLINK_INTERVAL_TICKS: Final[int] = 5
BANNER_HEIGHT: Final[int] = 58
BANNER_ANIMATION_MS: Final[int] = 280
BANNER_AUTO_HIDE_MS: Final[int] = 20000
CONFIG_RELOAD_DEBOUNCE_MS: Final[int] = 300
ORDER_DIALOG_REFRESH_MS: Final[int] = 2000
CHAT_MAX_MESSAGES: Final[int] = 300

HISTORY_WINDOW: Final[float] = 1800.0     # 그래프 표시 구간 (sim-sec)
GANTT_WINDOW: Final[float] = 300.0        # 간트 표시 구간 (sim-sec)
HISTORY_SAMPLE_INTERVAL: Final[float] = 2.0  # 히스토리 샘플 간격 (sim-sec)

LOW_BATTERY: Final[float] = 30.0
MID_BATTERY: Final[float] = 60.0

# ---------------------------------------------------------------------------
# 문구
# ---------------------------------------------------------------------------
APP_TITLE: Final[str] = "Robot Factory Simulator — CLOVER"
CONSOLE_TITLE: Final[str] = "🤖 AI 관제 콘솔"
QUICK_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    ("현재 상태", "현재 상태 알려줘"),
    ("효율 분석", "현재 효율 분석해줘"),
    ("병목", "지금 가장 병목인 구간이 어디야?"),
    ("주문 현황", "현재 주문 현황 보여줘"),
    ("납기 위험", "납기 위험 주문 있어?"),
    ("긴급 주문", "긴급 주문 넣어줘: 제품B 150개, 1시간 내"),
)
CHART_TABS: Final[tuple[str, ...]] = (
    "Throughput", "로봇 타임라인", "대기열", "배터리", "KPI 레이더", "주문 진행", "이벤트",
)
PALETTE_ITEMS: Final[tuple[tuple[str, str], ...]] = (
    ("station", "🏭 작업 스테이션"),
    ("charger", "⚡ 충전소"),
    ("wall", "▬ 벽"),
    ("door", "🚪 문"),
    ("obstacle", "▦ 장애물"),
    ("node", "• 경로 노드"),
)
PALETTE_MIME: Final[str] = "application/x-factory-palette"
