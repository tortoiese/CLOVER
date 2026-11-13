"""Observe current ownership and committed obligations without future samples."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from simulation.planning.contracts import ResourceHold
from simulation.task_generator import TaskState
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.elevator import Elevator
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot


def owner_key(robot: Robot) -> str:
    """Do not merge two instances that happen to reuse a display ID."""
    return f"robot:{robot.color_index}:{robot.robot_id}"


def cabin_release(robot: Robot, elevator: Elevator) -> tuple[float, int] | None:
    """Certify only a protected loaded ride owned by this live request/process."""
    end, floor, request = robot._cabin_release_at, robot._cabin_destination, robot._cabin_request
    if (
        robot._riding is elevator
        and request is not None
        and request in elevator.resource.users
        and request.proc is robot.process
        and end is not None
        and math.isfinite(end)
        and end > robot.env.now + 1e-9
        and type(floor) is int
        and floor >= 1
    ):
        return end, floor
    return None


def capture_holds(engine: SimulationEngine) -> tuple[ResourceHold, ...]:
    """Read actual claims, exact tail/charge ends and conservative future holds."""
    now = engine.now
    actors = [*engine.robots.values(), *engine.retired]
    processes = {r.process: r for r in actors}
    requests = {r._traffic_request: r for r in actors if r._traffic_request is not None}
    holds = []
    for pool in sorted(engine.corridors._pools.values(), key=lambda p: p.resource_id):
        for claim in sorted(pool.users, key=lambda c: c.request.request_id):
            robot = requests.get(claim.request)
            end = robot._traffic_release_at if robot else None
            if end is not None and (not math.isfinite(end) or end < now - 1e-9):
                end = None
            holds.append(
                ResourceHold(
                    owner_key(robot) if robot else f"traffic:{claim.request.request_id}",
                    pool.resource_id,
                    claim.amount,
                    pool.unit,
                    now,
                    max(now, end) if end is not None else None,
                    "actual",
                )
            )
    groups = [("work", engine.stations), ("charge", engine.chargers), ("cabin", engine.elevators)]
    for kind, facilities in groups:
        for fid, facility in sorted(facilities.items()):
            for i, request in enumerate(facility.resource.users):
                robot = processes.get(request.proc)
                end = robot._planned_charge_until if robot and kind == "charge" else None
                if robot and kind == "cabin":
                    certificate = cabin_release(robot, facility)
                    end = certificate[0] if certificate else None
                if end is not None and (not math.isfinite(end) or end < now - 1e-9):
                    end = None
                resource = kind + ":" + fid
                holds.append(
                    ResourceHold(
                        owner_key(robot) if robot else f"opaque:{resource}:{i}",
                        resource,
                        1,
                        "slot",
                        now,
                        max(now, end) if end is not None else None,
                        "actual",
                    )
                )
            for i, request in enumerate(facility.resource.queue):
                robot = processes.get(request.proc)
                holds.append(
                    ResourceHold(
                        owner_key(robot) if robot else f"queued:{kind}:{fid}:{i}",
                        kind + ":" + fid,
                        1,
                        "slot",
                        now,
                        None,
                        "future",
                    )
                )
    for robot in actors:
        plan = robot.planned_action
        if plan is not None:
            result, action = plan
            finished = robot.planned_route_completed()
            progress = robot.planned_route_progress()
            route = result.input.routes[action.arc.route] if action.arc.route is not None else None
            tail_at_destination = robot._traffic_release_at is not None and not robot.route
            for index, use in enumerate(action.arc.uses):
                movement = not use.resource.startswith(("work:", "charge:"))
                if movement and (finished or tail_at_destination):
                    continue  # Actual last-tail claims, if any, remain above.
                start = result.input.snapshot.now + use.start * result.input.snapshot.settings.time_unit
                end = robot._planned_charge_until if use.resource == "charge:" + action.arc.destination else None
                if route is not None and index < len(route.uses) and index < len(route.use_sections):
                    lo, hi = route.use_sections[index]
                    if progress > hi:
                        continue
                    if lo <= progress <= hi and robot._traffic_release_at is not None:
                        end = robot._traffic_release_at
                if use.resource.startswith("cabin:"):
                    start = now  # Do not change a committed cabin's future source floor.
                    cabin_index = index - len(route.uses) if route is not None else -1
                    if route is not None and 0 <= cabin_index < len(route.cabins):
                        trip = route.cabins[cabin_index]
                        if 0 <= trip.segment_index < progress:
                            continue
                        elevator = engine.elevators.get(trip.cabin)
                        certificate = cabin_release(robot, elevator) if elevator is not None else None
                        if trip.segment_index == progress and certificate is not None:
                            end = certificate[0]
                holds.append(
                    ResourceHold(
                        owner_key(robot),
                        use.resource,
                        use.amount,
                        use.unit,
                        min(start, end) if end is not None else start,
                        end,
                        "prefix",
                    )
                )
            continue
        if not robot.alive or robot.is_available():
            continue
        remaining_lifts = robot.route_lifts
        if (
            robot._riding is not None
            and cabin_release(robot, robot._riding) is not None
            and remaining_lifts
            and remaining_lifts[0] == robot._riding.id
        ):
            remaining_lifts = remaining_lifts[1:]
        future = {lift for lift in remaining_lifts if lift is not None}
        settled = (
            robot.current_task is not None and robot.current_task.status in {TaskState.WAITING, TaskState.PROCESSING}
        ) or robot.state in {RobotState.CHARGING, RobotState.CHARGE_WAIT}
        if not robot.route and not settled:
            future.update(engine.elevators)  # Assigned/faulted but route not yet observable.
        for cid in sorted(future):
            holds.append(ResourceHold(owner_key(robot), "cabin:" + cid, 1, "slot", now, None, "future"))
    return tuple(holds)
