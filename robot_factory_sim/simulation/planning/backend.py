"""Two-phase SCIP and CP-SAT compilers for one portable service-flow model."""

from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from dataclasses import replace

from simulation.planning.calendar import build_calendar
from simulation.planning.contracts import Action, PreparedInput, Report, Result
from simulation.planning.incumbent import check, work_hint
from simulation.planning.model import Model, Row, build_model


def cost(values: tuple[int, ...], objective: dict[int, int]) -> int:
    """Evaluate the exact integer objective of a complete incumbent."""
    return sum(values[v] * c for v, c in objective.items())


def check_incumbent(model: Model, values: tuple[int, ...], extra: Row | None = None) -> None:
    """Check every integer bound and sparse row after native float decoding."""
    check(model, values)
    for row in (extra,) if extra else ():
        value = sum(values[v] * c for v, c in row.terms)
        if (row.lower is not None and value < row.lower) or (row.upper is not None and value > row.upper):
            raise ValueError("MODEL_INVALID: incumbent row")


class Backend:
    """Retain phase-one solutions when the secondary optimization times out."""

    def __init__(self, kind: str = "milp") -> None:
        """Select a compiler for the same canonical service-flow problem."""
        if kind not in {"milp", "cpsat_linear", "cpsat_interval", "cpsat_native"}:
            raise ValueError("unsupported solver backend")
        self.kind = kind
        self.version = ""
        self.profile: dict[str, float] = defaultdict(float)

    def _compile_expired(self, started: float, deadline: float) -> bool:
        if time.monotonic() < deadline:
            return False
        self.profile["compile_seconds"] += time.monotonic() - started
        return True

    def phase(
        self,
        model: Model,
        objective: dict[int, int],
        deadline: float,
        extra: Row | None = None,
        hint: tuple[int, ...] | None = None,
    ) -> tuple[tuple[int, ...] | None, str, float | None]:
        """Compile the chosen representation within one phase deadline."""
        if self.kind != "milp":
            return self._cp(model, objective, deadline, extra, hint)
        return self._mip(model, objective, deadline, extra, hint)

    def _mip(
        self, model: Model, objective: dict[int, int], deadline: float, extra: Row | None, hint: tuple[int, ...] | None
    ) -> tuple[tuple[int, ...] | None, str, float | None]:
        if time.monotonic() >= deadline - 0.002:
            return None, "NO_INCUMBENT_TIMEOUT", None
        before = time.monotonic()
        from ortools.linear_solver import pywraplp

        self.profile["import_seconds"] += time.monotonic() - before
        before = time.monotonic()
        solver = pywraplp.Solver.CreateSolver("SCIP")
        if solver is None:
            raise ImportError("SCIP unavailable")
        self.version = solver.SolverVersion()
        solver.SetNumThreads(1)
        solver.SetSolverSpecificParametersAsString("limits/gap = 0\nrandomization/randomseedshift = 42")
        variables = []
        for i, v in enumerate(model.variables):
            if i % 128 == 0 and self._compile_expired(before, deadline):
                return None, "NO_INCUMBENT_TIMEOUT", None
            variables.append(solver.IntVar(v.lower, v.upper, v.name))
        for i, row in enumerate((*model.rows, *((extra,) if extra else ()))):
            if i % 128 == 0 and self._compile_expired(before, deadline):
                return None, "NO_INCUMBENT_TIMEOUT", None
            target = solver.RowConstraint(
                row.lower if row.lower is not None else -solver.infinity(),
                row.upper if row.upper is not None else solver.infinity(),
                "",
            )
            for v, coefficient in row.terms:
                target.SetCoefficient(variables[v], coefficient)
        target = solver.Objective()
        for v, coefficient in objective.items():
            target.SetCoefficient(variables[v], coefficient)
        target.SetMinimization()
        if hint is not None:
            solver.SetHint(variables, hint)
        self.profile["compile_seconds"] += time.monotonic() - before
        remaining = deadline - time.monotonic()
        if remaining <= 0.002:
            return None, "NO_INCUMBENT_TIMEOUT", None
        solver.SetTimeLimit(max(1, int(remaining * 1000)))
        before = time.monotonic()
        status = solver.Solve()
        self.profile["search_seconds"] += time.monotonic() - before
        names = {
            solver.OPTIMAL: "OPTIMAL",
            solver.FEASIBLE: "FEASIBLE",
            solver.INFEASIBLE: "INFEASIBLE",
            solver.NOT_SOLVED: "NO_INCUMBENT_TIMEOUT",
            solver.ABNORMAL: "ERROR",
            solver.UNBOUNDED: "MODEL_INVALID",
        }
        if status not in (solver.OPTIMAL, solver.FEASIBLE):
            return None, names.get(status, "ERROR"), None
        raw = tuple(v.solution_value() for v in variables)
        if any(not math.isfinite(v) or abs(v - round(v)) > 1e-5 for v in raw):
            raise ValueError("MODEL_INVALID: nonintegral native incumbent")
        values = tuple(round(v) for v in raw)
        bound = target.BestBound()
        return values, names[status], bound if math.isfinite(bound) else None

    def _cp(
        self, model: Model, objective: dict[int, int], deadline: float, extra: Row | None, hint: tuple[int, ...] | None
    ) -> tuple[tuple[int, ...] | None, str, float | None]:
        if time.monotonic() >= deadline - 0.002:
            return None, "NO_INCUMBENT_TIMEOUT", None
        before = time.monotonic()
        import ortools
        from ortools.sat.python import cp_model

        self.profile["import_seconds"] += time.monotonic() - before
        before = time.monotonic()
        self.version = "CP-SAT " + ortools.__version__
        target = cp_model.CpModel()
        variables = []
        for i, v in enumerate(model.variables):
            if i % 128 == 0 and self._compile_expired(before, deadline):
                return None, "NO_INCUMBENT_TIMEOUT", None
            variables.append(
                target.new_bool_var(v.name)
                if (v.lower, v.upper) == (0, 1)
                else target.new_int_var(v.lower, v.upper, v.name)
            )
        for i, row in enumerate((*model.rows, *((extra,) if extra else ()))):
            if i % 128 == 0 and self._compile_expired(before, deadline):
                return None, "NO_INCUMBENT_TIMEOUT", None
            if self.kind == "cpsat_interval" and row.kind == "capacity":
                continue
            expression = cp_model.LinearExpr.weighted_sum(
                [variables[v] for v, _ in row.terms], [c for _, c in row.terms]
            )
            if row.lower is not None:
                target.add(expression >= row.lower)
            if row.upper is not None:
                target.add(expression <= row.upper)
        if self.kind == "cpsat_interval":
            by_robot, by_resource = defaultdict(list), defaultdict(list)
            for v, arc in enumerate(model.arcs):
                if v % 128 == 0 and self._compile_expired(before, deadline):
                    return None, "NO_INCUMBENT_TIMEOUT", None
                by_robot[arc.robot].append(
                    target.new_optional_fixed_size_interval_var(
                        arc.start, arc.end - arc.start, variables[v], f"robot:{v}"
                    )
                )
                for i, use in enumerate(arc.uses):
                    interval = target.new_optional_fixed_size_interval_var(
                        use.start, use.end - use.start, variables[v], f"use:{v}:{i}"
                    )
                    by_resource[use.resource].append((interval, use.amount))
            for intervals in by_robot.values():
                target.add_no_overlap(intervals)
            calendar = build_calendar(model.input)
            for resource, intervals in by_resource.items():
                capacity = calendar.capacities[resource][1]
                for i, (start, end, demand) in enumerate(calendar.blocked(resource, model.terminal_tick)):
                    fixed = target.new_fixed_size_interval_var(start, end - start, f"fixed:{resource}:{i}")
                    intervals.append((fixed, demand))
                if capacity == 1 and all(demand == 1 for _, demand in intervals):
                    target.add_no_overlap([interval for interval, _ in intervals])
                else:
                    target.add_cumulative(
                        [interval for interval, _ in intervals], [demand for _, demand in intervals], capacity
                    )
        target.minimize(cp_model.LinearExpr.weighted_sum([variables[v] for v in objective], list(objective.values())))
        if hint is not None:
            for var, value in zip(variables, hint, strict=True):
                target.add_hint(var, value)
        self.profile["compile_seconds"] += time.monotonic() - before
        remaining = deadline - time.monotonic()
        if remaining <= 0.002:
            return None, "NO_INCUMBENT_TIMEOUT", None
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = remaining
        solver.parameters.num_search_workers = 1
        solver.parameters.random_seed = 42
        solver.parameters.relative_gap_limit = 0
        solver.parameters.absolute_gap_limit = 0
        before = time.monotonic()
        status = solver.solve(target)
        self.profile["search_seconds"] += time.monotonic() - before
        names = {
            cp_model.OPTIMAL: "OPTIMAL",
            cp_model.FEASIBLE: "FEASIBLE",
            cp_model.INFEASIBLE: "INFEASIBLE",
            cp_model.MODEL_INVALID: "MODEL_INVALID",
            cp_model.UNKNOWN: "NO_INCUMBENT_TIMEOUT",
        }
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, names.get(status, "ERROR"), None
        return tuple(solver.value(v) for v in variables), names[status], float(solver.best_objective_bound)

    def solve(self, data: PreparedInput, *, started: float | None = None) -> Result:
        """Use one total budget for building, compilation and both objectives."""
        if self.kind == "cpsat_native":
            from simulation.planning.native import solve_native

            return solve_native(data, started=started)
        started = time.monotonic() if started is None else started
        self.profile.clear()
        self.version = ""
        deadline = started + data.snapshot.settings.timeout
        report = Report(
            "NOT_SOLVED",
            backend=self.kind,
            cabin_scope=(
                "joint cabin state path with certified fixed ride, one rider, geometric route variants"
                if data.snapshot.settings.joint_cabins
                else "at most one compound use per cabin per horizon"
            ),
            representation="optional-fixed-grid-interval" if self.kind == "cpsat_interval" else "integer-linear",
        )
        try:
            if data.snapshot.settings.formulation != "grid":
                raise ValueError("MODEL_INVALID: grid compiler requires grid time domain")
            before = time.monotonic()
            model = build_model(data, deadline)
            from simulation.planning.semantic import semantic_spec_hash

            self.profile["model_seconds"] = time.monotonic() - before
            report = replace(
                report,
                model_hash=(
                    hashlib.sha256((model.fingerprint + ":interval-resource-v1").encode()).hexdigest()
                    if self.kind == "cpsat_interval"
                    else model.fingerprint
                ),
                semantic_hash=model.fingerprint,
                semantic_spec_hash=semantic_spec_hash(data),
                time_unit_seconds=data.snapshot.settings.time_unit,
                variables=len(model.variables),
                constraints=len(model.rows),
                build_seconds=time.monotonic() - started,
            )
            before = time.monotonic()
            hint_record = work_hint(model, deadline)
            hint = hint_record.values
            self.profile["hint_seconds"] = time.monotonic() - before
            report = replace(
                report,
                hint_source=hint_record.source,
                hint_trials=hint_record.trials,
                hint_budget_exhausted=hint_record.expired,
                hint_primary_cost=cost(hint, model.objective1),
                hint_secondary_cost=cost(hint, model.objective2),
            )
            zero_proof = cost(hint, model.objective1) == 0 and all(
                c >= 0 and model.variables[v].lower >= 0 for v, c in model.objective1.items()
            )
            if zero_proof:
                # A valid zero incumbent attains the exact nonnegative lower bound.
                first, status1, bound1 = hint, "OPTIMAL", 0.0
                native1, proof, source1 = "SKIPPED_ZERO_LOWER_BOUND", "NONNEGATIVE_ZERO_INCUMBENT", hint_record.source
            else:
                first, status1, bound1 = self.phase(
                    model, model.objective1, time.monotonic() + max(0, deadline - time.monotonic()) * 0.6, hint=hint
                )
                native1, proof, source1 = status1, "NATIVE" if status1 == "OPTIMAL" else "NONE", "NATIVE_PHASE1"
                if first is not None:
                    check_incumbent(model, first)
                if first is None or (cost(hint, model.objective1), cost(hint, model.objective2)) < (
                    cost(first, model.objective1),
                    cost(first, model.objective2),
                ):
                    if (
                        first is not None
                        and status1 == "OPTIMAL"
                        and cost(hint, model.objective1) < cost(first, model.objective1)
                    ):
                        raise ValueError("MODEL_INVALID: native optimum contradicted by checked incumbent")
                    first = hint
                    status1 = "OPTIMAL" if proof == "NATIVE" else "FEASIBLE"
                    source1 = hint_record.source
            report = replace(report, native_phase1_status=native1, phase1_incumbent_source=source1, phase1_proof=proof)
            check_incumbent(model, first)
            value1 = cost(first, model.objective1)
            lock = Row(tuple(sorted(model.objective1.items())), value1 if status1 == "OPTIMAL" else None, value1)
            second, status2, bound2 = None, "NOT_SOLVED", None
            source, native2 = source1, "NOT_SOLVED"
            try:
                second, status2, bound2 = self.phase(model, model.objective2, deadline, lock, first)
                native2 = status2
                if second is not None:
                    before = time.monotonic()
                    check_incumbent(model, second, lock)
                    self.profile["solution_check_seconds"] += time.monotonic() - before
                    if (cost(second, model.objective1), cost(second, model.objective2)) > (
                        cost(first, model.objective1),
                        cost(first, model.objective2),
                    ):
                        second, status2 = first, "FEASIBLE"
                    else:
                        source = "NATIVE_PHASE2"
            except ImportError:
                raise
            except Exception as exc:
                report = replace(report, message=f"phase2 failed; phase1 retained: {type(exc).__name__}: {exc}")
                status2 = native2 = "ERROR"
                second = None
                bound2 = None
            values = first if second is None else second
            report = replace(
                report,
                status="OPTIMAL" if status1 == status2 == "OPTIMAL" else "FEASIBLE",
                phase1_status=status1,
                phase1_cost=cost(values, model.objective1),
                phase1_bound=bound1,
                phase2_status=status2,
                native_phase2_status=native2,
                phase2_cost=cost(values, model.objective2),
                phase2_bound=bound2,
                solve_seconds=time.monotonic() - started - report.build_seconds,
                total_seconds=time.monotonic() - started,
                backend_version=self.version,
                solution_source=source,
                **self.profile,
            )
            actions = tuple(
                Action(arc, values[model.gain[v]] if v in model.gain else 0)
                for v, arc in enumerate(model.arcs)
                if values[v]
            )
            return Result(data, report, actions, tuple(uid for uid, v in model.deferred.items() if values[v]))
        except ImportError as exc:
            status, message = "UNAVAILABLE", str(exc)
        except TimeoutError as exc:
            status, message = "NO_INCUMBENT_TIMEOUT", str(exc)
        except Exception as exc:
            status = "BUILD_LIMIT" if "BUILD_LIMIT" in str(exc) else "MODEL_INVALID"
            message = f"{type(exc).__name__}: {exc}"
        return Result(
            data,
            replace(report, status=status, message=message, total_seconds=time.monotonic() - started, **self.profile),
        )
