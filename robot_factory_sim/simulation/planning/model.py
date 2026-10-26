"""One integer linear model compiled without semantic changes by either solver."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass

from simulation.planning.contracts import (
    MODEL_VERSION,
    Arc,
    CabinArc,
    Snapshot,
    energy_up,
)
from simulation.planning.units import cost_ticks, decimal, distance_ticks, gain_limit


@dataclass(frozen=True)
class Variable:
    """An integer variable with finite inclusive bounds."""

    name: str
    lower: int
    upper: int


@dataclass(frozen=True)
class Row:
    """A sparse bounded linear expression; None is an unbounded side."""

    terms: tuple[tuple[int, int], ...]
    lower: int | None
    upper: int | None


@dataclass
class Model:
    """Portable model and decoding indices, shared by MILP and CP-SAT."""

    snapshot: Snapshot
    arcs: list[Arc]
    variables: list[Variable]
    rows: list[Row]
    objective1: dict[int, int]
    objective2: dict[int, int]
    gain: dict[int, int]
    deferred: dict[int, int]
    fingerprint: str = ""
    cabins: dict[int, CabinArc] | None = None

    def variable(self, name: str, lower: int, upper: int) -> int:
        """Create an integer variable and return its stable index."""
        index = len(self.variables)
        self.variables.append(Variable(name, lower, upper))
        return index

    def row(self, terms: dict[int, int], lower: int | None, upper: int | None) -> None:
        """Add a canonical row, rejecting unsafe CP-SAT integer magnitudes."""
        clean = tuple(sorted((v, c) for v, c in terms.items() if c))
        bound = sum(abs(c) * max(abs(self.variables[v].lower), abs(self.variables[v].upper)) for v, c in clean)
        if bound >= 2**60:
            raise ValueError("integer expression exceeds safe CP-SAT range")
        self.rows.append(Row(clean, lower, upper))


def build_model(snapshot: Snapshot, deadline: float | None = None) -> Model:
    """Build an acyclic path model with energy, service capacity and job deferral."""
    snapshot.settings.validate()
    step = snapshot.settings.step
    n = math.floor(snapshot.settings.horizon / step + 1e-9)
    model = Model(snapshot, [], [], [], {}, {}, {}, {})
    facilities = {f.id: f for f in snapshot.facilities}
    routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
    job_vars: dict[int, list[int]] = defaultdict(list)
    resource_vars: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    ride_vars: dict[CabinArc, list[int]] = defaultdict(list)
    outgoing: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    incoming: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    energy_vars: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
    robot_arcs: dict[str, list[int]] = defaultdict(list)
    for robot in snapshot.robots:
        locations = [robot.location, *(f.id for f in snapshot.facilities if f.id != robot.location)]
        visited = {(robot.location, 0)}
        queue = deque([(robot.location, 0)])

        def add(arc: Arc, robot_id: str = robot.id, seen: set = visited, pending: deque = queue) -> None:
            index = len(model.arcs)
            if index >= snapshot.settings.max_arcs:
                raise ValueError("GRAPH_LIMIT: time-expanded arc limit exceeded")
            model.arcs.append(arc)
            model.variable(f"x{index}", 0, 1)
            outgoing[robot_id, arc.origin, arc.start].append(index)
            incoming[robot_id, arc.destination, arc.end].append(index)
            energy_vars[robot_id, arc.start].append((index, arc.energy))
            robot_arcs[robot_id].append(index)
            if arc.job is not None:
                job_vars[arc.job].append(index)
            if arc.resource:
                for tick in range(arc.start, arc.end):
                    resource_vars[arc.resource, tick].append((index, 1))
            if arc.kind == "move" and snapshot.scope == "traffic":
                route = routes[robot_id, arc.origin, arc.destination]
                for use in route.uses:
                    for tick in range(arc.start + use.start, arc.start + use.end):
                        resource_vars[use.resource, tick].append((index, use.amount))
                for ride in route.rides:
                    absolute = CabinArc(ride.cabin, ride.origin, ride.destination,
                                        arc.start + ride.start, arc.start + ride.end, "ride")
                    ride_vars[absolute].append(index)
            state = (arc.destination, arc.end)
            if state not in seen:
                seen.add(state)
                pending.append(state)

        while queue:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("model building exhausted total solve budget")
            location, tick = queue.popleft()
            if tick >= n:
                continue
            add(Arc(robot.id, location, location, tick, tick + 1, "wait"))
            facility = facilities.get(location)
            if facility is not None:
                if facility.kind == "charge" and facility.rate > 0:
                    add(Arc(robot.id, location, location, tick, tick + 1, "charge", location))
                elif facility.kind == "work" and not robot.charge_only:
                    duration = max(1, math.ceil(facility.duration / step))
                    if tick + duration <= n:
                        for job in snapshot.jobs:
                            if location not in job.stations:
                                continue
                            if robot.emergency_order is not None and robot.emergency_order != job.order_id:
                                continue
                            add(Arc(robot.id, location, location, tick, tick + duration, "work", location,
                                    job.id, energy_up(decimal(duration) * decimal(step) * decimal(snapshot.work_drain))))
            for destination in locations:
                route = routes.get((robot.id, location, destination))
                if destination == location or route is None:
                    continue
                duration = max(1, math.ceil(route.seconds / step))
                if tick + duration <= n:
                    add(Arc(robot.id, location, destination, tick, tick + duration, "move",
                            energy=energy_up(decimal(route.distance_m) * decimal(robot.drain)), distance_m=route.distance_m))

    # All arc variables precede auxiliary variables, so arc index equals x index.
    for job in snapshot.jobs:
        deferred = model.variable(f"defer{job.id}", 0, 1)
        model.deferred[job.id] = deferred
        model.objective1[deferred] = job.weight
        model.row({**{v: 1 for v in job_vars[job.id]}, deferred: 1}, 1, 1)
    for robot in snapshot.robots:
        states = {key[1:] for key in outgoing if key[0] == robot.id} | {
            key[1:] for key in incoming if key[0] == robot.id}
        terminal = []
        for location, tick in sorted(states):
            terms = {v: 1 for v in outgoing[robot.id, location, tick]}
            for v in incoming[robot.id, location, tick]:
                terms[v] = terms.get(v, 0) - 1
            if tick == n:
                sink = model.variable(f"sink:{robot.id}:{location}", 0, 1)
                terms[sink] = 1
                terminal.append((location, sink))
            supply = int(tick == 0 and location == robot.location)
            model.row(terms, supply, supply)
        model.row({v: 1 for _, v in terminal}, 1, 1)
        lower = robot.reserve if robot.battery >= robot.reserve else 0
        batteries = [model.variable(f"B:{robot.id}:{t}", lower, robot.capacity) for t in range(n + 1)]
        model.row({batteries[0]: 1}, robot.battery, robot.battery)
        for tick in range(n):
            terms = {batteries[tick + 1]: 1, batteries[tick]: -1}
            for v, energy in energy_vars[robot.id, tick]:
                terms[v] = energy
                arc = model.arcs[v]
                if arc.kind == "charge":
                    facility = facilities[arc.resource]
                    maximum = gain_limit(facility.rate, step, facility.rate_raw)
                    gain = model.variable(f"g{v}", 0, maximum)
                    model.gain[v] = gain
                    terms[gain] = -1
                    model.row({gain: 1, v: -maximum}, None, 0)
                    model.objective2[gain] = cost_ticks(decimal("0.000001"))
            model.row(terms, 0, 0)
        for location, sink in terminal:
            returns = [0 if location == f.id else energy_up(decimal(routes[robot.id, location, f.id].distance_m)
                                                           * decimal(robot.drain))
                       for f in snapshot.facilities if f.kind == "charge"
                       and (location == f.id or (robot.id, location, f.id) in routes)]
            if not returns:
                model.row({sink: 1}, 0, 0)
            else:
                required = max(robot.reserve, robot.charge_target) + min(returns)
                model.row({batteries[-1]: 1, sink: -required}, 0, None)
        for v in robot_arcs[robot.id]:
            arc = model.arcs[v]
            if arc.kind != "work":
                continue
            returns = [0 if arc.destination == f.id else
                       energy_up(decimal(routes[robot.id, arc.destination, f.id].distance_m) * decimal(robot.drain))
                       for f in snapshot.facilities if f.kind == "charge" and
                       (arc.destination == f.id or (robot.id, arc.destination, f.id) in routes)]
            if returns:
                model.row({batteries[arc.end]: 1, v: -(robot.reserve + min(returns))}, 0, None)
            else:
                model.row({v: 1}, 0, 0)
    capacities = {f.id: f.capacity for f in snapshot.facilities} | {r.id: r.capacity for r in snapshot.resources}
    for (resource, tick), variables in sorted(resource_vars.items()):
        start, end = snapshot.now + tick * step, snapshot.now + (tick + 1) * step
        used = sum(r.amount for r in snapshot.fixed if r.resource == resource and r.start < end and r.end > start)
        residual = max(0, capacities[resource] - used)
        coefficients: dict[int, int] = defaultdict(int)
        for v, amount in variables:
            coefficients[v] += amount
        model.row(coefficients, None, residual)
    # A single cabin must follow one floor/time path. Loaded ride variables are
    # linked exactly to robot moves; empty relocations cannot teleport a cabin.
    model.cabins = {}
    if snapshot.scope == "traffic":
        for cabin in snapshot.cabins:
            if not cabin.available:
                continue
            cabin_out: dict[tuple[int, int], list[int]] = defaultdict(list)
            cabin_in: dict[tuple[int, int], list[int]] = defaultdict(list)

            def cabin_arc(arc: CabinArc, outs: dict = cabin_out, ins: dict = cabin_in) -> int:
                if len(model.arcs) + len(model.cabins) >= snapshot.settings.max_arcs:
                    raise ValueError("GRAPH_LIMIT: cabin arc limit exceeded")
                v = model.variable(f"cabin:{len(model.cabins)}", 0, 1)
                model.cabins[v] = arc
                outs[arc.origin, arc.start].append(v)
                ins[arc.destination, arc.end].append(v)
                if arc.kind == "empty":
                    model.objective2[v] = cost_ticks(decimal("0.01") * decimal(arc.end - arc.start) * decimal(step))
                return v

            for tick in range(n):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("cabin model building exhausted solve budget")
                for floor in cabin.floors:
                    cabin_arc(CabinArc(cabin.id, floor, floor, tick, tick + 1, "wait"))
                    for destination in cabin.floors:
                        if destination == floor:
                            continue
                        duration = max(1, math.ceil(abs(destination - floor) * cabin.travel_seconds / step))
                        if tick + duration <= n:
                            cabin_arc(CabinArc(cabin.id, floor, destination, tick, tick + duration, "empty"))
            for arc, linked in ride_vars.items():
                if arc.cabin == cabin.id:
                    v = cabin_arc(arc)
                    model.row({v: 1, **{x: -1 for x in linked}}, 0, 0)
            sinks = []
            for floor in cabin.floors:
                for tick in range(n + 1):
                    terms = {v: 1 for v in cabin_out[floor, tick]}
                    for v in cabin_in[floor, tick]:
                        terms[v] = terms.get(v, 0) - 1
                    if tick == n:
                        sink = model.variable(f"cabin_sink:{cabin.id}:{floor}", 0, 1)
                        sinks.append(sink)
                        terms[sink] = 1
                    supply = int(tick == 0 and floor == cabin.floor)
                    model.row(terms, supply, supply)
            model.row({v: 1 for v in sinks}, 1, 1)
        available = {c.id for c in snapshot.cabins if c.available}
        for arc, linked in ride_vars.items():
            if arc.cabin not in available:
                model.row({v: 1 for v in linked}, 0, 0)
    jobs = {j.id: j for j in snapshot.jobs}
    for v, arc in enumerate(model.arcs):
        cost = decimal(0)
        if arc.kind == "work":
            job = jobs[arc.job]
            start = snapshot.now + arc.start * step
            end = snapshot.now + arc.end * step
            horizon = decimal(n) * decimal(step)
            cost = max(decimal(0), decimal(start) - decimal(job.created)) / horizon
            if job.deadline is not None:
                cost += 5 * max(decimal(0), decimal(end) - decimal(job.deadline)) / horizon
        elif arc.kind == "move":
            cost = decimal("0.00001") * distance_ticks(arc.distance_m)
        elif arc.kind == "charge":
            cost = decimal("0.01") * decimal(step)
        if cost:
            model.objective2[v] = cost_ticks(cost)
    for objective in (model.objective1, model.objective2):
        if sum(abs(c) * model.variables[v].upper for v, c in objective.items()) >= 2**60:
            raise ValueError("objective exceeds safe integer range")
    payload = {"version": MODEL_VERSION, "snapshot": asdict(snapshot),
               "variables": [asdict(v) for v in model.variables], "rows": [asdict(r) for r in model.rows],
               "objectives": [model.objective1, model.objective2]}
    # Request identity and backend do not change the mathematical model.
    payload["snapshot"].pop("token")
    payload["snapshot"]["settings"] = {key: payload["snapshot"]["settings"][key]
                                       for key in ("horizon", "step", "scope")}
    model.fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return model
