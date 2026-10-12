"""SCIP MILP backend with two phases and incumbent preservation."""

from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import replace
from typing import Any

from simulation.planning.models import (
    Action,
    Plan,
    PlanningBackend,
    Snapshot,
    SolveReport,
    SolveResult,
)
from simulation.planning.problem_builder import SINK, build_problem
from simulation.planning.warm_start import greedy_hint


class MilpBackend(PlanningBackend):
    """Solve nominal time-expanded scheduling without accessing engine objects."""

    name = "milp_rh"

    def solve(self, snapshot: Snapshot) -> SolveResult:
        """Use a total build/solve budget and keep a valid phase-one incumbent."""
        started = time.monotonic()
        try:
            return self._solve(snapshot, started)
        except Exception as exc:  # noqa: BLE001 -- a failed solver must return a recoverable result
            report = SolveReport("ERROR", self.name, time.monotonic() - started, message=str(exc))
            return SolveResult(snapshot.token, report)

    def _solve(self, snapshot: Snapshot, started: float) -> SolveResult:
        from ortools.linear_solver import pywraplp

        problem = build_problem(snapshot)
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if solver is None:
            raise RuntimeError("SCIP backend is unavailable")
        solver.SetNumThreads(1)
        solver.SetSolverSpecificParametersAsString("limits/gap = 0\nlimits/absgap = 0")
        ticks = math.floor(snapshot.settings.horizon / snapshot.settings.step + 1e-8)
        outgoing: dict[tuple[str, str, int], list[Any]] = defaultdict(list)
        incoming: dict[tuple[str, str, int], list[Any]] = defaultdict(list)
        energy: dict[tuple[str, int], list[Any]] = defaultdict(list)
        use: dict[tuple[str, int], list[Any]] = defaultdict(list)
        jobs: dict[int, list[tuple[int, Any]]] = defaultdict(list)
        ends: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        charger_map = {c.id: c for c in snapshot.chargers}
        fixed = {(s, t): n for s, t, n in problem.fixed_usage}
        return_map = {(r, loc): e for r, loc, e in problem.return_energy}
        variables = [solver.BoolVar(f"a{i}") for i in range(len(problem.arcs))]
        gains: dict[int, Any] = {}
        for i, arc in enumerate(problem.arcs):
            var = variables[i]
            outgoing[arc.robot_id, arc.origin, arc.start].append(var)
            incoming[arc.robot_id, arc.destination, arc.end].append(var)
            if arc.kind == "work":
                jobs[arc.job_id].append((i, var))
            if arc.kind in ("work", "charge"):
                for t in range(arc.start, arc.end):
                    use[arc.resource, t].append(var)
            if arc.energy:
                for t in range(arc.start, arc.end):
                    energy[arc.robot_id, t].append(-arc.energy / (arc.end - arc.start) * var)
            if arc.kind == "charge":
                rate = charger_map[arc.resource].rate * snapshot.settings.step
                gain = solver.NumVar(0, rate, f"g{i}")
                solver.Add(gain <= rate * var)
                gains[i] = gain
                energy[arc.robot_id, arc.start].append(gain)
            if arc.kind == "end":
                ends[arc.robot_id].append((arc.origin, var))
        battery: dict[tuple[str, int], Any] = {}
        hint_vars: list[Any] = []
        hint_values: list[float] = []
        for robot in snapshot.robots:
            if robot.ready > ticks:
                continue
            nodes = {key[1:] for key in set(outgoing) | set(incoming) if key[0] == robot.id}
            nodes.add((robot.location, robot.ready))
            for loc, t in nodes:
                rhs = 1 if (loc, t) == (robot.location, robot.ready) else -1 if loc == SINK else 0
                solver.Add(solver.Sum(outgoing[robot.id, loc, t]) - solver.Sum(incoming[robot.id, loc, t]) == rhs)
            for t in range(robot.ready, ticks + 1):
                battery[robot.id, t] = solver.NumVar(robot.reserve, robot.capacity, f"B_{robot.id}_{t}")
            solver.Add(battery[robot.id, robot.ready] == robot.battery)
            for t in range(robot.ready, ticks):
                solver.Add(battery[robot.id, t + 1] == battery[robot.id, t] + solver.Sum(energy[robot.id, t]))
            solver.Add(
                battery[robot.id, ticks]
                >= robot.reserve + solver.Sum(return_map[robot.id, loc] * var for loc, var in ends[robot.id])
            )
        deferred = {j.id: solver.BoolVar(f"f{j.id}") for j in snapshot.jobs}
        for job in snapshot.jobs:
            solver.Add(solver.Sum(var for _, var in jobs[job.id]) + deferred[job.id] == 1)
        capacities = {s.id: 1 for s in snapshot.stations}
        capacities.update({c.id: c.capacity for c in snapshot.chargers})
        for resource, cap in capacities.items():
            for t in range(ticks):
                solver.Add(solver.Sum(use[resource, t]) + fixed.get((resource, t), 0) <= cap)
        # Hint a complete all-wait path only when its terminal reserve is feasible.
        if all(
            r.battery >= r.reserve + return_map.get((r.id, r.location), math.inf)
            for r in snapshot.robots
            if r.ready <= ticks
        ):
            locations = {r.id: r.location for r in snapshot.robots}
            for i, arc in enumerate(problem.arcs):
                chosen = arc.origin == locations[arc.robot_id] and arc.kind in ("wait", "end")
                hint_vars.append(variables[i])
                hint_values.append(float(chosen))
            hint_vars.extend(deferred.values())
            hint_values.extend([1.0] * len(deferred))
            solver.SetHint(hint_vars, hint_values)

        hint = greedy_hint(problem)
        if hint is not None:
            selected, charging, completed = hint
            solver.SetHint(
                [*variables, *gains.values(), *deferred.values()],
                [
                    *(float(i in selected) for i in range(len(variables))),
                    *(charging.get(i, 0.0) for i in gains),
                    *(float(jid not in completed) for jid in deferred),
                ],
            )

        statuses = {
            solver.OPTIMAL: "OPTIMAL",
            solver.FEASIBLE: "FEASIBLE",
            solver.INFEASIBLE: "INFEASIBLE",
            solver.UNBOUNDED: "UNBOUNDED",
            solver.ABNORMAL: "ABNORMAL",
            solver.NOT_SOLVED: "NOT_SOLVED",
        }
        phase1 = solver.Sum(j.weight * deferred[j.id] for j in snapshot.jobs)
        solver.Minimize(phase1)
        remaining = snapshot.settings.timeout - (time.monotonic() - started)
        if remaining <= 0:
            report = SolveReport(
                "NOT_SOLVED",
                self.name,
                time.monotonic() - started,
                solver.NumVariables(),
                solver.NumConstraints(),
                message="Budget used in model build",
            )
            return SolveResult(snapshot.token, report)
        solver.SetTimeLimit(max(1, int(remaining * 0.4 * 1000)))
        status1 = solver.Solve()
        report = SolveReport(
            statuses.get(status1, "NOT_SOLVED"),
            self.name,
            time.monotonic() - started,
            solver.NumVariables(),
            solver.NumConstraints(),
            phase1_status=statuses.get(status1, "NOT_SOLVED"),
        )
        if status1 not in (solver.OPTIMAL, solver.FEASIBLE):
            return SolveResult(snapshot.token, report)
        cost1 = round(sum(j.weight * deferred[j.id].solution_value() for j in snapshot.jobs))
        report = replace(
            report, status="FEASIBLE", phase1_cost=float(cost1), phase1_bound=solver.Objective().BestBound()
        )

        def extract(current_report: SolveReport) -> Plan:
            balances = {r.id: r.battery for r in snapshot.robots}
            route_map = {(r.origin, r.destination): r for r in snapshot.routes}
            actions: list[Action] = []
            selected = sorted(
                (i for i, var in enumerate(variables) if var.solution_value() > 0.5),
                key=lambda i: (problem.arcs[i].robot_id, problem.arcs[i].start),
            )
            for i in selected:
                arc = problem.arcs[i]
                if arc.kind == "end":
                    continue
                gain = gains[i].solution_value() if i in gains else 0.0
                balances[arc.robot_id] += gain - arc.energy
                route = route_map.get((arc.origin, arc.destination))
                actions.append(
                    Action(
                        arc.robot_id,
                        arc.kind,
                        snapshot.now + arc.start * snapshot.settings.step,
                        snapshot.now + arc.end * snapshot.settings.step,
                        arc.origin,
                        arc.destination,
                        arc.resource,
                        arc.job_id,
                        arc.energy,
                        gain,
                        arc.distance,
                        route.points if arc.kind == "move" and route else (),
                        balances[arc.robot_id],
                    )
                )
            omitted = tuple(j.id for j in snapshot.jobs if deferred[j.id].solution_value() > 0.5)
            return Plan(snapshot, tuple(actions), omitted, current_report)

        incumbent = extract(report)
        remaining = snapshot.settings.timeout - (time.monotonic() - started)
        if remaining > 0.005:
            solver.Add(phase1 <= cost1)
            terms = []
            for job in snapshot.jobs:
                for i, var in jobs[job.id]:
                    arc = problem.arcs[i]
                    begin = snapshot.now + arc.start * snapshot.settings.step
                    finish = snapshot.now + arc.end * snapshot.settings.step
                    late = max(0.0, finish - job.deadline) if job.deadline is not None else 0.0
                    terms.append(((begin - job.created + 5.0 * late) / snapshot.settings.horizon) * var)
            terms.extend(
                0.01 * a.distance * snapshot.meters_per_pixel * variables[i]
                for i, a in enumerate(problem.arcs)
                if a.kind == "move"
            )
            terms.extend(0.01 * variables[i] + 0.001 * g for i, g in gains.items())
            solver.Minimize(solver.Sum(terms))
            solver.SetTimeLimit(max(1, int(remaining * 1000)))
            status2 = solver.Solve()
            report = replace(report, phase2_status=statuses.get(status2, "NOT_SOLVED"))
            if status2 in (solver.OPTIMAL, solver.FEASIBLE):
                status = "OPTIMAL" if status1 == solver.OPTIMAL and status2 == solver.OPTIMAL else "FEASIBLE"
                report = replace(
                    report,
                    status=status,
                    phase2_cost=solver.Objective().Value(),
                    phase2_bound=solver.Objective().BestBound(),
                )
                incumbent = extract(report)
            else:
                report = replace(report, status="FEASIBLE", message="Kept phase-one incumbent")
        report = replace(report, seconds=time.monotonic() - started)
        incumbent = replace(incumbent, report=report)
        return SolveResult(snapshot.token, report, incumbent)
