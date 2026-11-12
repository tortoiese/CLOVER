"""Physical distance and free-flow duration, separate from Dijkstra surrogate cost."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import config
from simulation.traffic_rules import tail_clearance_seconds

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


@dataclass(frozen=True)
class TripEstimate:
    """A current-state free-flow estimate, excluding competing traffic waits."""

    horizontal_m: float
    seconds: float
    routing_cost_px: float


def estimate_trip(
    engine: SimulationEngine, robot: Robot, destination: str, *, origin: tuple[float, float, int] | None = None
) -> TripEstimate:
    """Estimate horizontal travel, cabin motion, intersections and section clearance."""
    if hasattr(engine, "observed_trip"):
        return engine.observed_trip(robot, destination, origin)
    x, y, floor = origin or (*robot.position(), robot.floor)
    route = engine.path_graph.for_robot(robot.cfg.width).route_to_facility(x, y, destination, floor)
    if route is None:
        return TripEstimate(math.inf, math.inf, math.inf)
    metres = seconds = 0.0
    speed = max(0.05, robot.cfg.move_speed)
    cabin_floors = {eid: elevator.floor_position() for eid, elevator in engine.elevators.items()}
    index = 0
    while index < len(route.points):
        lift = route.lifts[index] if route.lifts else None
        if lift:
            target = route.floors[index]
            elevator = engine.elevators.get(lift)
            if elevator is None or not elevator.active:
                return TripEstimate(math.inf, math.inf, route.length)
            seconds += (abs(cabin_floors[lift] - floor) + abs(target - floor)) * elevator.cfg.travel_time
            seconds += elevator.door_time
            cabin_floors[lift] = target
            x, y = route.points[index]
            floor, index = target, index + 1
            continue
        end = engine.corridors.section_end(route, index)
        for i in range(index, end + 1):
            tx, ty = route.points[i]
            distance = math.hypot(tx - x, ty - y) * config.METERS_PER_PIXEL
            metres += distance
            seconds += distance / speed
            x, y = tx, ty
            if engine.corridors.is_intersection(route.nodes[i]):
                seconds += robot.cfg.length / speed + config.INTERSECTION_MARGIN_SEC
        demands = engine.corridors.section_demands(route, index, end, robot.cfg.width)
        if demands:
            seconds += tail_clearance_seconds(robot.cfg.length, speed)
        index = end + 1
    return TripEstimate(metres, seconds, route.length)
