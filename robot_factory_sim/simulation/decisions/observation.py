"""Detach currently visible policy facts without revealing future random work."""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from simulation.decisions.contracts import Observation
from simulation.planning.snapshot import topology_hash
from simulation.travel import estimate_trip

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine

TASK_FIELDS = (
    "task_id",
    "station_id",
    "priority",
    "created_at",
    "order_id",
    "allowed_stations",
    "deadline",
    "robot_id",
    "external_uid",
    "operation",
    "status",
)


def trip_key(robot: str, origin: tuple[float, float, int], destination: str) -> str:
    """Canonicalize numeric coordinate types for detached lookup."""
    return json.dumps([robot, float(origin[0]), float(origin[1]), int(origin[2]), destination])


def capture(
    engine: SimulationEngine,
    policy: str,
    token: str,
    state: str = "{}",
    deadline: float = math.inf,
) -> Observation:
    """Capture policy-visible facts and free-flow estimates; never truncate jobs.

    Args:
        engine: Live owner-thread plant, read only during this function.
        policy: Canonical selector identity.
        token: Unique engine-local decision request identity.
        state: Committed policy state, serialized separately from the plant.
        deadline: Optional monotonic capture deadline; failure is explicit.
    """
    robots, trips = [], {}
    facilities = {**engine.stations, **engine.chargers}
    points = {key: engine.path_graph.facility_point(key) or (f.cfg.x, f.cfg.y) for key, f in facilities.items()}
    floors = {key: engine.path_graph.facility_floor(key) or f.cfg.floor for key, f in facilities.items()}
    for robot in engine.robot_list():
        origins = {
            (*robot.position(), robot.floor),
            (robot.x, robot.y, robot.floor),
            *((*points[key], floors[key]) for key in facilities),
        }
        for origin in sorted(origins):
            for destination in facilities:
                if time.perf_counter() > deadline:
                    raise TimeoutError("policy observation exceeded budget")
                trip = estimate_trip(engine, robot, destination, origin=origin)
                trips[trip_key(robot.robot_id, origin, destination)] = [
                    v if math.isfinite(v) else None for v in (trip.horizontal_m, trip.seconds, trip.routing_cost_px)
                ]
        current = robot.current_task
        robots.append(
            {
                "cfg": asdict(robot.cfg),
                "robot_id": robot.robot_id,
                "x": robot.x,
                "y": robot.y,
                "position": robot.position(),
                "floor": robot.floor,
                "battery": robot.battery,
                "live": robot.live_battery(),
                "battery_pct": robot.battery_pct,
                "capacity": robot.capacity,
                "state": robot.state,
                "available": robot.is_available(),
                "alive": robot.alive,
                "forced_charge": robot.forced_charge,
                "emergency_order": robot.emergency_order,
                "current_task": {k: getattr(current, k) for k in TASK_FIELDS} if current else None,
                "target_id": robot.target_id,
            }
        )
        robots[-1]["instance"] = robot.color_index

    def facility(value: Any) -> dict[str, Any]:
        return {
            "cfg": asdict(value.cfg),
            "id": value.id,
            "active": value.active,
            "count": value.resource.count,
            "capacity": value.resource.capacity,
            "waiting": value.waiting,
            "free_slots": getattr(value, "free_slots", 0),
            "current_task": bool(getattr(value, "current_task", None)),
            "dedicated_order": getattr(value, "dedicated_order", None),
            "occupants": sorted(getattr(value, "occupants", ())),
            "incoming": sorted(getattr(value, "incoming", ())),
        }

    payload = {
        "robots": robots,
        "tasks": [{k: getattr(t, k) for k in TASK_FIELDS} for t in engine.pending_tasks],
        "stations": {key: facility(v) for key, v in engine.stations.items()},
        "chargers": {key: facility(v) for key, v in engine.chargers.items()},
        "station_order": list(engine.stations),
        "charger_order": list(engine.chargers),
        "points": points,
        "floors": floors,
        "connected": sorted(engine.path_graph.facility_nodes),
        "trips": trips,
        "params": dict(engine.params),
        "profile": asdict(engine.factory.profile),
        "plant": asdict(engine.plant),
        "topology": topology_hash(engine),
        "policy_state": json.loads(state),
    }
    return Observation(
        engine.execution_epoch,
        engine.layout_version,
        policy,
        token,
        engine.now,
        json.dumps(payload, sort_keys=True, allow_nan=False),
    )
