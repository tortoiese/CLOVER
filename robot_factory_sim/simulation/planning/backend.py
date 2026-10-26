"""MILP/SCIP and CP-SAT adapters for the same two-phase integer program."""

from __future__ import annotations

import time
from dataclasses import replace

from simulation.planning.contracts import Action, Report, Result, Snapshot
from simulation.planning.model import Model, Row, build_model
from simulation.planning.warm_start import feasible_hint


def _cost(values: tuple[int, ...], objective: dict[int, int]) -> int:
    return sum(values[v] * c for v, c in objective.items())


def _check(model: Model, values: tuple[int, ...], extra: Row | None = None) -> None:
    for v, value in zip(model.variables, values, strict=True):
        if value < v.lower or value > v.upper:
            raise ValueError(f"invalid incumbent bound: {v.name}")
    for row in [*model.rows, *([extra] if extra else [])]:
        value = sum(values[v] * c for v, c in row.terms)
        if row.lower is not None and value < row.lower or row.upper is not None and value > row.upper:
            raise ValueError("invalid incumbent linear constraint")


class Backend:
    """Two-phase minimization with phase-one incumbent retention on timeout."""

    def __init__(self, kind: str = "milp", *, use_hint: bool = True) -> None:
        if kind not in ("milp", "cpsat"):
            raise ValueError(kind)
        self.kind = kind
        self.solver_version = ""
        self.use_hint = use_hint

    def solve(self, snapshot: Snapshot, started: float | None = None) -> Result:
        """Include building, compilation and both solves in one wall-clock budget."""
        started = time.monotonic() if started is None else started
        deadline = started + snapshot.settings.timeout
        report = Report(self.kind, "NOT_SOLVED", 0, scope=snapshot.scope)
        try:
            model = build_model(snapshot, deadline)
            report = replace(report, model_hash=model.fingerprint, variables=len(model.variables),
                             constraints=len(model.rows), build_seconds=time.monotonic() - started)
            phase1_started = time.monotonic()
            hint = feasible_hint(model) if self.use_hint else None
            if hint is not None:
                try:
                    _check(model, hint)
                except ValueError:
                    hint = None
            if hint is not None and all(c >= 0 for c in model.objective1.values()) and _cost(hint, model.objective1) == 0:
                # Every defer weight is nonnegative. A feasible zero value meets
                # the exact mathematical lower bound; no solver proof is needed.
                first, status1, bound1 = hint, "OPTIMAL", 0.0
                report = replace(report, phase1_proof="NONNEGATIVE_ZERO_WITNESS")
            else:
                first, status1, bound1 = self._phase(model, model.objective1, deadline, fraction=.55, hint=hint)
                if hint is not None and (first is None or _cost(hint, model.objective1) < _cost(first, model.objective1)):
                    first, status1 = hint, "FEASIBLE"
            report = replace(report, phase1_seconds=time.monotonic() - phase1_started,
                             backend_version=self.solver_version, hint_used=hint is not None)
            if first is None:
                return Result(snapshot, replace(report, status=status1, phase1_status=status1,
                                                phase1_bound=bound1, seconds=time.monotonic() - started))
            _check(model, first)
            cost1 = _cost(first, model.objective1)
            lock = Row(tuple(sorted(model.objective1.items())), cost1 if status1 == "OPTIMAL" else None, cost1)
            phase2_started = time.monotonic()
            try:
                second, status2, bound2 = self._phase(model, model.objective2, deadline, extra=lock, hint=first)
                if second is not None:
                    _check(model, second, lock)
                    if (_cost(first, model.objective1), _cost(first, model.objective2)) < (
                            _cost(second, model.objective1), _cost(second, model.objective2)):
                        second, status2 = first, "FEASIBLE"
            except ImportError:
                raise
            except Exception as exc:  # noqa: BLE001 — retain the validated first incumbent
                second, status2, bound2 = None, "ERROR", None
                report = replace(report, message=f"phase2 failed; phase1 incumbent retained: {exc}")
            values = second if second is not None else first
            status = "OPTIMAL" if status1 == status2 == "OPTIMAL" else "FEASIBLE"
            report = replace(report, status=status, seconds=time.monotonic() - started,
                             phase2_seconds=time.monotonic() - phase2_started, backend_version=self.solver_version,
                             phase1_status=status1, phase1_cost=_cost(values, model.objective1), phase1_bound=bound1,
                             phase2_status=status2, phase2_cost=_cost(values, model.objective2), phase2_bound=bound2)
            actions = tuple(Action(arc, snapshot.now + arc.start * snapshot.settings.step,
                                   snapshot.now + arc.end * snapshot.settings.step,
                                   values[model.gain[v]] if v in model.gain else 0, f"{snapshot.token}:a{v}")
                            for v, arc in enumerate(model.arcs) if values[v])
            deferred = tuple(job for job, v in model.deferred.items() if values[v])
            cabins = tuple(arc for v, arc in (model.cabins or {}).items() if values[v])
            return Result(snapshot, report, actions, deferred, cabins)
        except (ImportError, ModuleNotFoundError) as exc:
            status = "UNAVAILABLE"
            message = f"Install requirements (ortools/SCIP): {exc}"
        except TimeoutError as exc:
            status = "TIMEOUT"
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 — isolate third-party native solver failures
            status = "ERROR"
            message = str(exc)
            if "GRAPH_LIMIT" in str(exc):
                status = "GRAPH_LIMIT"
        return Result(snapshot, replace(report, status=status, seconds=time.monotonic() - started,
                                        message=message))

    def _phase(self, model: Model, objective: dict[int, int], deadline: float, fraction: float = 1.0,
               extra: Row | None = None, hint: tuple[int, ...] | None = None,
               ) -> tuple[tuple[int, ...] | None, str, float | None]:
        remaining = (deadline - time.monotonic()) * fraction
        if remaining <= .002:
            return None, "TIMEOUT", None
        if self.kind == "cpsat":
            return self._cp(model, objective, time.monotonic() + remaining, extra, hint)
        return self._mip(model, objective, time.monotonic() + remaining, extra, hint)

    def _mip(self, model: Model, objective: dict[int, int], deadline: float, extra: Row | None,
             hint: tuple[int, ...] | None) -> tuple[tuple[int, ...] | None, str, float | None]:
        from ortools.linear_solver import pywraplp
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if solver is None:
            raise ImportError("SCIP backend unavailable")
        self.solver_version = solver.SolverVersion()
        solver.SetNumThreads(1)
        solver.SetSolverSpecificParametersAsString("limits/gap = 0\nrandomization/randomseedshift = 42")
        variables = [solver.IntVar(v.lower, v.upper, v.name) for v in model.variables]
        for row in [*model.rows, *([extra] if extra else [])]:
            target = solver.RowConstraint(row.lower if row.lower is not None else -solver.infinity(),
                                          row.upper if row.upper is not None else solver.infinity(), "")
            for v, coefficient in row.terms:
                target.SetCoefficient(variables[v], coefficient)
        target = solver.Objective()
        for v, coefficient in objective.items():
            target.SetCoefficient(variables[v], coefficient)
        target.SetMinimization()
        if hint:
            solver.SetHint(variables, hint)
        remaining = deadline - time.monotonic()
        if remaining <= .002:
            return None, "TIMEOUT", None
        solver.SetTimeLimit(max(1, int(remaining * 1000)))
        status = solver.Solve()
        names = {solver.OPTIMAL: "OPTIMAL", solver.FEASIBLE: "FEASIBLE", solver.INFEASIBLE: "INFEASIBLE",
                 solver.NOT_SOLVED: "TIMEOUT", solver.ABNORMAL: "ERROR", solver.UNBOUNDED: "ERROR"}
        feasible = status in (solver.OPTIMAL, solver.FEASIBLE)
        values = tuple(round(v.solution_value()) for v in variables) if feasible else None
        return values, names.get(status, "ERROR"), target.BestBound() if feasible else None

    def _cp(self, model: Model, objective: dict[int, int], deadline: float, extra: Row | None,
            hint: tuple[int, ...] | None) -> tuple[tuple[int, ...] | None, str, float | None]:
        import ortools
        from ortools.sat.python import cp_model
        self.solver_version = "CP-SAT " + ortools.__version__
        target = cp_model.CpModel()
        variables = [target.new_int_var(v.lower, v.upper, v.name) for v in model.variables]
        for row in [*model.rows, *([extra] if extra else [])]:
            expression = cp_model.LinearExpr.weighted_sum([variables[v] for v, _ in row.terms],
                                                          [c for _, c in row.terms])
            if row.lower == row.upper and row.lower is not None:
                target.add(expression == row.lower)
            else:
                if row.lower is not None:
                    target.add(expression >= row.lower)
                if row.upper is not None:
                    target.add(expression <= row.upper)
        target.minimize(cp_model.LinearExpr.weighted_sum([variables[v] for v in objective], list(objective.values())))
        if hint:
            for v, value in zip(variables, hint, strict=True):
                target.add_hint(v, value)
        remaining = deadline - time.monotonic()
        if remaining <= .002:
            return None, "TIMEOUT", None
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 42
        solver.parameters.relative_gap_limit = 0
        solver.parameters.absolute_gap_limit = 0
        status = solver.solve(target)
        names = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE", cp_model.INFEASIBLE: "INFEASIBLE",
                 cp_model.UNKNOWN: "TIMEOUT", cp_model.MODEL_INVALID: "ERROR"}
        feasible = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        values = tuple(solver.value(v) for v in variables) if feasible else None
        return values, names.get(status, "ERROR"), solver.best_objective_bound if feasible else None


def solve_payload(snapshot: Snapshot, kind: str) -> Result:
    """Process entry point; imports and native solver state stay in the child."""
    return Backend(kind).solve(snapshot)
