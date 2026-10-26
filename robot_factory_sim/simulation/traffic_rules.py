"""통행 폭 판정과 차선 일괄 확보. 단위는 m, 대기 순서는 FIFO다."""

from __future__ import annotations

import math

import simpy
from simpy.resources.resource import Request

from utils.config_models import FactoryProfile

WIDTH_TOLERANCE = 1e-9


class TrafficBlocked(ValueError):
    """현재 로봇이 통로의 물리 조건을 만족하지 못한다."""


def required_width(robot_width: float, profile: FactoryProfile) -> float:
    """로봇 폭과 벽·측면 여유를 포함한 최소 통로 폭을 반환한다.

    Args:
        robot_width: 로봇 폭(m).
        profile: 여유 폭 설정.

    Returns:
        필요한 통로 폭(m).
    """
    values = (robot_width, profile.edge_clearance, profile.side_clearance)
    if any(not math.isfinite(value) or value < 0 for value in values) or robot_width <= 0:
        raise ValueError("로봇 폭은 양수, 여유 폭은 유한한 비음수여야 합니다.")
    return sum(values)


def can_pass(edge_width: float, robot_width: float, profile: FactoryProfile) -> bool:
    """통로가 로봇과 여유 폭을 수용하는지 판정한다.

    Args:
        edge_width: 통로 폭(m).
        robot_width: 로봇 폭(m).
        profile: 여유 폭 설정.

    Returns:
        통행 가능 여부.
    """
    return math.isfinite(edge_width) and edge_width + WIDTH_TOLERANCE >= required_width(robot_width, profile)


class LaneRequest(Request):
    """로봇 하나가 필요한 차선을 원자적으로 요청하는 이벤트."""

    def __init__(self, resource: LaneResource, units: int, robot_id: str) -> None:
        """점유량과 요청 시각을 기록한다.

        Args:
            resource: 차선 또는 교차로 자원.
            units: 동시에 확보할 차선 수.
            robot_id: 요청 로봇 ID.
        """
        self.units = units
        self.robot_id = robot_id
        self.created_at = float(resource._env.now)
        super().__init__(resource)


class LaneResource(simpy.Resource):
    """여러 차선을 부분 점유하지 않고 한 번에 확보하는 FIFO 자원.

    상위 Resource의 취소·해제 컨텍스트를 사용한다. 큰 로봇 두 대가
    각각 차선 일부만 잡은 채 서로 기다리는 상황을 방지한다.
    """

    @property
    def count(self) -> int:
        """현재 점유 중인 차선 수를 반환한다."""
        return sum(request.units for request in self.users)

    def request(self, units: int = 1, robot_id: str = "") -> LaneRequest:
        """차선을 일괄 요청한다. 확보 불가능한 크기는 즉시 거부한다.

        Args:
            units: 필요한 차선 수.
            robot_id: 요청 로봇 ID.

        Returns:
            컨텍스트 관리가 가능한 요청 이벤트.
        """
        if isinstance(units, bool) or int(units) != units or not 1 <= units <= self.capacity:
            raise ValueError("필요한 차선 수가 통로 용량을 초과했습니다.")
        return LaneRequest(self, int(units), robot_id)

    def _do_put(self, event: LaneRequest) -> bool | None:
        if self.count + event.units <= self.capacity:
            self.users.append(event)
            event.usage_since = self._env.now
            event.succeed()
            return True
        return None
