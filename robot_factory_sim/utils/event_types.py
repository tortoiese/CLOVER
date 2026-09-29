"""이벤트 / 상태 / 테이블 이름 상수 모음.

모든 패키지(database, simulation, agent, gui)가 공통으로 참조하는 최하위 모듈이다.
"""

from typing import Final


class RobotState:
    """로봇 상태 상수."""

    IDLE: Final[str] = "idle"
    MOVING: Final[str] = "moving"
    WAITING: Final[str] = "waiting"          # 스테이션 대기열
    WORKING: Final[str] = "working"
    CHARGE_WAIT: Final[str] = "charge_wait"  # 충전기 대기열
    CHARGING: Final[str] = "charging"
    FAULT: Final[str] = "fault"

    ALL: Final[tuple[str, ...]] = (IDLE, MOVING, WAITING, WORKING, CHARGE_WAIT, CHARGING, FAULT)
    BUSY: Final[frozenset[str]] = frozenset({MOVING, WORKING, WAITING})

    LABELS: Final[dict[str, str]] = {
        IDLE: "대기",
        MOVING: "이동",
        WAITING: "작업대기",
        WORKING: "작업",
        CHARGE_WAIT: "충전대기",
        CHARGING: "충전",
        FAULT: "고장",
    }


class ProductionEventType:
    """production_events.event_type 상수."""

    CRITICAL_ORDER: Final[str] = "critical_order"
    DEADLINE_RISK: Final[str] = "deadline_risk"
    DEMAND_SURGE: Final[str] = "demand_surge"
    ORDER_CANCELLED: Final[str] = "order_cancelled"
    ORDER_REDUCED: Final[str] = "order_reduced"
    EQUIPMENT_FAILURE: Final[str] = "equipment_failure"
    EQUIPMENT_RESTORED: Final[str] = "equipment_restored"
    ORDER_COMPLETED: Final[str] = "order_completed"

    LABELS: Final[dict[str, str]] = {
        CRITICAL_ORDER: "🔴 긴급 주문",
        DEADLINE_RISK: "🟠 납기 임박",
        DEMAND_SURGE: "🟡 생산량 급증",
        ORDER_CANCELLED: "🔵 주문 취소",
        ORDER_REDUCED: "🔵 주문 감소",
        EQUIPMENT_FAILURE: "⚫ 설비 고장",
        EQUIPMENT_RESTORED: "✅ 설비 복구",
        ORDER_COMPLETED: "✅ 주문 완료",
    }


class TimelineKind:
    """이벤트 타임라인(대시보드 Tab 7) 마커 종류."""

    EMERGENCY: Final[str] = "emergency"   # 🔴
    CONFIG: Final[str] = "config"         # ⚙
    SUCCESS: Final[str] = "success"       # ✅
    FAILURE: Final[str] = "failure"       # ⚫
    INFO: Final[str] = "info"


class Severity:
    """알림 심각도."""

    INFO: Final[str] = "info"
    WARNING: Final[str] = "warning"
    CRITICAL: Final[str] = "critical"


class ChangeType:
    """config_changelog.change_type 상수."""

    INSERT: Final[str] = "INSERT"
    UPDATE: Final[str] = "UPDATE"
    DELETE: Final[str] = "DELETE"


class OrderStatus:
    """production_orders.status 상수."""

    PENDING: Final[str] = "pending"
    IN_PROGRESS: Final[str] = "in_progress"
    COMPLETED: Final[str] = "completed"
    CANCELLED: Final[str] = "cancelled"

    ACTIVE: Final[frozenset[str]] = frozenset({PENDING, IN_PROGRESS})


class Tables:
    """DB 테이블 이름 상수."""

    FACTORY_PROFILE: Final[str] = "factory_profile"
    WORK_STATIONS: Final[str] = "work_stations"
    CHARGING_STATIONS: Final[str] = "charging_stations"
    ROBOTS: Final[str] = "robots"
    OBSTACLES: Final[str] = "obstacles"
    PATH_NODES: Final[str] = "path_nodes"
    PATH_EDGES: Final[str] = "path_edges"
    CHANGELOG: Final[str] = "config_changelog"
    ORDERS: Final[str] = "production_orders"
    EVENTS: Final[str] = "production_events"
    PRESETS: Final[str] = "config_presets"

    CONFIG_TABLES: Final[frozenset[str]] = frozenset(
        {FACTORY_PROFILE, WORK_STATIONS, CHARGING_STATIONS, ROBOTS, OBSTACLES, PATH_NODES, PATH_EDGES}
    )

    LABELS: Final[dict[str, str]] = {
        FACTORY_PROFILE: "공장 정보",
        WORK_STATIONS: "작업 스테이션",
        CHARGING_STATIONS: "충전소",
        ROBOTS: "로봇",
        OBSTACLES: "장애물/벽/문",
        PATH_NODES: "경로 노드",
        PATH_EDGES: "경로 엣지",
        ORDERS: "생산 주문",
    }
