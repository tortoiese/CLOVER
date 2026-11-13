"""Free-start optional intervals on a bounded sequence of visits per robot.

Zero native_actions covers every possible positive-duration action in H.
An explicit smaller bound is a different, reported mathematical scope.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Any

from simulation.planning.cabin_state import ready_state
from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import Action, Arc, PreparedInput, Report, Result, Use
from simulation.planning.model import initial_location
from simulation.planning.profiling import PhaseTimer
from simulation.planning.semantic import semantic_spec_hash
from simulation.planning.units import decimal, energy_up, gain_limit
from simulation.planning.validation import validate


@dataclass(frozen=True)
class Template:
    """A geometric service/charge alternative without an enumerated start time."""

    robot: str
    origin: str
    destination: str
    kind: str
    job: str | None
    route: int | None
    travel: int
    work: int
    energy: int
    uses: tuple[Use, ...]


@dataclass
class Choice:
    """One optional template in a sequence position."""

    template: Template
    presence: Any
    start: Any
    end: Any
    gain: Any


@dataclass(frozen=True)
class TripNode:
    """One possible cabin ride plus immutable robot sequence bounds."""

    presence: Any
    start: Any
    end: Any
    origin: float
    destination: float
    robot: str
    slot: int
    alternative: int
    visit: int
    earliest_end: int
    latest_start: int


def templates(data: PreparedInput, robot: Any) -> list[Template]:
    """Use the same K=1 route variants, rounding, jobs and return-energy screen."""
    snap, unit = data.snapshot, data.snapshot.settings.time_unit
    facilities = {f.id: f for f in snap.facilities if f.active}
    routes = defaultdict(list)
    for index, route in enumerate(data.routes):
        if route.robot == robot.id:
            routes[route.origin, route.destination].append((index, route))
    result = []
    for origin in (initial_location(robot, data), *facilities):
        for destination, facility in facilities.items():
            variants = [(None, None)] if origin == destination else routes.get((origin, destination), [])
            for index, route in variants:
                travel = math.ceil(route.seconds / unit - 1e-9) if route else 0
                distance = route.distance_m if route else 0
                uses = (
                    tuple(
                        Use(
                            u.resource,
                            u.unit,
                            u.amount,
                            math.floor(u.start / unit + 1e-9),
                            math.ceil(u.end / unit - 1e-9),
                        )
                        for u in route.uses
                    )
                    if route
                    else ()
                )
                if route:
                    uses += tuple(
                        Use(
                            "cabin:" + c.cabin,
                            "slot",
                            1,
                            math.floor(c.start / unit + 1e-9),
                            max(math.floor(c.start / unit + 1e-9) + 1, math.ceil(c.end / unit - 1e-9)),
                        )
                        for c in route.cabins
                    )
                if facility.kind == "work":
                    work = max(1, math.ceil(facility.nominal_seconds / unit - 1e-9))
                    energy = energy_up(
                        decimal(distance) * decimal(robot.drain)
                        + decimal(work) * decimal(unit) * decimal(snap.work_drain)
                    )
                    for job in snap.jobs:
                        if (
                            destination in job.stations
                            and facility.operation == job.operation
                            and facility.dedicated_order in (None, job.order_id)
                            and robot.emergency_order in (None, job.order_id)
                        ):
                            result.append(
                                Template(
                                    robot.id, origin, destination, "service", job.uid, index, travel, work, energy, uses
                                )
                            )
                elif gain_limit(0, unit, facility.charge_rate) > 0:
                    result.append(
                        Template(
                            robot.id,
                            origin,
                            destination,
                            "charge",
                            None,
                            index,
                            travel,
                            1,
                            energy_up(decimal(distance) * decimal(robot.drain)),
                            uses,
                        )
                    )
    return result


def impossible_link(before: TripNode, after: TripNode) -> bool:
    """Prune only mutually exclusive alternatives or impossible positive-time order."""
    if before.earliest_end > after.latest_start:
        return True
    if before.robot != after.robot:
        return False
    if after.slot < before.slot:
        return True
    return after.slot == before.slot and (after.alternative != before.alternative or after.visit <= before.visit)


class NativeModel:
    """Optional visit intervals, integer energy and shared resource/cabin paths."""

    def __init__(self, data: PreparedInput, deadline: float, timer: PhaseTimer | None = None) -> None:
        self.timer = timer or PhaseTimer()
        self.timer.start("native_import")
        importing = time.perf_counter()
        import ortools
        from ortools.sat.python import cp_model

        self.cp, self.version = cp_model, "CP-SAT " + ortools.__version__
        self.import_seconds = time.perf_counter() - importing
        self.timer.start("native_setup")
        self.data, self.deadline = data, deadline
        cfg = data.snapshot.settings
        if cfg.formulation != "native":
            raise ValueError("MODEL_INVALID: native compiler requires native time domain")
        cfg.validate()
        self.unit = cfg.time_unit
        self.horizon = math.floor(cfg.horizon / self.unit + 1e-9)
        self.count = min(self.horizon, cfg.native_actions) if cfg.native_actions else self.horizon
        self.terminal = self.horizon + cfg.tail_limit
        self.scale = round(cfg.step / self.unit)
        self.target = cp_model.CpModel()
        self.counters: dict[str, int] = defaultdict(int)
        self.timer.read_counts = self.measurement_counts
        self.choices: list[Choice] = []
        self.primary, self.secondary = {}, {}
        self.hints: dict[int, int] = {}
        self.by_resource = defaultdict(list)
        self.cabin_trips = defaultdict(list)
        self.job_choices = defaultdict(list)
        self.size = 0
        self.calendar = build_calendar(data)
        self.facilities = {f.id: f for f in data.snapshot.facilities if f.active}
        self.jobs = {j.uid: j for j in data.snapshot.jobs}
        self.return_cache: dict[tuple[str, str], int | None] = {}
        self.route_distances: dict[tuple[str, str, str], float] = {}
        for route in data.routes:
            key = route.robot, route.origin, route.destination
            self.route_distances[key] = min(self.route_distances.get(key, math.inf), route.distance_m)
        self.timer.start("native_robot_model")
        for robot in data.snapshot.robots:
            if robot.available:
                self._robot(robot)
        for job in data.snapshot.jobs:
            deferred = self.boolean("defer:" + job.uid, 1)
            self.primary[deferred.index] = job.weight
            self.target.add(sum(self.job_choices[job.uid]) + deferred == 1)
        self.timer.start("native_resource_model")
        self._resources()
        self.timer.start("native_cabin_model")
        self._cabins()
        self.timer.start("native_model_validate")
        self.check_budget()
        error = self.target.validate()
        if error:
            raise ValueError("MODEL_INVALID: " + error)

    def measurement_counts(self) -> dict[str, int]:
        """Return current construction counts even if __init__ exits by exception."""
        return {
            "variables": len(self.target.proto.variables),
            "constraints": len(self.target.proto.constraints),
            **self.counters,
        }

    def check_budget(self, extra: int = 0) -> None:
        """Bound construction before native solve, including circuit edges."""
        self.size += extra
        if self.size > self.data.snapshot.settings.max_arcs:
            raise ValueError("BUILD_LIMIT: native alternative/circuit edge limit exceeded")
        if time.perf_counter() >= self.deadline:
            raise TimeoutError("native construction exhausted decision budget")

    def integer(self, name: str, lower: int, upper: int, hint: int = 0) -> Any:
        """Allocate a bounded variable and a complete safe-hold hint."""
        self.check_budget(1)
        value = self.target.new_int_var(lower, upper, name)
        self.hints[value.index] = hint
        return value

    def boolean(self, name: str, hint: int = 0) -> Any:
        """Allocate a selection literal with a stable index."""
        self.check_budget(1)
        value = self.target.new_bool_var(name)
        self.hints[value.index] = hint
        return value

    def _return(self, robot: Any, location: str) -> int | None:
        self.counters["return_queries"] += 1
        key = robot.id, location
        if self.data.snapshot.settings.native_cache and key in self.return_cache:
            self.counters["return_cache_hits"] += 1
            return self.return_cache[key]
        self.counters["return_cache_misses"] += 1
        if self.data.snapshot.settings.native_cache:
            distances = [
                0 if location == f.id else self.route_distances[robot.id, location, f.id]
                for f in self.facilities.values()
                if f.kind == "charge" and (location == f.id or (robot.id, location, f.id) in self.route_distances)
            ]
            value = robot.reserve + energy_up(decimal(min(distances)) * decimal(robot.drain)) if distances else None
            self.return_cache[key] = value
            return value
        distances = [
            0 if location == f.id else route.distance_m
            for f in self.facilities.values()
            if f.kind == "charge"
            for route in (
                [None]
                if location == f.id
                else [r for r in self.data.routes if (r.robot, r.origin, r.destination) == (robot.id, location, f.id)]
            )
        ]
        return robot.reserve + energy_up(decimal(min(distances)) * decimal(robot.drain)) if distances else None

    def _robot(self, robot: Any) -> None:
        target, terminal, count = self.target, self.terminal, self.count
        alternatives = templates(self.data, robot)
        self.check_budget(len(alternatives) * count)
        locations = [initial_location(robot, self.data), *self.facilities]
        ids = {location: index for index, location in enumerate(locations)}
        location = self.integer(robot.id + ":loc:0", 0, len(ids) - 1)
        battery = self.integer(robot.id + ":B:0", 0, robot.capacity, robot.battery)
        target.add(location == 0)
        target.add(battery == robot.battery)
        previous_end: Any = 0
        previous_active: Any = None
        intervals = []
        active_nodes, end_batteries, destination_nodes = [], [], []
        for slot in range(count):
            self.check_budget()
            label = f"{robot.id}:{slot}"
            active = self.boolean(label + ":active")
            if previous_active is not None:
                target.add(active <= previous_active)
            start = self.integer(label + ":start", 0, terminal)
            end = self.integer(label + ":end", 0, terminal)
            duration = self.integer(label + ":duration", 0, terminal)
            next_location = self.integer(label + ":loc", 0, len(ids) - 1)
            next_battery = self.integer(label + ":B", 0, robot.capacity, robot.battery)
            units = self.integer(label + ":charge_units", 0, self.horizon)
            gain = self.integer(label + ":gain", 0, robot.capacity)
            target.add(start >= previous_end)
            target.add(start < self.horizon).only_enforce_if(active)
            target.add(end == previous_end).only_enforce_if(active.Not())
            target.add(start == previous_end).only_enforce_if(active.Not())
            target.add(next_location == location).only_enforce_if(active.Not())
            target.add(duration == end - start)
            intervals.append(target.new_optional_interval_var(start, duration, end, active, label + ":robot"))
            selections, charging, consumption = [], [], []
            for ti, item in enumerate(alternatives):
                if ti % 32 == 0:
                    self.check_budget()
                presence = self.boolean(label + f":p:{ti}")
                selections.append(presence)
                target.add(location == ids[item.origin]).only_enforce_if(presence)
                target.add(next_location == ids[item.destination]).only_enforce_if(presence)
                if item.travel + item.work - 1 > self.data.snapshot.settings.tail_limit:
                    raise ValueError("BUILD_LIMIT: native service exceeds physical tail limit")
                if item.energy > robot.capacity:
                    target.add(presence == 0)
                consumption.append(presence * item.energy)
                if item.kind == "service":
                    target.add(end == start + item.travel + item.work).only_enforce_if(presence)
                    self.job_choices[item.job].append(presence)
                    work = target.new_optional_fixed_size_interval_var(
                        start + item.travel, item.work, presence, label + f":work:{ti}"
                    )
                    self.by_resource["work:" + item.destination].append((work, 1))
                    required = self._return(robot, item.destination)
                    if required is None:
                        target.add(presence == 0)
                    else:
                        target.add(next_battery >= required).only_enforce_if(presence)
                    self._cost(item, presence, start, end, label + f":cost:{ti}")
                else:
                    charging.append(presence)
                    target.add(units >= 1).only_enforce_if(presence)
                    target.add(end == start + item.travel + units).only_enforce_if(presence)
                    one = self.boolean(label + f":one:{ti}", 1)
                    target.add(units <= 1).only_enforce_if(one)
                    target.add(units >= 2).only_enforce_if(one.Not())
                    target.add(end <= self.horizon).only_enforce_if([presence, one.Not()])
                    rate = gain_limit(0, self.unit, self.facilities[item.destination].charge_rate)
                    target.add(gain <= rate * units).only_enforce_if(presence)
                    work = target.new_optional_interval_var(
                        start + item.travel, units, end, presence, label + f":charge:{ti}"
                    )
                    self.by_resource["charge:" + item.destination].append((work, 1))
                    cost = self.integer(label + f":charge_cost:{ti}", 0, 100 * terminal)
                    target.add(cost == 100 * (item.travel + units)).only_enforce_if(presence)
                    target.add(cost == 0).only_enforce_if(presence.Not())
                    self.secondary[cost.index] = 1
                for ui, use in enumerate(item.uses):
                    interval = target.new_optional_fixed_size_interval_var(
                        start + use.start, use.end - use.start, presence, label + f":use:{ti}:{ui}"
                    )
                    self.by_resource[use.resource].append((interval, use.amount))
                if item.route is not None:
                    for ci, cabin in enumerate(self.data.routes[item.route].cabins):
                        lo = math.floor(cabin.start / self.unit + 1e-9)
                        hi = max(lo + 1, math.ceil(cabin.end / self.unit - 1e-9))
                        self.cabin_trips[cabin.cabin].append(
                            TripNode(
                                presence,
                                start + lo,
                                start + hi,
                                float(cabin.source_floor),
                                float(cabin.destination),
                                robot.id,
                                slot,
                                ti,
                                ci,
                                slot + hi,
                                self.horizon - 1 + lo,
                            )
                        )
                self.choices.append(Choice(item, presence, start, end, gain))
            target.add(sum(selections) == active)
            target.add(units == 0).only_enforce_if([p.Not() for p in charging])
            target.add(gain == 0).only_enforce_if([p.Not() for p in charging])
            target.add(battery >= sum(consumption))
            target.add(next_battery == battery - sum(consumption) + gain)
            self.secondary[gain.index] = self.scale
            active_nodes.append(active)
            end_batteries.append(next_battery)
            destination_nodes.append(next_location)
            previous_active, previous_end, location, battery = active, end, next_location, next_battery
        target.add_no_overlap(intervals)
        for slot, active in enumerate(active_nodes):
            last = self.boolean(f"{robot.id}:{slot}:last")
            target.add(last == active - (active_nodes[slot + 1] if slot + 1 < count else 0))
            for location, index in ids.items():
                if index == 0:
                    continue
                at = self.boolean(f"{robot.id}:{slot}:last_at:{index}")
                target.add(destination_nodes[slot] == index).only_enforce_if(at)
                target.add(destination_nodes[slot] != index).only_enforce_if(at.Not())
                # Inactive safe hold retains location zero, so at's hold hint is false.
                required = self._return(robot, location)
                if required is None:
                    target.add_bool_or([last.Not(), at.Not()])
                else:
                    target.add(end_batteries[slot] >= required).only_enforce_if([last, at])

    def _cost(self, item: Template, presence: Any, start: Any, end: Any, label: str) -> None:
        job, snap = self.jobs[item.job], self.data.snapshot
        distance = self.data.routes[item.route].distance_m if item.route is not None else 0
        base = 100 * max(0, snap.now - job.created) + 10 * distance
        wait = round(100 * self.unit)
        maximum = (
            self.scale
            * math.ceil(
                base
                + 100 * self.unit * self.terminal
                + 500
                * max(
                    0, snap.now + self.unit * self.terminal - (job.deadline if job.deadline is not None else snap.now)
                )
            )
            + self.terminal
            + 1
        )
        cost = self.integer(label, 0, maximum)
        self.target.add(cost >= self.scale * (math.ceil(base) + wait * (start + item.travel)) + end).only_enforce_if(
            presence
        )
        if job.deadline is not None:
            late_base = math.ceil(base + 500 * (snap.now - job.deadline))
            self.target.add(
                cost >= self.scale * (late_base + wait * (start + item.travel) + round(500 * self.unit) * end) + end
            ).only_enforce_if(presence)
        self.target.add(cost == 0).only_enforce_if(presence.Not())
        self.secondary[cost.index] = 1

    def _resources(self) -> None:
        for resource, uses in self.by_resource.items():
            self.check_budget()
            for index, (lo, hi, demand) in enumerate(self.calendar.blocked(resource, self.terminal)):
                uses.append((self.target.new_fixed_size_interval_var(lo, hi - lo, f"fixed:{resource}:{index}"), demand))
            capacity = self.calendar.capacities[resource][1]
            if capacity == 1 and all(demand == 1 for _, demand in uses):
                self.target.add_no_overlap([interval for interval, _ in uses])
            else:
                self.target.add_cumulative([interval for interval, _ in uses], [demand for _, demand in uses], capacity)

    def _cabins(self) -> None:
        cabins = {c.id: c for c in self.data.snapshot.cabins}
        if sum(len(nodes) for nodes in self.cabin_trips.values()) > self.data.snapshot.settings.max_cabin_states:
            raise ValueError("BUILD_LIMIT: native cabin node limit exceeded")
        for cid, nodes in self.cabin_trips.items():
            source, ready = ready_state(cabins[cid], self.data.snapshot.now, self.unit)
            empty = self.boolean("cabin_empty:" + cid, 1)
            arcs = [(0, 0, empty)]
            self.target.add(sum(n.presence for n in nodes) == 0).only_enforce_if(empty)
            self.target.add(sum(n.presence for n in nodes) >= 1).only_enforce_if(empty.Not())
            if not self.data.snapshot.settings.joint_cabins:
                self.target.add(sum(n.presence for n in nodes) <= 1)
            by_origin = defaultdict(list)
            for j, node in enumerate(nodes, 1):
                by_origin[node.origin].append((j, node))
            for i, node in enumerate(nodes, 1):
                self.check_budget()
                arcs.append((i, i, node.presence.Not()))
                if node.origin == source:
                    first = self.boolean(f"cabin:{cid}:0:{i}")
                    arcs.append((0, i, first))
                    self.target.add(node.start >= ready).only_enforce_if(first)
                    self.check_budget(1)
                last = self.boolean(f"cabin:{cid}:{i}:0")
                arcs.append((i, 0, last))
                self.check_budget(1)
                for j, following in by_origin[node.destination]:
                    if i == j:
                        continue
                    self.counters["cabin_pairs_considered"] += 1
                    if self.data.snapshot.settings.native_pruning and impossible_link(node, following):
                        self.counters["cabin_links_pruned"] += 1
                        continue
                    self.check_budget(1)
                    link = self.boolean(f"cabin:{cid}:{i}:{j}")
                    arcs.append((i, j, link))
                    self.target.add(following.start >= node.end).only_enforce_if(link)
                    self.counters["cabin_links_kept"] += 1
            self.target.add_circuit(arcs)

    def expression(self, coefficients: dict[int, int]) -> Any:
        """Build an exact integer objective using stable variable indices."""
        return sum(self.target.get_int_var_from_proto_index(i) * c for i, c in coefficients.items())

    def decode(self, values: tuple[int, ...], report: Report) -> Result:
        """Fill free waiting gaps explicitly for the shared independent validator."""
        grouped = defaultdict(list)
        for choice in self.choices:
            if not values[choice.presence.index]:
                continue
            t = choice.template
            start, end = values[choice.start.index], values[choice.end.index]
            uses = tuple(replace(u, start=start + u.start, end=start + u.end) for u in t.uses)
            uses += (
                Use(("work:" if t.kind == "service" else "charge:") + t.destination, "slot", 1, start + t.travel, end),
            )
            cabins = tuple(c.cabin for c in self.data.routes[t.route].cabins) if t.route is not None else ()
            grouped[t.robot].append(
                Action(
                    Arc(
                        t.robot,
                        t.origin,
                        t.destination,
                        start,
                        end,
                        t.kind,
                        t.energy,
                        t.job,
                        t.route,
                        start + t.travel,
                        uses,
                        cabins,
                    ),
                    values[choice.gain.index] if t.kind == "charge" else 0,
                )
            )
        actions = []
        for robot in self.data.snapshot.robots:
            if not robot.available:
                continue
            location, at = initial_location(robot, self.data), 0
            for action in sorted(grouped[robot.id], key=lambda a: a.arc.start):
                if action.arc.start > at:
                    actions.append(Action(Arc(robot.id, location, location, at, action.arc.start, "wait")))
                actions.append(action)
                location, at = action.arc.destination, action.arc.end
            if at < self.horizon:
                actions.append(Action(Arc(robot.id, location, location, at, self.horizon, "wait")))
        deferred = tuple(j.uid for j in self.data.snapshot.jobs if not any(a.arc.job == j.uid for a in actions))
        result = Result(self.data, report, tuple(actions), deferred)
        errors = validate(result)
        if errors:
            raise ValueError("MODEL_INVALID: native independent validation: " + "; ".join(errors))
        return result


def solve_native(data: PreparedInput, *, started: float | None = None) -> Result:
    """Compile and solve two objectives under the same parent-enforced total budget."""
    started = time.perf_counter() if started is None else started
    deadline = started + data.snapshot.settings.timeout
    timer = PhaseTimer()
    report = Report(
        "NOT_SOLVED",
        backend="cpsat_native",
        representation="free-start-optional-interval",
        time_unit_seconds=data.snapshot.settings.time_unit,
        cabin_scope="circuit floor continuity after certified fixed ride; one rider",
    )
    try:
        built = NativeModel(data, deadline, timer)
        model_time = time.perf_counter() - started
        timer.start("native_fingerprint")
        target = built.target
        fingerprint = hashlib.sha256(
            (str(target.proto) + str(built.primary) + str(built.secondary)).encode()
        ).hexdigest()
        report = replace(
            report,
            model_hash=fingerprint,
            semantic_hash=fingerprint,
            semantic_spec_hash=semantic_spec_hash(data),
            secondary_scale=built.scale,
            action_scope="complete" if built.count == built.horizon else f"at most {built.count} visits per robot",
            variables=len(target.proto.variables),
            constraints=len(target.proto.constraints),
            build_seconds=model_time,
            model_seconds=model_time,
            import_seconds=built.import_seconds,
            compile_seconds=max(0, model_time - built.import_seconds),
            backend_version=built.version,
            hint_source="SAFE_HOLD_HINT",
            hint_primary_cost=sum(j.weight for j in data.snapshot.jobs),
            hint_secondary_cost=0,
        )
        variables = [target.get_int_var_from_proto_index(i) for i in range(len(target.proto.variables))]
        safe = tuple(built.hints.get(i, 0) for i in range(len(variables)))
        # A physically validated safe hold is retained if native search finds no incumbent.
        timer.start("native_incumbent_validate")
        built.decode(safe, replace(report, status="FEASIBLE"))
        search_seconds = 0.0

        def phase(objective: dict[int, int], until: float, hint: tuple[int, ...]) -> tuple[Any, str, float | None]:
            nonlocal search_seconds
            phase_name = "native_primary" if objective is built.primary else "native_secondary"
            timer.start(phase_name + "_setup")
            target.minimize(built.expression(objective))
            target.clear_hints()
            if data.snapshot.settings.warm_start:
                for variable, value in zip(variables, hint, strict=True):
                    target.add_hint(variable, value)
            remaining = until - time.perf_counter()
            if remaining <= 0.002:
                return None, "NO_INCUMBENT_TIMEOUT", None
            solver = built.cp.CpSolver()
            solver.parameters.num_search_workers = 1
            solver.parameters.random_seed = 42
            solver.parameters.max_time_in_seconds = remaining
            solver.parameters.relative_gap_limit = 0
            solver.parameters.absolute_gap_limit = 0
            searching = time.perf_counter()
            timer.start(phase_name + "_search")
            status = solver.solve(target)
            search_seconds += time.perf_counter() - searching
            names = {
                built.cp.OPTIMAL: "OPTIMAL",
                built.cp.FEASIBLE: "FEASIBLE",
                built.cp.UNKNOWN: "NO_INCUMBENT_TIMEOUT",
                built.cp.MODEL_INVALID: "MODEL_INVALID",
                built.cp.INFEASIBLE: "INFEASIBLE",
            }
            if status not in (built.cp.OPTIMAL, built.cp.FEASIBLE):
                return None, names.get(status, "ERROR"), None
            return tuple(solver.value(v) for v in variables), names[status], solver.best_objective_bound

        def score(values: tuple[int, ...], objective: dict[int, int]) -> int:
            return sum(values[i] * c for i, c in objective.items())

        first, s1, b1 = phase(built.primary, time.perf_counter() + max(0, deadline - time.perf_counter()) * 0.6, safe)
        if s1 in {"MODEL_INVALID", "INFEASIBLE", "ERROR"}:
            raise ValueError("MODEL_INVALID: native primary solve: " + s1)
        first_source = "SAFE_HOLD_HINT" if first is None else "NATIVE_PHASE1"
        first = safe if first is None else first
        cost1 = score(first, built.primary)
        target.add(built.expression(built.primary) == cost1) if s1 == "OPTIMAL" else target.add(
            built.expression(built.primary) <= cost1
        )
        try:
            second, s2, b2 = phase(built.secondary, deadline, first)
        except Exception as exc:
            second, s2, b2 = None, "ERROR", None
            report = replace(report, message=f"phase2 failed; phase1 retained: {type(exc).__name__}: {exc}")
        values = (
            second
            if second is not None
            and (score(second, built.primary), score(second, built.secondary)) <= (cost1, score(first, built.secondary))
            else first
        )
        report = replace(
            report,
            status="OPTIMAL" if s1 == s2 == "OPTIMAL" else "FEASIBLE",
            phase1_status="OPTIMAL" if s1 == "OPTIMAL" else "FEASIBLE",
            native_phase1_status=s1,
            phase1_proof="NATIVE" if s1 == "OPTIMAL" else "NONE",
            phase1_incumbent_source=first_source,
            phase1_cost=score(values, built.primary),
            phase1_bound=b1,
            phase2_status=s2,
            native_phase2_status=s2,
            phase2_cost=score(values, built.secondary),
            phase2_bound=b2,
            total_seconds=time.perf_counter() - started,
            solve_seconds=time.perf_counter() - started - model_time,
            solution_source="NATIVE_PHASE2" if values is second else "NATIVE_PHASE1_OR_SAFE_HOLD",
            search_seconds=search_seconds,
        )
        timer.start("native_solution_validate")
        result = built.decode(values, report)
        return replace(result, report=profile_report(result.report, timer, started, True))
    except ImportError as exc:
        status, message = "UNAVAILABLE", str(exc)
    except TimeoutError as exc:
        status, message = "NO_INCUMBENT_TIMEOUT", str(exc)
    except Exception as exc:
        status, message = (
            ("BUILD_LIMIT" if "BUILD_LIMIT" in str(exc) else "MODEL_INVALID"),
            f"{type(exc).__name__}: {exc}",
        )
    return Result(data, profile_report(replace(report, status=status, message=message), timer, started, False))


def profile_report(report: Report, timer: PhaseTimer, started: float, complete: bool) -> Report:
    """Finalize exclusive times and partial counts on both success and failure."""
    timer.stop()
    stages = timer.snapshot()
    counts = timer.read_counts() if timer.read_counts is not None else {}
    timer.read_counts = None  # Drop the callback's reference to the large native model.
    build_names = {
        "native_import",
        "native_setup",
        "native_robot_model",
        "native_resource_model",
        "native_cabin_model",
        "native_model_validate",
        "native_fingerprint",
    }
    build = sum(value for name, value in stages.items() if name in build_names)
    search = sum(value for name, value in stages.items() if name.endswith("_search"))
    checks = sum(stages.get(name, 0.0) for name in ("native_incumbent_validate", "native_solution_validate"))
    return replace(
        report,
        variables=counts.get("variables", report.variables),
        constraints=counts.get("constraints", report.constraints),
        timing_stage=timer.last,
        timing_complete=complete,
        stage_seconds=stages,
        work_counters=counts,
        import_seconds=stages.get("native_import", 0.0),
        build_seconds=build,
        model_seconds=max(0.0, build - stages.get("native_import", 0.0)),
        compile_seconds=max(0.0, build - stages.get("native_import", 0.0)),
        solve_seconds=sum(
            value for name, value in stages.items() if name.startswith(("native_primary", "native_secondary"))
        ),
        search_seconds=search,
        solution_check_seconds=checks,
        total_seconds=time.perf_counter() - started,
    )
