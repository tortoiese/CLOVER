"""Construct a capacity- and energy-feasible incumbent for a time-limited MILP."""

from __future__ import annotations

import math
from collections import defaultdict

from simulation.planning.models import Problem


def greedy_hint(problem: Problem) -> tuple[set[int], dict[int, float], set[int]] | None:
    """Build complete discrete paths to seed SCIP; never change the MILP objective.

    Returns:
        Selected arc indices, charging gains and completed jobs, or None when
        even an all-wait/return path cannot meet the model's reserve rules.
    """
    snapshot = problem.snapshot
    step = snapshot.settings.step
    ticks = math.floor(snapshot.settings.horizon / step + 1e-8)
    robot_map = {r.id: r for r in snapshot.robots if r.ready <= ticks}
    stations = {s.id: s for s in snapshot.stations}
    chargers = {c.id: c for c in snapshot.chargers}
    returns = {(r, loc): e for r, loc, e in problem.return_energy}
    index = {
        (a.robot_id, a.origin, a.destination, a.start, a.kind, a.resource, a.job_id): i
        for i, a in enumerate(problem.arcs)
    }
    use = defaultdict(int, {(resource, t): n for resource, t, n in problem.fixed_usage})
    state = {r.id: (r.location, r.ready, r.battery) for r in robot_map.values()}
    chosen: set[int] = set()
    gains: dict[int, float] = {}
    completed: set[int] = set()
    charge_count = defaultdict(int)

    def wait(rid: str, loc: str, start: int, end: int) -> bool:
        for t in range(start, end):
            i = index.get((rid, loc, loc, t, "wait", "", None))
            if i is None:
                return False
            chosen.add(i)
        return True

    def move(rid: str, loc: str, target: str, t: int):
        if loc == target:
            return None, t, 0.0
        i = index.get((rid, loc, target, t, "move", "", None))
        if i is None:
            return None, ticks + 1, math.inf
        return i, problem.arcs[i].end, problem.arcs[i].energy

    for _ in range(len(snapshot.jobs) + 2 * len(robot_map) + 1):
        best = None
        for rid, robot in robot_map.items():
            loc, t, battery = state[rid]
            for job in snapshot.jobs:
                if job.id in completed or robot.emergency_order is not None and robot.emergency_order != job.order_id:
                    continue
                for sid in job.stations:
                    station = stations.get(sid)
                    if station is None:
                        continue
                    movement, arrival, drain = move(rid, loc, station.location, t)
                    for start in range(arrival, ticks):
                        work = index.get((rid, station.location, station.location, start, "work", sid, job.id))
                        if work is None:
                            continue
                        arc = problem.arcs[work]
                        if (
                            battery - drain - arc.energy
                            < robot.reserve + returns.get((rid, station.location), math.inf) - 1e-7
                        ):
                            break
                        if any(use[sid, k] >= 1 for k in range(start, arc.end)):
                            continue
                        score = ((snapshot.now + arc.end * step - job.created) / job.weight, drain, rid, job.id)
                        if best is None or score < best[0]:
                            best = (score, rid, station.location, movement, arrival, work, drain)
                        break
        if best is not None:
            _, rid, loc, movement, arrival, work, drain = best
            arc = problem.arcs[work]
            if movement is not None:
                chosen.add(movement)
            if not wait(rid, loc, arrival, arc.start):
                return None
            chosen.add(work)
            completed.add(arc.job_id)
            for t in range(arc.start, arc.end):
                use[arc.resource, t] += 1
            state[rid] = (loc, arc.end, state[rid][2] - drain - arc.energy)
            continue

        # If energy prevents all currently feasible work, recharge at most twice per robot.
        charging = None
        if len(completed) < len(snapshot.jobs):
            for rid, robot in robot_map.items():
                loc, t, battery = state[rid]
                if charge_count[rid] >= 2 or battery >= robot.capacity * 0.9 - 1e-7:
                    continue
                for c in snapshot.chargers:
                    movement, arrival, drain = move(rid, loc, c.location, t)
                    if battery - drain < robot.reserve - 1e-7:
                        continue
                    needed = robot.capacity * 0.9 - (battery - drain)
                    count = max(1, math.ceil(needed / (c.rate * step) - 1e-8))
                    for start in range(arrival, ticks - count + 1):
                        ids = [
                            index.get((rid, c.location, c.location, k, "charge", c.id, None))
                            for k in range(start, start + count)
                        ]
                        if any(i is None for i in ids) or any(
                            use[c.id, k] >= c.capacity for k in range(start, start + count)
                        ):
                            continue
                        score = (start + count, drain, rid, c.id)
                        if charging is None or score < charging[0]:
                            charging = (score, rid, c, movement, arrival, start, ids, drain, needed)
                        break
        if charging is None:
            break
        _, rid, charger, movement, arrival, start, ids, drain, needed = charging
        if movement is not None:
            chosen.add(movement)
        if not wait(rid, charger.location, arrival, start):
            return None
        remaining = needed
        for i in ids:
            chosen.add(i)
            arc = problem.arcs[i]
            amount = min(remaining, charger.rate * step)
            gains[i] = amount
            remaining -= amount
            use[charger.id, arc.start] += 1
        charge_count[rid] += 1
        state[rid] = (charger.location, start + len(ids), state[rid][2] - drain + needed)

    for rid, (loc, t, battery) in state.items():
        if battery < robot_map[rid].reserve + returns.get((rid, loc), math.inf) - 1e-7:
            return None
        if not wait(rid, loc, t, ticks):
            return None
        end = index.get((rid, loc, "@sink", ticks, "end", "", None))
        if end is None:
            return None
        chosen.add(end)
    return chosen, gains, completed
