"""Bounded deterministic first-service hints, independent of the native solvers."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass

from simulation.planning.cabin_flow import complete_hint
from simulation.planning.calendar import build_calendar
from simulation.planning.model import Model, initial_location


def objective(values: tuple[int, ...], coefficients: dict[int, int]) -> int:
    """Evaluate a sparse integer objective exactly."""
    return sum(values[v] * coefficient for v, coefficient in coefficients.items())


def check(model: Model, values: tuple[int, ...]) -> None:
    """Check all original integer rows, including those replaced by CP intervals."""
    if len(values) != len(model.variables):
        raise ValueError("MODEL_INVALID: incomplete incumbent")
    for variable, value in zip(model.variables, values, strict=True):
        if type(value) is not int or not variable.lower <= value <= variable.upper:
            raise ValueError("MODEL_INVALID: incumbent variable bounds")
    for row in model.rows:
        value = sum(values[v] * c for v, c in row.terms)
        if (row.lower is not None and value < row.lower) or (row.upper is not None and value > row.upper):
            raise ValueError("MODEL_INVALID: incumbent row")


@dataclass(frozen=True)
class Hint:
    """A checked incumbent and evidence about its bounded construction."""

    values: tuple[int, ...]
    source: str
    trials: int = 0
    expired: bool = False


def work_hint(model: Model, deadline: float) -> Hint:
    """Try at most eight robot orders, assigning one safe first service per robot.

    This is an incumbent generator, not an optimal assignment or charging solver.
    It changes no variables, constraints, objectives or semantic/model hashes.
    """
    safe = tuple(model.hint.get(v, 0) for v in range(len(model.variables)))
    check(model, safe)
    best = Hint(safe, "SAFE_HOLD_HINT")
    settings = model.input.snapshot.settings
    if not settings.warm_start:
        return best
    deadline = min(deadline, time.monotonic() + settings.warm_start_seconds)
    robots = {r.id: r for r in model.input.snapshot.robots if r.available}
    origins = {rid: initial_location(robot, model.input) for rid, robot in robots.items()}
    actor_sinks: dict[str, list[int]] = defaultdict(list)
    for (rid, _, _), index in model.sinks.items():
        actor_sinks[rid].append(index)
    candidates: dict[str, list[int]] = defaultdict(list)
    actor_arcs: dict[str, list[int]] = defaultdict(list)
    for v, arc in enumerate(model.arcs):
        if v % 128 == 0 and time.monotonic() >= deadline:
            return Hint(best.values, best.source, best.trials, True)
        actor_arcs[arc.robot].append(v)
        if arc.kind == "service" and arc.origin == origins[arc.robot]:
            candidates[arc.robot].append(v)
    jobs = {j.uid: j for j in model.input.snapshot.jobs}
    for choices in candidates.values():
        choices.sort(key=lambda v: (-jobs[model.arcs[v].job].weight, model.objective2[v], model.arcs[v].end, v))
    ids = tuple(sorted(robots))
    orders = list(dict.fromkeys((ids[i:] + ids[:i]) for i in range(min(4, len(ids)))))
    orders += [order[::-1] for order in orders if order[::-1] not in orders]
    calendar = build_calendar(model.input)
    horizon = math.floor(settings.horizon / settings.step + 1e-9)
    for order in orders:
        values = list(safe)
        used: dict[tuple[str, int], int] = defaultdict(int)
        cabins: set[str] = set()
        selected_jobs: set[str] = set()
        for rid in order:
            robot = robots[rid]
            for v in candidates[rid]:
                if time.monotonic() >= deadline:
                    break
                arc = model.arcs[v]
                required = model.return_energy.get((rid, arc.destination))
                if arc.job in selected_jobs or required is None or robot.battery - arc.energy < required:
                    continue
                if cabins.intersection(arc.cabins):
                    continue
                first_sources = {}
                for use in model.cabin_trips.get(v, ()):
                    first_sources.setdefault(use.cabin, use.source)
                if any(
                    source
                    != (model.cabin_fixed[cid].destination if cid in model.cabin_fixed else model.cabin_initial[cid])
                    for cid, source in first_sources.items()
                ):
                    continue
                demands: dict[tuple[str, int], int] = defaultdict(int)
                for use in arc.uses:
                    for tick in range(use.start, use.end):
                        demands[use.resource, tick] += use.amount
                if any(used[key] + amount > calendar.at(key[0], key[1]) for key, amount in demands.items()):
                    continue
                # Replace this actor's safe hold by wait -> service -> wait -> sink.
                for index in actor_arcs[rid]:
                    values[index] = 0
                for index in actor_sinks[rid]:
                    values[index] = 0
                for tick in range(arc.start):
                    values[model.waits[rid, arc.origin, tick]] = 1
                values[v] = 1
                for tick in range(arc.end, horizon):
                    values[model.waits[rid, arc.destination, tick]] = 1
                values[model.sinks[rid, arc.destination, max(horizon, arc.end)]] = 1
                for tick, index in enumerate(model.batteries[rid]):
                    values[index] = robot.battery - (arc.energy if tick > arc.start else 0)
                values[model.deferred[arc.job]] = 0
                selected_jobs.add(arc.job)
                cabins.update(arc.cabins)
                for key, amount in demands.items():
                    used[key] += amount
                break
        if not complete_hint(model, values):
            continue
        candidate = tuple(values)
        check(model, candidate)

        def score(x: tuple[int, ...]) -> tuple[int, int]:
            return objective(x, model.objective1), objective(x, model.objective2)

        if score(candidate) < score(best.values):
            best = Hint(candidate, "WORK_WARM_START", best.trials)
        best = Hint(best.values, best.source, best.trials + 1)
        if time.monotonic() >= deadline:
            return Hint(best.values, best.source, best.trials, True)
    return best
