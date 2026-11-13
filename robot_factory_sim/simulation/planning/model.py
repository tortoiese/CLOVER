"""Sparse service-flow MILP: starts in H, full occupancy and energy through tail."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field

from simulation.planning.cabin_flow import Trip, add_cabin_flow
from simulation.planning.cabin_state import validate_releases
from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import MODEL_VERSION, Arc, PreparedInput, RobotInput, Use
from simulation.planning.units import decimal, energy_up, gain_limit


@dataclass(frozen=True)
class Variable:
    """A bounded integer variable, usable by MILP and future CP-SAT compilers."""

    name: str
    lower: int
    upper: int


@dataclass(frozen=True)
class Row:
    """A sparse finite-integer linear row with optional unbounded sides."""

    terms: tuple[tuple[int, int], ...]
    lower: int | None
    upper: int | None
    kind: str = "linear"


@dataclass
class Model:
    """Canonical sparse constraints plus decoding and a checked safe-hold hint."""

    input: PreparedInput
    arcs: list[Arc]
    variables: list[Variable]
    rows: list[Row]
    objective1: dict[int, int]
    objective2: dict[int, int]
    gain: dict[int, int]
    deferred: dict[str, int]
    hint: dict[int, int]
    terminal_tick: int = 0
    fingerprint: str = ""
    waits: dict[tuple[str, str, int], int] = field(default_factory=dict)
    sinks: dict[tuple[str, str, int], int] = field(default_factory=dict)
    batteries: dict[str, tuple[int, ...]] = field(default_factory=dict)
    return_energy: dict[tuple[str, str], int | None] = field(default_factory=dict)
    cabin_waits: dict[tuple[str, float, int], int] = field(default_factory=dict)
    cabin_sinks: dict[tuple[str, float], int] = field(default_factory=dict)
    cabin_initial: dict[str, float] = field(default_factory=dict)
    cabin_trips: dict[int, tuple] = field(default_factory=dict)
    cabin_fixed: dict[str, Trip] = field(default_factory=dict)
    cabin_fixed_variables: dict[str, int] = field(default_factory=dict)

    def variable(self, name: str, lower: int, upper: int) -> int:
        """Append a bounded integer variable with stable index."""
        if lower > upper:
            raise ValueError("MODEL_INVALID: reversed variable bounds")
        self.variables.append(Variable(name, lower, upper))
        return len(self.variables) - 1

    def row(self, terms: dict[int, int], lower: int | None, upper: int | None, kind: str = "linear") -> None:
        """Add a sparse row and reject unsafe integer magnitudes."""
        clean = tuple(sorted((index, value) for index, value in terms.items() if value))
        if sum(abs(c) * max(abs(self.variables[v].lower), abs(self.variables[v].upper)) for v, c in clean) >= 2**60:
            raise ValueError("MODEL_INVALID: integer expression overflow")
        self.rows.append(Row(clean, lower, upper, kind))


def initial_location(robot: RobotInput, data: PreparedInput) -> str:
    """Use a virtual current location that cannot alias a facility ID."""
    location = "@" + robot.id
    while location in {f.id for f in data.snapshot.facilities}:
        location = "@" + location
    return location


def budgets(data: PreparedInput) -> dict[str, tuple[str, int]]:
    """Compatibility view of residual capacity at observation tick zero."""
    calendar = build_calendar(data)
    return {key: (unit, calendar.at(key, 0)) for key, (unit, _) in calendar.capacities.items()}


def build_model(data: PreparedInput, deadline: float | None = None) -> Model:
    """Build a DAG path per idle robot with indivisible move/work service arcs."""
    snapshot, settings = data.snapshot, data.snapshot.settings
    settings.validate()
    validate_releases(snapshot, json.loads(snapshot.factory_json)["profile"]["floor_count"])
    step = settings.step
    horizon = math.floor(settings.horizon / step + 1e-9)
    model = Model(data, [], [], [], {}, {}, {}, {}, {})
    facilities = {f.id: f for f in snapshot.facilities if f.active}
    routes = defaultdict(list)
    for i, r in enumerate(data.routes):
        routes[r.robot, r.origin, r.destination].append((i, r))
    calendar = build_calendar(data)
    outgoing, incoming, jobs, resources, energies, robot_arcs = (defaultdict(list) for _ in range(6))
    cabin_arcs: dict[str, set[int]] = defaultdict(set)
    states_by_robot = {}

    def check_budget() -> None:
        if deadline is not None and time.perf_counter() >= deadline:
            raise TimeoutError("model building exhausted decision budget")

    for robot in snapshot.robots:
        if not robot.available:
            continue
        source = initial_location(robot, data)
        seen = {(source, 0)}
        pending = deque([(source, 0)])

        def add(arc: Arc) -> None:
            if any(
                use.amount > calendar.at(use.resource, tick) for use in arc.uses for tick in range(use.start, use.end)
            ):
                return  # Individually impossible under fixed obligations; waiting remains available.
            if arc.end - horizon > settings.max_tail_ticks:
                raise ValueError("BUILD_LIMIT: long service exceeds explicit tail limit")
            if len(model.arcs) >= settings.max_arcs:
                raise ValueError("BUILD_LIMIT: service arc limit exceeded")
            v = len(model.arcs)
            model.arcs.append(arc)
            model.variable(f"x{v}", 0, 1)
            outgoing[robot.id, arc.origin, arc.start].append(v)
            incoming[robot.id, arc.destination, arc.end].append(v)
            energies[robot.id, arc.start].append((v, arc.energy))
            robot_arcs[robot.id].append(v)
            if arc.job is not None:
                jobs[arc.job].append(v)
            for use in arc.uses:
                for tick in range(use.start, use.end):
                    resources[use.resource, tick].append((v, use.amount))
            for cabin in arc.cabins:
                cabin_arcs[cabin].add(v)
            if arc.kind == "wait" and arc.origin == source:
                model.hint[v] = 1
            if arc.kind == "wait":
                model.waits[robot.id, arc.origin, arc.start] = v
            state = arc.destination, arc.end
            if state not in seen:
                seen.add(state)
                pending.append(state)

        while pending:
            check_budget()
            location, tick = pending.popleft()
            if tick >= horizon:
                continue
            add(Arc(robot.id, location, location, tick, tick + 1, "wait"))
            for destination, facility in facilities.items():
                check_budget()
                choices = (
                    [(None, None)] if location == destination else routes.get((robot.id, location, destination), [])
                )
                for route_index, route in choices:
                    if (
                        route
                        and route.cabins
                        and not settings.joint_cabins
                        and len({c.cabin for c in route.cabins}) != len(route.cabins)
                    ):
                        continue
                    travel = math.ceil(route.seconds / step - 1e-9) if route else 0
                    distance = route.distance_m if route else 0
                    uses = (
                        tuple(
                            Use(
                                u.resource,
                                u.unit,
                                u.amount,
                                tick + math.floor(u.start / step + 1e-9),
                                tick + math.ceil(u.end / step - 1e-9),
                            )
                            for u in route.uses
                        )
                        if route
                        else ()
                    )
                    cabins = tuple(c.cabin for c in route.cabins) if route else ()
                    if route:
                        uses += tuple(
                            Use(
                                "cabin:" + c.cabin,
                                "slot",
                                1,
                                tick + math.floor(c.start / step + 1e-9),
                                max(
                                    tick + math.floor(c.start / step + 1e-9) + 1, tick + math.ceil(c.end / step - 1e-9)
                                ),
                            )
                            for c in route.cabins
                        )
                    if facility.kind == "work":
                        duration = max(1, math.ceil(facility.nominal_seconds / step - 1e-9))
                        end = tick + travel + duration
                        consumption = energy_up(
                            decimal(distance) * decimal(robot.drain)
                            + decimal(duration) * decimal(step) * decimal(snapshot.work_drain)
                        )
                        service_uses = (*uses, Use("work:" + destination, "slot", 1, tick + travel, end))
                        for job in snapshot.jobs:
                            if (
                                destination not in job.stations
                                or job.operation != facility.operation
                                or facility.dedicated_order not in (None, job.order_id)
                                or robot.emergency_order not in (None, job.order_id)
                            ):
                                continue
                            add(
                                Arc(
                                    robot.id,
                                    location,
                                    destination,
                                    tick,
                                    end,
                                    "service",
                                    consumption,
                                    job.uid,
                                    route_index,
                                    tick + travel,
                                    service_uses,
                                    cabins,
                                )
                            )
                    elif facility.kind == "charge" and gain_limit(0, step, facility.charge_rate) > 0:
                        end = tick + travel + 1
                        add(
                            Arc(
                                robot.id,
                                location,
                                destination,
                                tick,
                                end,
                                "charge",
                                energy_up(decimal(distance) * decimal(robot.drain)),
                                route=route_index,
                                service_start=tick + travel,
                                uses=(*uses, Use("charge:" + destination, "slot", 1, tick + travel, end)),
                                cabins=cabins,
                            )
                        )
        states_by_robot[robot.id] = seen
    # Arc variables are a contiguous prefix; all auxiliary indices follow them.
    for job in snapshot.jobs:
        v = model.variable("defer:" + job.uid, 0, 1)
        model.deferred[job.uid] = v
        model.objective1[v] = job.weight
        model.hint[v] = 1
        model.row({**{a: 1 for a in jobs[job.uid]}, v: 1}, 1, 1)
    terminal = max((arc.end for arc in model.arcs), default=horizon)
    model.terminal_tick = terminal
    for robot in snapshot.robots:
        if not robot.available:
            continue
        source = initial_location(robot, data)
        sinks = []
        for location, tick in sorted(states_by_robot[robot.id]):
            terms = {v: 1 for v in outgoing[robot.id, location, tick]}
            for v in incoming[robot.id, location, tick]:
                terms[v] = terms.get(v, 0) - 1
            if tick >= horizon:
                sink = model.variable(f"sink:{robot.id}:{location}:{tick}", 0, 1)
                sinks.append((location, tick, sink))
                model.sinks[robot.id, location, tick] = sink
                terms[sink] = 1
                if location == source and tick == horizon:
                    model.hint[sink] = 1
            supply = int(location == source and tick == 0)
            model.row(terms, supply, supply)
        model.row({v: 1 for _, _, v in sinks}, 1, 1)
        battery = [model.variable(f"B:{robot.id}:{tick}", 0, robot.capacity) for tick in range(terminal + 1)]
        model.batteries[robot.id] = tuple(battery)
        for v in battery:
            model.hint[v] = robot.battery
        model.row({battery[0]: 1}, robot.battery, robot.battery)
        gains_at = defaultdict(list)
        for v in robot_arcs[robot.id]:
            arc = model.arcs[v]
            if arc.kind == "charge":
                maximum = gain_limit(0, step, facilities[arc.destination].charge_rate)
                gain = model.variable(f"gain:{v}", 0, maximum)
                model.gain[v] = gain
                gains_at[arc.end - 1].append(gain)
                model.row({gain: 1, v: -maximum}, None, 0)
                model.objective2[gain] = 1
        for tick in range(terminal):
            terms = {battery[tick + 1]: 1, battery[tick]: -1}
            for v, consumption in energies[robot.id, tick]:
                terms[v] = consumption
            for gain in gains_at[tick]:
                terms[gain] = -1
            model.row(terms, 0, 0)

        def required_return(location: str) -> int | None:
            choices = [
                0
                if location == f.id
                else energy_up(
                    decimal(min(r.distance_m for _, r in routes[robot.id, location, f.id])) * decimal(robot.drain)
                )
                for f in facilities.values()
                if f.kind == "charge" and (location == f.id or (robot.id, location, f.id) in routes)
            ]
            return robot.reserve + min(choices) if choices else None

        for location, tick, sink in sinks:
            if location == source:  # Safe hold at the observed initial state is always available.
                continue
            required = required_return(location)
            model.return_energy[robot.id, location] = required
            model.row({sink: 1}, 0, 0) if required is None else model.row({battery[tick]: 1, sink: -required}, 0, None)
        for v in robot_arcs[robot.id]:
            arc = model.arcs[v]
            if arc.kind == "service":
                required = required_return(arc.destination)
                model.row({v: 1}, 0, 0) if required is None else model.row({battery[arc.end]: 1, v: -required}, 0, None)
    for (resource, tick), variables in sorted(resources.items()):
        coefficients: dict[int, int] = defaultdict(int)
        for v, amount in variables:
            coefficients[v] += amount
        model.row(coefficients, None, calendar.at(resource, tick), "capacity")
    # The comparison control permits one new trip after the observed fixed ride.
    if not settings.joint_cabins:
        for variables in cabin_arcs.values():
            model.row({v: 1 for v in variables}, None, 1)
    else:
        add_cabin_flow(model, deadline)
    jobs_by_uid = {j.uid: j for j in snapshot.jobs}
    for v, arc in enumerate(model.arcs):
        if arc.kind == "service":
            job = jobs_by_uid[arc.job]
            start = snapshot.now + arc.service_start * step
            end = snapshot.now + arc.end * step
            distance = data.routes[arc.route].distance_m if arc.route is not None else 0
            cost = 100 * max(0, start - job.created) + 10 * distance
            if job.deadline is not None:
                cost += 500 * max(0, end - job.deadline)
            model.objective2[v] = max(1, math.ceil(cost) + arc.end)
        elif arc.kind == "charge":
            model.objective2[v] = 100 * (arc.end - arc.start)
    check_budget()
    payload = {
        "version": MODEL_VERSION,
        "input": asdict(data),
        "variables": [asdict(v) for v in model.variables],
        "rows": [asdict(r) for r in model.rows],
        "objectives": [model.objective1, model.objective2],
    }
    for key in ("token", "epoch"):
        payload["input"]["snapshot"].pop(key)
    payload["input"]["snapshot"]["settings"] = {
        key: payload["input"]["snapshot"]["settings"][key] for key in ("horizon", "step", "sigma", "joint_cabins")
    }
    model.fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return model
