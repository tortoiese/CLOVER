"""시뮬레이션 기본 파라미터 및 경로 설정.

DB 값이 항상 우선이며, 이 파일의 값은 DB에 값이 없을 때 사용하는 fallback이다.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# 경로
# ---------------------------------------------------------------------------
BASE_DIR: Path = Path(__file__).resolve().parent
DB_PATH: Path = BASE_DIR / "factory_config.db"
SCHEMA_PATH: Path = BASE_DIR / "database" / "schema.sql"
QSS_PATH: Path = BASE_DIR / "gui" / "styles" / "theme.qss"

# ---------------------------------------------------------------------------
# 단위 / 공장 기본값 (fallback)
# ---------------------------------------------------------------------------
METERS_PER_PIXEL: float = 0.1          # 화면 1px = 0.1m
DEFAULT_FACTORY_NAME: str = "Default Factory"
DEFAULT_FLOOR_WIDTH: float = 800.0
DEFAULT_FLOOR_HEIGHT: float = 600.0
DEFAULT_GRID_CELL_SIZE: float = 50.0

DEFAULT_STATION_WIDTH: float = 80.0
DEFAULT_STATION_HEIGHT: float = 60.0
DEFAULT_TASK_TYPE: str = "assembly"
DEFAULT_PROCESS_TIME: float = 5.0      # sim-sec
DEFAULT_PROCESS_TIME_STD: float = 1.0

DEFAULT_CHARGER_CAPACITY: int = 2
DEFAULT_CHARGE_RATE: float = 2.0       # 배터리 단위 / sim-sec

DEFAULT_ROBOT_TYPE: str = "AGV"
DEFAULT_BATTERY_CAPACITY: float = 100.0
DEFAULT_MOVE_SPEED: float = 2.0        # m/s
DEFAULT_BATTERY_DRAIN_RATE: float = 0.5  # 배터리 단위 / m
DEFAULT_CHARGE_THRESHOLD: float = 20.0   # %

TASK_TYPES: tuple[str, ...] = ("assembly", "welding", "painting", "inspection", "packaging", "machining")
OBSTACLE_TYPES: tuple[str, ...] = ("wall", "door", "obstacle")
NODE_TYPES: tuple[str, ...] = ("waypoint", "station", "charger")
ORDER_PRIORITIES: tuple[str, ...] = ("critical", "high", "normal", "low")

# ---------------------------------------------------------------------------
# 시뮬레이션 런타임
# ---------------------------------------------------------------------------
RANDOM_SEED: int = 42
SPEED_OPTIONS: tuple[int, ...] = (1, 2, 5, 10)
DEFAULT_SPEED: int = 1
DEFAULT_SCHEDULER_MODE: str = "optimized"   # "baseline" | "optimized"
SCHEDULER_MODES: tuple[str, ...] = ("baseline", "optimized")

WORKER_LOOP_INTERVAL_MS: int = 25       # SimWorker 루프 주기
STATE_EMIT_INTERVAL: float = 0.08       # 상태 스냅샷 emit 간격 (wall-sec)
MAX_WALL_STEP: float = 0.25             # 한 루프에서 진행할 최대 wall-sec
ORDER_FLUSH_INTERVAL: float = 1.0       # 주문 진행률 DB 반영 간격 (wall-sec)
POLL_INTERVAL: float = 1.0              # ConfigWatcher / EventWatcher 폴링 간격

DISPATCH_INTERVAL: float = 0.5          # 스케줄러 배정 주기 (sim-sec)
MONITOR_INTERVAL: float = 5.0           # 납기/급증 감시 주기 (sim-sec)
ORDER_RELEASE_INTERVAL: float = 1.0     # 주문 작업 릴리즈 주기 (sim-sec)

DEFAULT_TASK_RATE: float = 0.12         # 포아송 λ (tasks / sim-sec)
TASK_RATE_MIN: float = 0.01
TASK_RATE_MAX: float = 2.0
MAX_BACKGROUND_TASKS: int = 40
ORDER_PIPELINE_PER_ROBOT: int = 2       # 주문당 동시 진행 작업 수 (로봇 1대당)

CHARGE_STEP: float = 1.0                # 충전 루프 단위 (sim-sec)
WORK_DRAIN_PER_SEC: float = 0.05        # 작업 중 배터리 소모 (단위/sec)
FULL_CHARGE_LEVEL: float = 100.0        # Baseline 충전 목표 (%)
OPTIMIZED_CHARGE_TARGET: float = 90.0   # 최적화 충전 목표 (%)
MIN_CHARGE_TARGET: float = 50.0         # 최소충전 복귀 모드 목표 (%)
OPPORTUNISTIC_CHARGE_LEVEL: float = 60.0  # 유휴 시 선제 충전 기준 (%)
PRECHARGE_LEVEL: float = 80.0           # 주문 감소 시 선충전 기준 (%)
BATTERY_RESERVE: float = 10.0           # 작업 후 최소 잔량 (%)
FAULT_RECOVERY_TIME: float = 60.0       # 배터리 방전 후 구조까지 (sim-sec)
RESCUE_BATTERY_LEVEL: float = 15.0      # 구조 후 배터리 (%)
RECALL_MIN_BATTERY: float = 50.0        # 긴급 복귀 가능한 최소 배터리 (%)

DEADLINE_RISK_FACTOR: float = 1.3       # 남은시간 < 예상소요 × 1.3 → 납기 임박
SURGE_UNITS_PER_ROBOT: int = 60         # 활성 주문 잔량 합계 임계치 (로봇 1대당)
DEFAULT_TASK_CYCLE: float = 20.0        # 작업 1건 평균 소요 추정치 (sim-sec)

THROUGHPUT_WINDOW: float = 120.0        # 처리량 계산 윈도우 (sim-sec)
METRIC_SAMPLE_LIMIT: int = 500

SCHEDULER_WEIGHTS: dict[str, float] = {
    "distance": 1.0,
    "battery": 0.6,
    "queue": 0.8,
    "priority": 1.5,
    "deadline": 1.2,
}

PRIORITY_LEVELS: dict[str, int] = {"critical": 0, "high": 1, "normal": 2, "low": 3}
BACKGROUND_PRIORITY: int = 3
BACKGROUND_PRODUCT: str = "general"

# ---------------------------------------------------------------------------
# ConfigWatcher
# ---------------------------------------------------------------------------
WATCHER_IGNORED_ACTORS: frozenset[str] = frozenset({"simulation", "event_watcher"})
WATCHER_IGNORED_TABLES: frozenset[str] = frozenset({"production_events", "config_presets"})
