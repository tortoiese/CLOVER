"""One continuous grid-time path per cabin, coupled to selected robot trips."""

from __future__ import annotations

import math
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from simulation.planning.cabin_state import ready_state

if TYPE_CHECKING:
    from simulation.planning.model import Model


@dataclass(frozen=True)
class Trip:
    """A compound empty call, boarding, loaded travel and alighting transition."""

    cabin: str
    source: float
    destination: float
    start: int
    end: int


def trips(model: Model, index: int) -> tuple[Trip, ...]:
    """Recover trip transitions from one robot action's geometric variant."""
    arc = model.arcs[index]
    if arc.route is None:
        return ()
    step = model.input.snapshot.settings.step
    result = []
    for use in model.input.routes[arc.route].cabins:
        start = arc.start + math.floor(use.start / step + 1e-9)
        end = max(start + 1, arc.start + math.ceil(use.end / step - 1e-9))
        result.append(Trip(use.cabin, float(use.source_floor), float(use.destination), start, end))
    return tuple(result)


def add_cabin_flow(model: Model, deadline: float | None = None) -> None:
    """Connect trip selectors to cabin states; no free reposition or teleport."""
    inbound, outbound = defaultdict(list), defaultdict(list)
    floors: dict[str, set[float]] = defaultdict(set)
    for index in range(len(model.arcs)):
        uses = trips(model, index)
        if uses:
            model.cabin_trips[index] = uses
        for use in uses:
            outbound[use.cabin, use.source, use.start].append(index)
            inbound[use.cabin, use.destination, use.end].append(index)
            floors[use.cabin].update((use.source, use.destination))
    cabins = {c.id: c for c in model.input.snapshot.cabins}
    terminal = model.terminal_tick
    states = 0
    for cid, positions in floors.items():
        source = float(cabins[cid].floor)
        model.cabin_initial[cid] = source
        positions.add(source)
        ready_floor, ready_tick = ready_state(cabins[cid], model.input.snapshot.now, model.input.snapshot.settings.step)
        if ready_tick:
            if ready_tick > terminal:
                raise ValueError("MODEL_INVALID: fixed cabin trip exceeds model terminal")
            positions.add(ready_floor)
            trip = Trip(cid, source, ready_floor, 0, ready_tick)
            model.cabin_fixed[cid] = trip
            fixed = model.variable("cabin_fixed:" + cid, 1, 1)
            model.cabin_fixed_variables[cid] = fixed
            model.hint[fixed] = 1
            outbound[cid, source, 0].append(fixed)
            inbound[cid, ready_floor, ready_tick].append(fixed)
        states += len(positions) * (terminal + 1)
        if states > model.input.snapshot.settings.max_cabin_states:
            raise ValueError("BUILD_LIMIT: cabin state limit exceeded")
        for floor in sorted(positions):
            for tick in range(terminal):
                if tick % 128 == 0 and deadline is not None and time.perf_counter() >= deadline:
                    raise TimeoutError("cabin flow building exhausted decision budget")
                v = model.variable(f"cabin_idle:{cid}:{floor}:{tick}", 0, 1)
                model.cabin_waits[cid, floor, tick] = v
                outbound[cid, floor, tick].append(v)
                inbound[cid, floor, tick + 1].append(v)
                if floor == ready_floor and tick >= ready_tick:
                    model.hint[v] = 1
            sink = model.variable(f"cabin_sink:{cid}:{floor}", 0, 1)
            model.cabin_sinks[cid, floor] = sink
            outbound[cid, floor, terminal].append(sink)
            if floor == ready_floor:
                model.hint[sink] = 1
            for tick in range(terminal + 1):
                terms = Counter(outbound[cid, floor, tick])
                terms.subtract(inbound[cid, floor, tick])
                supply = int(tick == 0 and floor == source)
                model.row(dict(terms), supply, supply)
        model.row({v: 1 for (owner, _), v in model.cabin_sinks.items() if owner == cid}, 1, 1)


def complete_hint(model: Model, values: list[int]) -> bool:
    """Fill auxiliary idle/sink values for a checked, continuous selected trip list."""
    selected = defaultdict(list)
    for index, uses in model.cabin_trips.items():
        if values[index]:
            for use in uses:
                selected[use.cabin].append(use)
    for index in model.cabin_waits.values():
        values[index] = 0
    for index in model.cabin_sinks.values():
        values[index] = 0
    for cid, floor in model.cabin_initial.items():
        tick = 0
        required = [model.cabin_fixed[cid]] if cid in model.cabin_fixed else []
        for use in sorted([*required, *selected[cid]], key=lambda t: (t.start, t.end)):
            if use.source != floor or use.start < tick or use.end > model.terminal_tick:
                return False
            for at in range(tick, use.start):
                values[model.cabin_waits[cid, floor, at]] = 1
            floor, tick = use.destination, use.end
        for at in range(tick, model.terminal_tick):
            values[model.cabin_waits[cid, floor, at]] = 1
        values[model.cabin_sinks[cid, floor]] = 1
    return True
