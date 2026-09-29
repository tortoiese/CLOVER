"""MockAgent — 규칙(정규식) 기반 관제 에이전트.

LLM 없이 자연어 명령을 해석하고, 생산 이벤트에 자율 대응 계획(AgentAction)을 세운다.
실제 RAG + LLM 에이전트는 BaseAgent를 구현하여 이 클래스를 대체한다.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import config
from agent.base import BaseAgent
from agent.models import AgentAction, AgentResponse, BottleneckReport, ProductionEvent
from utils.event_types import ActionKind, ProductionEventType, RobotState, SchedulerModeLabel, Severity
from utils.metrics import KPI_DEFINITIONS

Context = dict[str, Any]

ROBOT_ID_RE = re.compile(r"(?<![A-Za-z])[Rr]-?(\d{1,3})(?!\d)")
STATION_ID_RE = re.compile(r"(?<![A-Za-z])[Ww][Ss]-?(\d{1,3})(?!\d)")
ORDER_ID_RE = re.compile(r"ORD-\d{4}-\d{4}", re.IGNORECASE)
POINT_RE = re.compile(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")
COUNT_RE = re.compile(r"(\d+)\s*대")
QTY_RE = re.compile(r"(\d+)\s*개")
HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*시간")
MINUTES_RE = re.compile(r"(\d+)\s*분")
SPEED_RE = re.compile(r"(\d+)\s*(?:배속|배|x|X)")
PRODUCT_RE = re.compile(r"제품\s*([A-Za-z0-9]+)")

MAX_DEDICATED_STATIONS: int = 2
DEADLINE_SAFETY: float = 0.9          # 납기의 90% 안에 끝내도록 로봇 수 산정
DEFAULT_EMERGENCY_SHARE: float = 0.6  # 납기 없는 긴급 주문에 투입할 로봇 비율
LOW_BATTERY_WARN: float = 30.0
TASK_RATE_UP: float = 1.5
TASK_RATE_DOWN: float = 1 / 1.5
MIN_IDLE_BATTERY: float = 25.0

HELP_TEXT = """사용 가능한 명령 예시:
├ 현재 상태 알려줘 / 현재 효율 분석해줘 / 지금 가장 병목인 구간이 어디야?
├ 현재 주문 현황 보여줘 / 납기 위험 주문 있어?
├ 긴급 주문 넣어줘: 제품A 300개, 2시간 내
├ 로봇 3대 추가해줘 / R-02 충전해
├ 배속 5배로 / 작업 생성 빈도 높여줘 / 스케줄링 최적화 모드로 전환
├ WS-03 고장 처리해 / WS-03 복구해 / WS-03 위치를 (200, 150)으로 옮겨줘
├ 충전소 하나 더 추가해 위치 (400, 300)
└ 문 위치를 (500, 0)-(500, 100)으로 옮겨 / ORD-2026-0001 주문 취소해"""


# ----------------------------------------------------------------------
# 포맷 / 계산 보조
# ----------------------------------------------------------------------
def fmt_duration(seconds: float | None) -> str:
    """sim-sec를 사람이 읽는 시간으로 바꾼다.

    Args:
        seconds: 초

    Returns:
        "1.7시간" / "12.5분" / "40초" / "산정 불가"
    """
    if seconds is None or math.isnan(seconds) or math.isinf(seconds):
        return "산정 불가"
    if abs(seconds) >= 3600:
        return f"{seconds / 3600:.1f}시간"
    if abs(seconds) >= 60:
        return f"{seconds / 60:.1f}분"
    return f"{seconds:.0f}초"


def fmt_value(value: float, decimals: int = 1) -> str:
    """NaN 안전 숫자 포맷."""
    return "—" if value is None or math.isnan(value) else f"{value:.{decimals}f}"


def normalize_robot_id(number: str) -> str:
    """'2' → 'R-02'."""
    return f"R-{int(number):02d}"


def normalize_station_id(number: str) -> str:
    """'3' → 'WS-03'."""
    return f"WS-{int(number):02d}"


def estimate_eta(remaining: int, robots: int, stations: list[Context], cycle: float) -> float:
    """잔량 예상 소요시간 (로봇 처리율과 스테이션 처리율 중 병목 기준).

    Args:
        remaining: 남은 수량
        robots: 투입 로봇 수
        stations: 사용 스테이션 dict 목록 (avg_process_time 포함)
        cycle: 작업 1건 평균 사이클 (sim-sec)

    Returns:
        예상 소요시간 (sim-sec)
    """
    if remaining <= 0:
        return 0.0
    if robots <= 0 or not stations:
        return math.inf
    robot_rate = robots / max(1e-6, cycle)
    avg_process = sum(float(s.get("avg_process_time") or config.DEFAULT_PROCESS_TIME) for s in stations) / len(stations)
    station_rate = len(stations) / max(0.1, avg_process)
    return remaining / min(robot_rate, station_rate)


class MockAgent(BaseAgent):
    """규칙 기반 관제 에이전트."""

    name = "mock"

    def __init__(self) -> None:
        """명령 규칙 테이블을 구성한다. (순서가 우선순위)"""
        self._rules: list[tuple[Callable[[str], bool], Callable[[str, Context], Awaitable[AgentResponse]]]] = [
            (lambda m: _has(m, "도움", "help", "명령어", "뭐 할 수"), self._cmd_help),
            (lambda m: "주문" in m and _has(m, "긴급", "넣어", "추가", "등록") and bool(QTY_RE.search(m)),
             self._cmd_create_order),
            (lambda m: bool(ORDER_ID_RE.search(m)) and "취소" in m, self._cmd_cancel_order),
            (lambda m: "납기" in m and _has(m, "위험", "임박", "초과"), self._cmd_deadline_risk),
            (lambda m: "주문" in m and _has(m, "현황", "보여", "상태", "목록", "진행"), self._cmd_order_status),
            (lambda m: "병목" in m, self._cmd_bottleneck),
            (lambda m: "충전소" in m and _has(m, "추가", "설치", "더"), self._cmd_add_charger),
            (lambda m: "로봇" in m and bool(COUNT_RE.search(m)) and _has(m, "추가", "투입", "더", "늘려"),
             self._cmd_add_robots),
            (lambda m: bool(STATION_ID_RE.search(m)) and _has(m, "옮겨", "이동", "위치") and bool(POINT_RE.search(m)),
             self._cmd_move_station),
            (lambda m: bool(STATION_ID_RE.search(m)) and _has(m, "고장", "정지", "비활성", "멈춰"),
             self._cmd_station_fault),
            (lambda m: bool(STATION_ID_RE.search(m)) and _has(m, "복구", "재가동", "수리", "활성"),
             self._cmd_station_restore),
            (lambda m: _has(m, "문", "벽") and len(POINT_RE.findall(m)) >= 2, self._cmd_move_obstacle),
            (lambda m: bool(ROBOT_ID_RE.search(m)) and "충전" in m, self._cmd_force_charge),
            (lambda m: "배속" in m or bool(SPEED_RE.search(m)) and "배" in m, self._cmd_speed),
            (lambda m: _has(m, "빈도", "생성률", "작업량") and _has(m, "높", "늘", "증가", "올려", "낮", "줄", "감소",
                                                               "내려"), self._cmd_task_rate),
            (lambda m: _has(m, "최적화", "baseline", "베이스라인", "기존 방식", "라운드로빈") and _has(m, "모드", "전환", "바꿔"),
             self._cmd_mode),
            (lambda m: "최소충전" in m or "최소 충전" in m, self._cmd_min_charge),
            (lambda m: _has(m, "효율", "kpi", "KPI", "성능", "분석"), self._cmd_efficiency),
            (lambda m: _has(m, "상태", "현황", "요약", "어때"), self._cmd_status),
        ]

    # ------------------------------------------------------------------
    # BaseAgent
    # ------------------------------------------------------------------
    async def process_message(self, user_msg: str, sim_context: Context) -> AgentResponse:
        """사용자 메시지를 규칙 테이블로 해석한다.

        Args:
            user_msg: 사용자 입력
            sim_context: 시뮬레이션 상태

        Returns:
            AgentResponse
        """
        message = user_msg.strip()
        for matches, handler in self._rules:
            if matches(message):
                return await handler(message, sim_context)
        return AgentResponse(f"명령을 이해하지 못했습니다: \"{message}\"\n\n{HELP_TEXT}")

    async def handle_production_event(self, event: ProductionEvent, sim_context: Context) -> AgentResponse:
        """생산 이벤트 유형별 자율 대응.

        Args:
            event: 생산 이벤트
            sim_context: 시뮬레이션 상태

        Returns:
            AgentResponse (source="event")
        """
        handlers: dict[str, Callable[[ProductionEvent, Context], AgentResponse]] = {
            ProductionEventType.CRITICAL_ORDER: self._on_critical_order,
            ProductionEventType.DEADLINE_RISK: self._on_deadline_risk,
            ProductionEventType.DEMAND_SURGE: self._on_demand_surge,
            ProductionEventType.ORDER_CANCELLED: self._on_order_released,
            ProductionEventType.ORDER_REDUCED: self._on_order_released,
            ProductionEventType.EQUIPMENT_FAILURE: self._on_equipment_failure,
            ProductionEventType.EQUIPMENT_RESTORED: self._on_equipment_restored,
            ProductionEventType.ORDER_COMPLETED: self._on_order_completed,
        }
        handler = handlers.get(event.event_type)
        if handler is None:
            response = AgentResponse(f"알 수 없는 생산 이벤트: {event.event_type} {event.payload}")
        else:
            response = handler(event, sim_context)
        response.source = "event"
        return response

    async def analyze_bottleneck(self, sim_context: Context) -> BottleneckReport:
        """스테이션 가동률·대기열과 충전 대기를 점수화하여 병목을 찾는다.

        Args:
            sim_context: 시뮬레이션 상태

        Returns:
            BottleneckReport
        """
        stations = [s for s in sim_context.get("stations", []) if s.get("active")]
        robots = sim_context.get("robots", [])
        if not stations:
            return BottleneckReport(None, 0.0, "가동 중인 스테이션이 없습니다.")
        n_robots = max(1, len(robots))

        def score(station: Context) -> float:
            backlog = station["queue_length"] + station["pending_tasks"]
            return station["utilization"] / 100.0 + backlog / n_robots

        ranked = sorted(stations, key=score, reverse=True)
        top = ranked[0]
        details = [
            f"{s['id']}({s['label']}) 가동률 {s['utilization']:.0f}% · 대기 로봇 {s['queue_length']} · "
            f"미배정 작업 {s['pending_tasks']} · 점수 {score(s):.2f}"
            for s in ranked[:3]
        ]
        charge_queue = sum(c.get("queue_length", 0) for c in sim_context.get("chargers", []))
        if charge_queue:
            details.append(f"충전 대기 로봇 {charge_queue}대 — 충전소 증설 또는 선제 충전 검토")
        states = Counter(r["state"] for r in robots)
        idle_ratio = states.get(RobotState.IDLE, 0) / n_robots
        if score(top) >= 1.0:
            if idle_ratio > 0.4:
                advice = "로봇은 여유가 있으므로 병목 스테이션 증설/처리시간 단축이 효과적입니다."
            else:
                advice = "로봇이 부족합니다. 로봇 추가 투입을 권장합니다."
        else:
            advice = "뚜렷한 병목은 없습니다."
        summary = f"최대 병목: {top['id']}({top['label']}) — {advice}"
        return BottleneckReport(top["id"], score(top), summary, details)

    # ------------------------------------------------------------------
    # 조회형 명령
    # ------------------------------------------------------------------
    async def _cmd_help(self, message: str, ctx: Context) -> AgentResponse:
        """도움말."""
        return AgentResponse(HELP_TEXT)

    async def _cmd_status(self, message: str, ctx: Context) -> AgentResponse:
        """현재 상태 요약."""
        if not ctx:
            return AgentResponse("아직 시뮬레이션 상태를 받지 못했습니다. [▶ 시작]을 눌러주세요.")
        robots = ctx["robots"]
        states = Counter(r["state"] for r in robots)
        state_text = ", ".join(f"{RobotState.LABELS.get(s, s)} {n}" for s, n in states.most_common())
        avg_battery = sum(r["battery_pct"] for r in robots) / len(robots) if robots else 0.0
        low = [r["id"] for r in robots if r["battery_pct"] < LOW_BATTERY_WARN]
        active_orders = [o for o in ctx["orders"] if o["status"] in ("pending", "in_progress")]
        kpis = ctx["kpis"]
        lines = [
            f"📊 현재 상태 ({ctx['factory_name']})",
            f"├ 시뮬레이션: {'실행 중' if ctx['running'] else '일시정지'} · T+{fmt_duration(ctx['sim_time'])} · "
            f"{ctx['speed']}x · {SchedulerModeLabel.LABELS.get(ctx['mode'], ctx['mode'])} 모드",
            f"├ 로봇 {len(robots)}대: {state_text or '없음'}",
            f"├ 평균 배터리 {avg_battery:.0f}%" + (f" (저전력: {', '.join(low)})" if low else ""),
            f"├ 작업 대기열 {ctx['pending_tasks']}건 · 처리량 {fmt_value(kpis['throughput'])}건/분",
            f"└ 활성 주문 {len(active_orders)}건 · 작업 생성 λ={ctx['params'].get('task_rate', 0):.3f}/s",
        ]
        return AgentResponse("\n".join(lines))

    async def _cmd_efficiency(self, message: str, ctx: Context) -> AgentResponse:
        """KPI 요약 (표시 모드 vs 비교 모드)."""
        if not ctx:
            return AgentResponse("KPI를 계산할 데이터가 아직 없습니다.")
        mode = ctx["mode"]
        other_mode = next((m for m in ctx["engine_kpis"] if m != mode), None)
        other = ctx["engine_kpis"].get(other_mode, {}) if other_mode else {}
        label = SchedulerModeLabel.LABELS
        lines = [f"📈 효율 분석 — {label.get(mode, mode)} vs {label.get(other_mode, other_mode)}"]
        wins = 0
        for index, kpi in enumerate(KPI_DEFINITIONS):
            value = ctx["kpis"].get(kpi.key, math.nan)
            base = other.get(kpi.key, math.nan)
            prefix = "└" if index == len(KPI_DEFINITIONS) - 1 else "├"
            delta = ""
            if not (math.isnan(value) or math.isnan(base)) and abs(base) > 1e-9:
                change = (value - base) / abs(base) * 100.0
                better = change > 0 if kpi.higher_is_better else change < 0
                wins += int(better)
                delta = f" ({'▲' if change > 0 else '▼'}{abs(change):.0f}% {'개선' if better else '악화'})"
            lines.append(f"{prefix} {kpi.label}: {fmt_value(value, kpi.decimals)}{kpi.unit} "
                         f"vs {fmt_value(base, kpi.decimals)}{kpi.unit}{delta}")
        lines.append(f"\n→ 비교 가능한 지표 중 {wins}개 개선. 처리량·대기시간·주문 달성률을 우선 확인하세요.")
        return AgentResponse("\n".join(lines))

    async def _cmd_bottleneck(self, message: str, ctx: Context) -> AgentResponse:
        """병목 분석."""
        report = await self.analyze_bottleneck(ctx)
        body = "\n".join(f"├ {d}" for d in report.details)
        return AgentResponse(f"🔍 병목 분석\n{body}\n└ {report.summary}")

    async def _cmd_order_status(self, message: str, ctx: Context) -> AgentResponse:
        """주문 현황."""
        orders = ctx.get("orders", [])
        if not orders:
            return AgentResponse("등록된 주문이 없습니다. [📋 주문 관리]에서 추가하거나 '긴급 주문 넣어줘: 제품A 100개, 1시간 내'라고 입력하세요.")
        lines = ["📋 주문 현황"]
        for order in orders:
            pct = order["completed"] / max(1, order["quantity"]) * 100.0
            deadline = order["deadline_remaining"]
            deadline_text = f"납기 {fmt_duration(deadline)} 남음" if deadline is not None else "납기 없음"
            if order["status"] in ("pending", "in_progress"):
                risk = " ⚠ 위험" if order["at_risk"] else ""
                lines.append(f"├ {order['order_id']} [{order['priority']}] {order['product_type']} "
                             f"{order['completed']}/{order['quantity']} ({pct:.0f}%) · "
                             f"예상 {fmt_duration(order['eta_seconds'])}"
                             f" · {deadline_text}{risk}")
            else:
                lines.append(f"├ {order['order_id']} {order['product_type']} — {order['status']}")
        lines[-1] = "└" + lines[-1][1:]
        return AgentResponse("\n".join(lines))

    async def _cmd_deadline_risk(self, message: str, ctx: Context) -> AgentResponse:
        """납기 위험 주문 필터링."""
        risky = [o for o in ctx.get("orders", []) if o.get("at_risk")]
        if not risky:
            return AgentResponse("✅ 현재 납기 위험 주문은 없습니다.")
        lines = [f"⚠ 납기 위험 주문 {len(risky)}건"]
        for order in risky:
            remaining = order["quantity"] - order["completed"]
            lines.append(f"├ {order['order_id']} {order['product_type']} 잔량 {remaining}개 · "
                         f"예상 {fmt_duration(order['eta_seconds'])} > 남은 {fmt_duration(order['deadline_remaining'])}")
        lines.append("└ '긴급 주문' 대응처럼 로봇 재배치가 필요하면 우선순위를 critical로 올리거나 로봇을 추가하세요.")
        return AgentResponse("\n".join(lines), severity=Severity.WARNING)

    # ------------------------------------------------------------------
    # 제어형 명령
    # ------------------------------------------------------------------
    async def _cmd_create_order(self, message: str, ctx: Context) -> AgentResponse:
        """주문 등록 (긴급 여부 판별)."""
        qty_match = QTY_RE.search(message)
        quantity = int(qty_match.group(1)) if qty_match else 0
        if quantity <= 0:
            return AgentResponse("수량을 인식하지 못했습니다. 예: '긴급 주문 넣어줘: 제품A 300개, 2시간 내'")
        product_match = PRODUCT_RE.search(message)
        product = f"제품{product_match.group(1)}" if product_match else "제품X"
        deadline = 0.0
        if hours := HOURS_RE.search(message):
            deadline += float(hours.group(1)) * 3600.0
        if minutes := MINUTES_RE.search(message):
            deadline += float(minutes.group(1)) * 60.0
        priority = "critical" if "긴급" in message else "high" if _has(message, "높음", "우선") else "normal"
        stations = [normalize_station_id(n) for n in STATION_ID_RE.findall(message)]
        params = {
            "product_type": product,
            "quantity": quantity,
            "priority": priority,
            "deadline_seconds": deadline or None,
            "required_stations": stations,
        }
        tail = "AI 긴급 대응을 곧 시작합니다." if priority == "critical" else "시뮬레이션에 반영됩니다."
        deadline_text = fmt_duration(deadline) + " 내" if deadline else "납기 없음"
        return AgentResponse(
            f"📝 {priority} 주문 등록 요청: {product} {quantity}개 · {deadline_text}"
            + (f" · 스테이션 {', '.join(stations)}" if stations else "") + f"\n{tail}",
            [AgentAction(ActionKind.CREATE_ORDER, params)],
        )

    async def _cmd_cancel_order(self, message: str, ctx: Context) -> AgentResponse:
        """주문 취소."""
        match = ORDER_ID_RE.search(message)
        assert match is not None
        order_id = match.group(0).upper()
        return AgentResponse(f"🗑 {order_id} 주문 취소를 요청합니다.",
                             [AgentAction(ActionKind.CANCEL_ORDER, {"order_id": order_id})])

    async def _cmd_add_robots(self, message: str, ctx: Context) -> AgentResponse:
        """로봇 추가."""
        count_match = COUNT_RE.search(message)
        count = min(20, int(count_match.group(1))) if count_match else 1
        return AgentResponse(f"🤖 로봇 {count}대를 DB에 추가합니다. 저장 즉시 시뮬레이션에 투입됩니다.",
                             [AgentAction(ActionKind.ADD_ROBOTS, {"count": count})])

    async def _cmd_force_charge(self, message: str, ctx: Context) -> AgentResponse:
        """특정 로봇 강제 충전."""
        match = ROBOT_ID_RE.search(message)
        assert match is not None
        robot_id = normalize_robot_id(match.group(1))
        known = {r["id"] for r in ctx.get("robots", [])}
        if known and robot_id not in known:
            return AgentResponse(f"{robot_id} 로봇을 찾을 수 없습니다. (가동 중: {', '.join(sorted(known))})")
        return AgentResponse(f"🔋 {robot_id} 강제 충전을 지시합니다.",
                             [AgentAction(ActionKind.FORCE_CHARGE, {"robot_id": robot_id})])

    async def _cmd_speed(self, message: str, ctx: Context) -> AgentResponse:
        """배속 변경."""
        match = SPEED_RE.search(message) or re.search(r"(\d+)", message)
        if match is None:
            return AgentResponse(f"배속 값을 인식하지 못했습니다. 가능: {', '.join(f'{s}x' for s in config.SPEED_OPTIONS)}")
        speed = min(config.SPEED_OPTIONS, key=lambda s: abs(s - int(match.group(1))))
        return AgentResponse(f"⏩ 배속을 {speed}x로 변경합니다.", [AgentAction(ActionKind.SET_SPEED, {"speed": speed})])

    async def _cmd_task_rate(self, message: str, ctx: Context) -> AgentResponse:
        """작업 생성 빈도 조절."""
        up = _has(message, "높", "늘", "증가", "올려")
        factor = TASK_RATE_UP if up else TASK_RATE_DOWN
        current = float(ctx.get("params", {}).get("task_rate", config.DEFAULT_TASK_RATE))
        return AgentResponse(
            f"📦 작업 생성 빈도(포아송 λ)를 {'높입니다' if up else '낮춥니다'}: {current:.3f} → {current * factor:.3f} /s",
            [AgentAction(ActionKind.SCALE_TASK_RATE, {"factor": factor})],
        )

    async def _cmd_mode(self, message: str, ctx: Context) -> AgentResponse:
        """스케줄러 모드 전환."""
        mode = "baseline" if _has(message, "baseline", "베이스라인", "기존 방식", "라운드로빈") else "optimized"
        label = SchedulerModeLabel.LABELS[mode]
        return AgentResponse(f"🔀 스케줄링 모드를 {label}로 전환합니다. (두 엔진은 계속 병렬 실행되어 KPI 비교가 유지됩니다)",
                             [AgentAction(ActionKind.SET_MODE, {"mode": mode})])

    async def _cmd_min_charge(self, message: str, ctx: Context) -> AgentResponse:
        """최소충전 복귀 모드 토글."""
        enabled = not _has(message, "꺼", "해제", "off", "OFF", "끄")
        return AgentResponse(f"🔋 최소충전 복귀 모드 {'ON' if enabled else 'OFF'} "
                             f"(충전 목표 {config.MIN_CHARGE_TARGET:.0f}%)",
                             [AgentAction(ActionKind.SET_MIN_CHARGE_MODE, {"enabled": enabled})])

    async def _cmd_station_fault(self, message: str, ctx: Context) -> AgentResponse:
        """스테이션 고장 처리."""
        station_id = normalize_station_id(STATION_ID_RE.search(message).group(1))  # type: ignore[union-attr]
        station = _find(ctx.get("stations", []), station_id)
        if ctx and station is None:
            return AgentResponse(f"{station_id} 스테이션을 찾을 수 없습니다.")
        lines = [f"⚫ {station_id} 고장 처리 (비활성화) → DB 반영"]
        if station is not None:
            alternatives = [s["id"] for s in ctx["stations"]
                            if s["id"] != station_id and s["active"] and s["task_type"] == station["task_type"]]
            affected = station["queue_length"] + station["pending_tasks"] + int(station["busy"])
            lines.append(f"├ 진행 중/대기 작업 {affected}건 재배정 예정")
            lines.append(f"└ 동일 공정 대체 스테이션: {', '.join(alternatives) if alternatives else '없음 → 가용 스테이션으로 분산'}")
        return AgentResponse("\n".join(lines),
                             [AgentAction(ActionKind.SET_STATION_ACTIVE, {"station_id": station_id, "active": False})],
                             severity=Severity.WARNING)

    async def _cmd_station_restore(self, message: str, ctx: Context) -> AgentResponse:
        """스테이션 복구."""
        station_id = normalize_station_id(STATION_ID_RE.search(message).group(1))  # type: ignore[union-attr]
        return AgentResponse(f"🔧 {station_id} 재가동을 요청합니다.",
                             [AgentAction(ActionKind.SET_STATION_ACTIVE, {"station_id": station_id, "active": True})])

    async def _cmd_move_station(self, message: str, ctx: Context) -> AgentResponse:
        """스테이션 이동."""
        station_id = normalize_station_id(STATION_ID_RE.search(message).group(1))  # type: ignore[union-attr]
        x, y = (float(v) for v in POINT_RE.search(message).groups())  # type: ignore[union-attr]
        return AgentResponse(f"📐 {station_id} 위치를 ({x:.0f}, {y:.0f})로 옮기고 경로를 재계산합니다.",
                             [AgentAction(ActionKind.MOVE_STATION, {"station_id": station_id, "x": x, "y": y})])

    async def _cmd_add_charger(self, message: str, ctx: Context) -> AgentResponse:
        """충전소 추가."""
        point = POINT_RE.search(message)
        if point is None:
            floor = ctx.get("floor", {"width": config.DEFAULT_FLOOR_WIDTH, "height": config.DEFAULT_FLOOR_HEIGHT})
            x, y = floor["width"] / 2.0, floor["height"] / 2.0
        else:
            x, y = (float(v) for v in point.groups())
        slots = re.search(r"(\d+)\s*(?:슬롯|구|칸)", message)
        capacity = int(slots.group(1)) if slots else config.DEFAULT_CHARGER_CAPACITY
        return AgentResponse(f"⚡ 충전소({capacity}슬롯)를 ({x:.0f}, {y:.0f})에 추가합니다.",
                             [AgentAction(ActionKind.ADD_CHARGER, {"x": x, "y": y, "capacity": capacity})])

    async def _cmd_move_obstacle(self, message: str, ctx: Context) -> AgentResponse:
        """문/벽 이동."""
        points = POINT_RE.findall(message)[:2]
        coords = tuple(float(v) for point in points for v in point)
        obstacle_type = "door" if "문" in message else "wall"
        name = "문" if obstacle_type == "door" else "벽"
        return AgentResponse(
            f"🚪 {name} 위치를 ({coords[0]:.0f}, {coords[1]:.0f})-({coords[2]:.0f}, {coords[3]:.0f})로 옮깁니다. "
            "경로 그래프를 다시 계산합니다.",
            [AgentAction(ActionKind.MOVE_OBSTACLE, {"obstacle_type": obstacle_type, "coords": coords})],
        )

    # ------------------------------------------------------------------
    # 생산 이벤트 대응
    # ------------------------------------------------------------------
    def _on_critical_order(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """🔴 긴급 주문: 유휴 로봇 재배치 + 충전 로봇 복귀 + 저우선순위 선점 + 전용 스테이션."""
        payload = event.payload
        order_id = event.order_id or payload.get("order_id", "?")
        order = _find(ctx.get("orders", []), order_id, key="order_id")
        product = payload.get("product_type", order["product_type"] if order else "?")
        quantity = int(payload.get("quantity", order["quantity"] if order else 0))
        remaining = order["quantity"] - order["completed"] if order else quantity - int(payload.get("completed", 0))
        deadline_left = _deadline_left(payload, ctx, order)
        cycle = float(ctx.get("avg_task_cycle") or config.DEFAULT_TASK_CYCLE)
        robots = ctx.get("robots", [])
        stations = self._pick_stations(payload.get("required_stations") or [], ctx)

        idle = [r for r in robots if r["state"] == RobotState.IDLE and r["battery_pct"] > MIN_IDLE_BATTERY
                and not r.get("emergency_order")]
        charging = sorted(
            (r for r in robots if r["state"] in (RobotState.CHARGING, RobotState.CHARGE_WAIT)
             and r["battery_pct"] >= config.RECALL_MIN_BATTERY),
            key=lambda r: -r["battery_pct"],
        )
        low_priority = sorted(
            (r for r in robots if r["state"] in (RobotState.MOVING, RobotState.WAITING)
             and r.get("task_priority") is not None and r["task_priority"] >= config.PRIORITY_LEVELS["normal"]
             and r["battery_pct"] > config.RECALL_MIN_BATTERY),
            key=lambda r: -r["battery_pct"],
        )

        if deadline_left is not None and deadline_left > 0:
            needed = math.ceil(remaining * cycle / (deadline_left * DEADLINE_SAFETY))
        else:
            needed = math.ceil(len(robots) * DEFAULT_EMERGENCY_SHARE)
        needed = max(1, min(len(robots), needed)) if robots else 0

        assign = [r["id"] for r in idle[:needed]]
        recall = [r["id"] for r in charging[:max(0, needed - len(assign))]]
        preempt = [r["id"] for r in low_priority[:max(0, needed - len(assign) - len(recall))]]
        eta_before = estimate_eta(remaining, len(idle), stations, cycle)
        # 지정하지 않은 유휴 로봇도 critical 작업을 최우선으로 받으므로 함께 계산한다.
        eta_after = estimate_eta(remaining, len(idle) + len(recall) + len(preempt), stations, cycle)
        at_risk = deadline_left is not None and eta_before > deadline_left

        lines = [
            f"🚨 긴급 주문 감지: {order_id}",
            f"├ 제품: {product}, 수량: {quantity}개, 납기: {fmt_duration(deadline_left) if deadline_left else '미지정'}",
            f"├ 현재 가용 로봇: {', '.join(r['id'] for r in idle) or '없음'} ({len(idle)}대)",
            f"├ 예상 소요시간: {fmt_duration(eta_before)} (로봇 {len(idle)}대 기준)",
        ]
        lines.append("⚠ 납기 초과 위험!" if at_risk else "✅ 현재 인력으로도 납기 대응 가능 — 우선 배정만 수행")
        lines.append("\n[자동 조치 실행 중]")
        for robot_id in assign:
            lines.append(f"✅ {robot_id}(유휴) → {product} 라인 즉시 투입")
        for robot_id in recall:
            battery = _find(robots, robot_id)["battery_pct"]  # type: ignore[index]
            lines.append(f"✅ {robot_id}(충전 중 {battery:.0f}%) 긴급 복귀 → {product} 라인 투입")
        for robot_id in preempt:
            lines.append(f"✅ {robot_id}(저우선순위 작업) 선점 → {product} 라인 전환")
        station_ids = [s["id"] for s in stations]
        if station_ids:
            lines.append(f"✅ {', '.join(station_ids)} 우선순위 → {product} 전용 전환")
        if at_risk:
            lines.append(f"✅ 최소충전 복귀 모드 ON (충전 목표 {config.MIN_CHARGE_TARGET:.0f}%)")
        verdict = "✅ 납기 충족 가능" if deadline_left is None or eta_after <= deadline_left else "⚠ 추가 로봇 투입 필요"
        lines.append(f"\n조치 후 예상 소요시간: {fmt_duration(eta_after)} {verdict}")

        plan = {
            "order_id": order_id,
            "assign": assign,
            "recall": recall,
            "preempt": preempt,
            "dedicate": station_ids,
        }
        if at_risk:
            plan["min_charge_mode"] = True
        return AgentResponse("\n".join(lines), [AgentAction(ActionKind.EMERGENCY_PLAN, plan)],
                             severity=Severity.CRITICAL, title=f"🔴 긴급 주문 {order_id} — {product} {quantity}개")

    def _on_deadline_risk(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """🟠 납기 임박: 로봇 추가 투입 + 우선순위 상향 + 스테이션 1곳 전용."""
        payload = event.payload
        order_id = event.order_id or payload.get("order_id", "?")
        order = _find(ctx.get("orders", []), order_id, key="order_id")
        robots = ctx.get("robots", [])
        idle = [r["id"] for r in robots if r["state"] == RobotState.IDLE and r["battery_pct"] > MIN_IDLE_BATTERY]
        charging = [r["id"] for r in robots if r["state"] in (RobotState.CHARGING, RobotState.CHARGE_WAIT)
                    and r["battery_pct"] >= config.RECALL_MIN_BATTERY]
        stations = self._pick_stations(payload.get("required_stations") or [], ctx)[:1]
        priority = payload.get("priority", "normal")
        boost = "high" if config.PRIORITY_LEVELS.get(priority, 2) > config.PRIORITY_LEVELS["high"] else None
        plan = {
            "order_id": order_id,
            "assign": idle[:3],
            "recall": charging[:2],
            "dedicate": [s["id"] for s in stations],
            "boost_priority": boost,
        }
        left = order["deadline_remaining"] if order else payload.get("deadline_left")
        eta = order["eta_seconds"] if order else payload.get("eta_seconds")
        lines = [
            f"🟠 납기 임박: {order_id} ({payload.get('product_type', '?')})",
            f"├ 잔량 {payload.get('remaining', '?')}개 · 남은 시간 {fmt_duration(left)} · 예상 소요 {fmt_duration(eta)}",
            "[자동 조치]",
        ]
        if plan["assign"]:
            lines.append(f"✅ 유휴 로봇 추가 투입: {', '.join(plan['assign'])}")
        if plan["recall"]:
            lines.append(f"✅ 충전 로봇 복귀: {', '.join(plan['recall'])}")
        if boost:
            lines.append(f"✅ 주문 우선순위 {priority} → {boost}")
        if plan["dedicate"]:
            lines.append(f"✅ {plan['dedicate'][0]} 스테이션 우선 배정")
        if len(lines) == 3:
            lines.append("⚠ 추가 투입 가능한 로봇이 없습니다 — '로봇 2대 추가해줘'로 증설을 검토하세요.")
        return AgentResponse("\n".join(lines), [AgentAction(ActionKind.EMERGENCY_PLAN, plan)],
                             severity=Severity.WARNING, title=f"🟠 납기 임박 {order_id}")

    def _on_demand_surge(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """🟡 생산량 급증: 유휴 로봇 전수 가동 + 최소충전 복귀."""
        payload = event.payload
        robots = ctx.get("robots", [])
        charging = [r["id"] for r in robots if r["state"] in (RobotState.CHARGING, RobotState.CHARGE_WAIT)
                    and r["battery_pct"] >= config.RECALL_MIN_BATTERY]
        idle = [r["id"] for r in robots if r["state"] == RobotState.IDLE]
        total = int(payload.get("total_units", 0))
        per_robot = config.SURGE_UNITS_PER_ROBOT
        suggested = max(0, math.ceil(total / per_robot) - len(robots))
        lines = [
            "🟡 생산량 급증 감지",
            f"├ 활성 주문 잔량 {total}개 > 임계치 {payload.get('threshold', '?')}개 (로봇 {len(robots)}대)",
            "[자동 조치]",
            f"✅ 유휴 로봇 전수 가동: {', '.join(idle) if idle else '유휴 로봇 없음 (전원 가동 중)'}",
            f"✅ 최소충전 복귀 모드 ON (충전 목표 {config.MIN_CHARGE_TARGET:.0f}%)",
        ]
        if charging:
            lines.append(f"✅ 충전 중 로봇 복귀: {', '.join(charging)}")
        if suggested:
            lines.append(f"💡 권장: 로봇 {suggested}대 추가 투입 ('로봇 {suggested}대 추가해줘')")
        actions = [AgentAction(ActionKind.SET_MIN_CHARGE_MODE, {"enabled": True})]
        if charging:
            actions.append(AgentAction(ActionKind.RECALL_ROBOTS, {"robot_ids": charging}))
        return AgentResponse("\n".join(lines), actions, severity=Severity.WARNING, title="🟡 생산량 급증")

    def _on_order_released(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """🔵 주문 취소/감소: 불필요 로봇 대기 + 배터리 선충전."""
        payload = event.payload
        order_id = event.order_id or payload.get("order_id", "?")
        if event.event_type == ProductionEventType.ORDER_CANCELLED:
            head = f"🔵 주문 취소: {order_id} (잔량 {payload.get('released_units', '?')}개 해제)"
        else:
            head = f"🔵 주문 감소: {order_id} ({payload.get('old_quantity')} → {payload.get('new_quantity')}개)"
        actions = [AgentAction(ActionKind.PRECHARGE)]
        lines = [head, "[자동 조치]", "✅ 여유 로봇 대기 전환", f"✅ 잔량 {config.PRECHARGE_LEVEL:.0f}% 미만 유휴 로봇 선충전"]
        if not _critical_active(ctx, exclude=order_id):
            actions.append(AgentAction(ActionKind.SET_MIN_CHARGE_MODE, {"enabled": False}))
            lines.append("✅ 최소충전 복귀 모드 해제")
        return AgentResponse("\n".join(lines), actions)

    def _on_equipment_failure(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """⚫ 설비 고장: 대체 스테이션 재배정 결과 + 납기 재계산 보고."""
        payload = event.payload
        station_id = payload.get("station_id", "?")
        alternatives = payload.get("alternatives") or []
        affected = payload.get("affected_orders") or []
        lines = [
            f"⚫ 설비 고장: {station_id} ({payload.get('label', '')}, {payload.get('task_type', '')})",
            "[자동 조치]",
            f"✅ 대기·진행 작업 → {', '.join(alternatives) if alternatives else '가용 스테이션'} 재배정",
        ]
        orders = {o["order_id"]: o for o in ctx.get("orders", [])}
        for order_id in affected:
            order = orders.get(order_id)
            if order is None:
                continue
            left = order["deadline_remaining"]
            verdict = ""
            if left is not None:
                verdict = " ⚠ 납기 위험" if order["eta_seconds"] > left else " ✅ 납기 충족"
            lines.append(f"├ 납기 재계산 {order_id}: 예상 {fmt_duration(order['eta_seconds'])}{verdict}")
        if not alternatives:
            lines.append("⚠ 동일 공정 대체 설비가 없습니다 — 복구 전까지 처리량 저하가 예상됩니다.")
        return AgentResponse("\n".join(lines), severity=Severity.WARNING, title=f"⚫ 설비 고장 {station_id}")

    def _on_equipment_restored(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """✅ 설비 복구."""
        payload = event.payload
        return AgentResponse(f"✅ 설비 복구: {payload.get('station_id', '?')} ({payload.get('label', '')}) — 작업 배정 재개")

    def _on_order_completed(self, event: ProductionEvent, ctx: Context) -> AgentResponse:
        """✅ 주문 완료."""
        payload = event.payload
        order_id = event.order_id or payload.get("order_id", "?")
        on_time = payload.get("on_time", True)
        text = (f"✅ 주문 완료: {order_id} ({payload.get('product_type', '')} {payload.get('quantity', '')}개) — "
                + ("납기 내 완료" if on_time else "⚠ 납기 초과 완료"))
        actions = []
        if payload.get("priority") == "critical" and not _critical_active(ctx, exclude=order_id):
            actions.append(AgentAction(ActionKind.SET_MIN_CHARGE_MODE, {"enabled": False}))
            text += "\n긴급 대응 종료 — 최소충전 복귀 모드 해제, 전용 스테이션 반환"
        return AgentResponse(text, actions)

    @staticmethod
    def _pick_stations(required: list[str], ctx: Context) -> list[Context]:
        """전용 전환할 스테이션: 필수 스테이션 중 가동 중인 것, 없으면 부하가 가장 낮은 스테이션."""
        stations = [s for s in ctx.get("stations", []) if s.get("active")]
        chosen = [s for s in stations if s["id"] in required]
        if not chosen:
            chosen = sorted(stations, key=lambda s: s["queue_length"] + s["pending_tasks"] + s["utilization"] / 100.0)
        return chosen[:MAX_DEDICATED_STATIONS]


# ----------------------------------------------------------------------
# 모듈 보조 함수
# ----------------------------------------------------------------------
def _has(message: str, *keywords: str) -> bool:
    """키워드 중 하나라도 포함하는지."""
    return any(k in message for k in keywords)


def _find(items: list[Context], item_id: str, key: str = "id") -> Context | None:
    """id로 dict 항목 찾기."""
    return next((item for item in items if item.get(key) == item_id), None)


def _critical_active(ctx: Context, exclude: str | None = None) -> bool:
    """진행 중인 critical 주문이 남아 있는지."""
    return any(
        o["priority"] == "critical" and o["status"] in ("pending", "in_progress") and o["order_id"] != exclude
        for o in ctx.get("orders", [])
    )


def _deadline_left(payload: Context, ctx: Context, order: Context | None) -> float | None:
    """납기까지 남은 sim-sec (시뮬레이션 시계 기준)."""
    if order is not None and order.get("deadline_remaining") is not None:
        return float(order["deadline_remaining"])
    if payload.get("deadline_left") is not None:
        return float(payload["deadline_left"])
    if payload.get("deadline") and ctx.get("sim_datetime"):
        try:
            deadline = datetime.fromisoformat(str(payload["deadline"]))
            now = datetime.fromisoformat(str(ctx["sim_datetime"]))
            return (deadline - now).total_seconds()
        except ValueError:
            pass
    if payload.get("deadline_seconds") is not None:
        return float(payload["deadline_seconds"])
    return None


if __name__ == "__main__":
    import asyncio

    agent = MockAgent()
    for text in ("도움말", "긴급 주문 넣어줘: 제품A 300개, 2시간 내", "로봇 3대 추가해줘", "R-02 충전해", "배속 5배로",
                 "WS-03 고장 처리해", "충전소 하나 더 추가해 위치 (400, 300)", "문 위치를 (500, 0)-(500, 100)으로 옮겨",
                 "작업 생성 빈도 높여줘", "스케줄링 최적화 모드로 전환", "현재 상태 알려줘"):
        reply = asyncio.run(agent.process_message(text, {}))
        print(f"> {text}\n{reply.text}\n  actions={reply.actions}\n")
