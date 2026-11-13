"""Observation helpers that yield exactly the original resource event."""

from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import simpy

if TYPE_CHECKING:
    from simulation.robot import Robot

OBSERVATION_EVENTS = frozenset({"resource_wait_start", "resource_wait_end", "cabin_phase_start", "cabin_phase_end"})


def observe_wait(robot: Robot, request: simpy.Event, channel: str, resource: str) -> Generator[Any, Any, None]:
    """Observe admission, withdrawal and open waits without scheduling another event.

    Args:
        robot: Requesting robot, including its stable instance identity.
        request: The exact original resource/bundle event to yield.
        channel: Corridor, intersection, station, charge or cabin.
        resource: Logical resource or initial blocker of a traffic bundle.
    """
    ledger, started = robot.engine.ledger, float(robot.env.now)
    identity = f"wait-{ledger.sequence}"
    task = robot.current_task
    metadata = {
        "wait_id": identity,
        "robot": robot.robot_id,
        "robot_instance": robot.color_index,
        "channel": channel,
        "resource": resource,
        "uid": task.external_uid if task else None,
    }
    demands = getattr(request, "demands", {})
    ledger.emit(
        "resource_wait_start",
        **metadata,
        demands=[
            {"resource": pool.resource_id, "unit": pool.unit, "amount": amount} for pool, amount in demands.items()
        ],
    )
    outcome = "withdrawn"
    try:
        yield request
        outcome = "granted"
    except GeneratorExit:
        outcome = "shutdown"
        raise
    except simpy.Interrupt:
        # An interrupt can race an already triggered grant at the same time.
        outcome = "granted" if request.triggered and request.ok else "withdrawn"
        raise
    except Exception:
        outcome = "failed"
        raise
    finally:
        ledger.emit("resource_wait_end", **metadata, outcome=outcome, elapsed=float(robot.env.now) - started)
