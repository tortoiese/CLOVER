"""Rolling Horizon 유휴 로봇 배정 MILP v1 (전체 공정/충전 일정의 최적성은 보장하지 않음)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

import config
from simulation.scheduler import OptimizedScheduler

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


class RollingMilpScheduler(OptimizedScheduler):
    """현재 유휴 로봇·대기 작업·사용 가능한 설비의 이진 배정을 반복 계산한다.

    배정 수를 우선 최대화한 후 우선순위·이동·에너지 비용을 줄인다.
    충전 시점/충전소 선택은 기존 예측 충전 규칙을 사용한다.
    """

    name = "rolling_milp"

    def __init__(self, horizon: float = 60.0, replan: float = 5.0) -> None:
        """계획 구간과 재계획 주기를 설정한다."""
        super().__init__()
        self.horizon = horizon
        self.replan = replan
        self._next_plan = 0.0
        self.solve_records: list[dict[str, float | int | None]] = []

    def dispatch(self, engine: SimulationEngine) -> None:
        """진행 중 작업은 유지하고 재계획 시점에 유휴 배정만 갱신한다."""
        if engine.now < self._next_plan:
            return
        self._next_plan = engine.now + self.replan
        self._dispatch(engine, [r for r in engine.robot_list() if r.is_available()])

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        tasks = sorted(engine.pending_tasks, key=self._task_key(engine))[:40]
        entries = []
        stations = []
        for task in tasks:
            for robot in idle:
                if robot.emergency_order and task.order_id != robot.emergency_order:
                    continue
                for station_id in task.allowed_stations or (task.station_id,):
                    if not engine.station_available(station_id):
                        continue
                    station = engine.stations[station_id]
                    if (station.resource.count or station.waiting
                            or station.dedicated_order not in (None, task.order_id)):
                        continue
                    if any(r.target_id == station_id for r in engine.robot_list() if not r.is_available()):
                        continue
                    distance = engine.path_graph.distance_to_facility(
                        robot.x, robot.y, station_id, robot.floor, robot.cfg.width)
                    if not math.isfinite(distance):
                        continue
                    if not any(math.isfinite(engine.path_graph.distance_to_facility(
                            station.cfg.x, station.cfg.y, charger.id, station.cfg.floor, robot.cfg.width))
                               for charger in engine.chargers.values() if charger.active):
                        continue
                    energy = self._energy_needed(robot, engine, station_id, distance)
                    reserve = robot.capacity * config.BATTERY_RESERVE / 100
                    travel_time = distance * config.METERS_PER_PIXEL / robot.cfg.move_speed
                    if robot.live_battery() - energy < reserve:
                        continue
                    if travel_time + station.cfg.avg_process_time > self.horizon:
                        continue
                    if station_id not in stations:
                        stations.append(station_id)
                    entries.append((task, robot, station_id, distance, energy))
        if entries:
            rows = len(idle) + len(tasks) + len(stations)
            matrix = np.zeros((rows, len(entries)))
            costs = []
            for j, (task, robot, station_id, distance, energy) in enumerate(entries):
                matrix[idle.index(robot), j] = 1
                matrix[len(idle) + tasks.index(task), j] = 1
                matrix[len(idle) + len(tasks) + stations.index(station_id), j] = 1
                # 배정 수 증가를 우선하고, 유계인 부목적은 정규화한다.
                costs.append(task.priority * 10 + distance * config.METERS_PER_PIXEL + energy)
            costs = np.asarray(costs, dtype=float)
            costs /= max(1.0, float(costs.max()))
            objective = costs - (min(len(idle), len(tasks), len(stations)) + 1)
            result = milp(objective, integrality=np.ones(len(entries)), bounds=Bounds(0, 1),
                          constraints=LinearConstraint(matrix, 0, 1),
                          options={"time_limit": 0.25, "mip_rel_gap": 0.001})
            gap = getattr(result, "mip_gap", None)
            self.solve_records.append({"status": int(result.status),
                                       "gap": float(gap) if gap is not None and math.isfinite(gap) else None})
            x = result.x
            # 시간 제한 시에도 정수성·자원 제약을 검증한 유효 해만 사용한다.
            if x is not None and np.all(np.isfinite(x)) and np.all(np.abs(x - np.rint(x)) < 1e-5):
                rounded = np.rint(x)
                if np.all((rounded >= 0) & (rounded <= 1)) and np.all(matrix @ rounded <= 1 + 1e-6):
                    for chosen, (task, robot, station_id, _, _) in zip(rounded, entries, strict=True):
                        if chosen:
                            task.station_id = station_id
                            engine.assign(task, robot)
        # 배정되지 않은 로봇은 기존 규칙으로 충전한다. 충전 계획은 MILP 변수에 포함하지 않는다.
        for robot in idle:
            if not robot.is_available():
                continue
            if self.should_charge(robot, engine) or (not engine.pending_tasks
                                                     and robot.battery_pct < config.OPPORTUNISTIC_CHARGE_LEVEL):
                engine.send_to_charge(robot)
            elif engine.pending_tasks and robot.battery_pct < config.PRECHARGE_LEVEL:
                engine.send_to_charge(robot)
