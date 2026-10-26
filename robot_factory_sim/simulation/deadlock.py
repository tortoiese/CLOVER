"""Wait-for graph cycle detection, separate from elapsed waiting thresholds."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def cycle_components(graph: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Return strongly connected owner groups, with one entry per actual cycle."""
    counter = 0
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    active: set[str] = set()
    cycles = []

    def visit(node: str) -> None:
        nonlocal counter
        indices[node] = low[node] = counter
        counter += 1
        stack.append(node)
        active.add(node)
        for other in sorted(graph.get(node, set())):
            if other not in indices:
                visit(other)
                low[node] = min(low[node], low[other])
            elif other in active:
                low[node] = min(low[node], indices[other])
        if low[node] == indices[node]:
            component = []
            while True:
                other = stack.pop()
                active.remove(other)
                component.append(other)
                if other == node:
                    break
            if len(component) > 1 or node in graph.get(node, set()):
                cycles.append(tuple(sorted(component)))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(cycles))


def resource_cycles(resources: Iterable[Any], owners: dict[Any, str]) -> tuple[tuple[str, ...], ...]:
    """Build a graph from observed SimPy users/queues without changing occupancy."""
    graph: dict[str, set[str]] = {}
    for resource in resources:
        holders = {owners[user.proc] for user in resource.users if user.proc in owners}
        for waiter in resource.queue:
            owner = owners.get(waiter.proc)
            if owner is not None:
                graph.setdefault(owner, set()).update(holders)
    return cycle_components(graph)


def observe_and_recover(engine: SimulationEngine) -> None:
    """Count new cycle episodes and interrupt only a waiting, stationary victim."""
    from simulation.robot import InterruptKind
    from utils.event_types import RobotState, TimelineKind

    resources = [s.resource for s in engine.stations.values()] + [c.resource for c in engine.chargers.values()]
    resources += list({id(r): r for r in engine.corridors._corridor_resource.values()}.values())
    resources += list(engine.corridors._intersection_resource.values())
    resources += [e.resource for e in engine.elevators.values()]
    owners = {r.process: r.robot_id for r in engine.robot_list()}
    cycles = set(resource_cycles(resources, owners))
    new = cycles - engine._deadlock_episodes
    engine._deadlock_episodes = cycles
    for cycle in sorted(new):
        engine.metrics.record_deadlock()
        victims = [engine.robots[rid] for rid in cycle if rid in engine.robots and
                   engine.robots[rid].state in (RobotState.WAITING, RobotState.CHARGE_WAIT, RobotState.MOVING) and
                   engine.robots[rid]._segment is None and engine.robots[rid]._riding is None and
                   engine.robots[rid]._charge_step is None]
        victim = max(victims, key=lambda r: r.robot_id) if victims else None
        recovered = bool(victim and victim.interrupt(InterruptKind.STATION_DOWN))
        engine._timeline(TimelineKind.WARNING if hasattr(TimelineKind, "WARNING") else TimelineKind.INFO,
                         f"교착 순환 {','.join(cycle)} · " + (f"{victim.robot_id} 안전 중단" if recovered else "안전 경계 대기"))
        engine.deadlock_events.append({"time": engine.now, "owners": cycle,
                                       "victim": victim.robot_id if recovered else None, "recovery": recovered})
        if len(engine.deadlock_events) > 50_000:
            del engine.deadlock_events[:1000]
            engine.log_truncated += 1000
