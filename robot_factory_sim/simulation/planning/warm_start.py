"""Common feasible hints: never change an objective or remove a candidate arc."""

from __future__ import annotations

from simulation.planning.contracts import Action
from simulation.planning.model import Model
from simulation.planning.validation import (
    capacity_errors,
    reservations_for,
    return_requirement,
)


def feasible_hint(model: Model) -> tuple[int, ...] | None:
    """Build single-job prefixes and wait suffixes; decline unsupported recovery cases."""
    snapshot = model.snapshot
    n = int(snapshot.settings.horizon // snapshot.settings.step)
    step = snapshot.settings.step
    index = {(a.robot, a.kind, a.origin, a.destination, a.start, a.job): v for v, a in enumerate(model.arcs)}
    names = {v.name: i for i, v in enumerate(model.variables)}
    routes = {(r.robot, r.origin, r.destination): r for r in snapshot.routes}
    values = [0] * len(model.variables)
    used_jobs = set()
    fixed = list(snapshot.fixed)
    capacities = {f.id: f.capacity for f in snapshot.facilities} | {r.id: r.capacity for r in snapshot.resources}
    for robot in snapshot.robots:
        location, tick, selected = robot.location, 0, []
        for job in sorted(snapshot.jobs, key=lambda j: (-j.weight, j.created, j.id)):
            if job.id in used_jobs or robot.charge_only or robot.emergency_order not in (None, job.order_id):
                continue
            candidates = []
            for sid in job.stations:
                move = index.get((robot.id, "move", location, sid, 0, None))
                route = routes.get((robot.id, location, sid))
                if move is None or route is None or route.rides:
                    continue
                work = index.get((robot.id, "work", sid, sid, model.arcs[move].end, job.id))
                if work is None:
                    continue
                back = return_requirement(snapshot, robot.id, sid)
                energy = model.arcs[move].energy + model.arcs[work].energy
                if back is None or robot.battery < energy + robot.reserve + back:
                    continue
                actions = tuple(Action(model.arcs[v], snapshot.now + model.arcs[v].start * step,
                                       snapshot.now + model.arcs[v].end * step) for v in (move, work))
                reservations = reservations_for(actions, snapshot)
                if capacity_errors(reservations, capacities, tuple(fixed)):
                    continue
                candidates.append((model.arcs[work].end, energy, sid, move, work, reservations))
            if candidates:
                _, _, location, move, work, reservations = min(candidates)
                selected = [move, work]
                tick = model.arcs[work].end
                fixed.extend(reservations)
                used_jobs.add(job.id)
                break
        while tick < n:
            wait = index.get((robot.id, "wait", location, location, tick, None))
            if wait is None:
                return None
            selected.append(wait)
            tick = model.arcs[wait].end
        sink = names.get(f"sink:{robot.id}:{location}")
        if sink is None:
            return None
        values[sink] = 1
        for v in selected:
            values[v] = 1
        battery = robot.battery
        starts = {model.arcs[v].start: model.arcs[v].energy for v in selected}
        for tick in range(n + 1):
            values[names[f"B:{robot.id}:{tick}"]] = battery
            battery -= starts.get(tick, 0)
    for job, v in model.deferred.items():
        values[v] = int(job not in used_jobs)
    for v, arc in (model.cabins or {}).items():
        cabin = next(c for c in snapshot.cabins if c.id == arc.cabin)
        if arc.kind == "wait" and arc.origin == cabin.floor:
            values[v] = 1
    for cabin in snapshot.cabins:
        sink = names.get(f"cabin_sink:{cabin.id}:{cabin.floor}")
        if sink is not None:
            values[sink] = 1
    return tuple(values)
