"""Worker commands; import only the selected policy's optional dependencies."""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any


def bootstrap(policy: str) -> dict[str, Any]:
    """Prepare imports/backend construction without solving any factory input."""
    if policy in {"baseline", "optimized"}:
        from simulation.decisions.adapters import propose

        assert callable(propose)
        version = "python-selector"
    elif policy == "assignment_milp_legacy":
        import scipy

        from simulation.milp_scheduler import RollingMilpScheduler

        RollingMilpScheduler()
        version = scipy.__version__
    elif policy == "rh_milp_grid":
        from ortools.linear_solver import pywraplp

        solver = pywraplp.Solver.CreateSolver("SCIP")
        if solver is None:
            raise ImportError("SCIP backend unavailable")
        version = solver.SolverVersion()
    elif policy in {"rh_cpsat_linear", "rh_cpsat_interval", "rh_cpsat_native"}:
        import ortools
        from ortools.sat.python import cp_model

        cp_model.CpModel()
        cp_model.CpSolver()
        version = ortools.__version__
    else:
        raise ValueError("unsupported worker policy")
    return {
        "backend_version": version,
        "bootstrap_kind": "imports-and-backend-construction; no factory solve",
    }


def handle(payload: dict[str, Any], ready_policy: str | None) -> tuple[dict[str, Any], str | None]:
    """Dispatch one RPC, retaining identity even on an unavailable backend."""
    identity = {key: payload[key] for key in ("session", "generation", "request_id", "epoch", "policy")}
    args, command, policy = payload["arguments"], payload["command"], payload["policy"]
    started = time.perf_counter()
    cpu_started = time.process_time()
    try:
        if command == "ready":
            result = bootstrap(policy)
            status, ready_policy = "READY", policy
        elif ready_policy != policy:
            raise ValueError("worker is not ready for this policy")
        elif command == "propose":
            from simulation.decisions.adapters import propose

            result = propose(args["observation"], **args["options"])
            status = "OK"
        elif command == "frozen_grid":
            from simulation.planning.backend import Backend
            from simulation.planning.validation import validate

            kinds = {"rh_milp_grid": "milp", "rh_cpsat_linear": "cpsat_linear"}
            if policy not in kinds:
                raise ValueError("frozen grid comparison supports MILP and CP-SAT linear only")
            backend_started = time.perf_counter()
            backend_cpu_started = time.process_time()
            plan = Backend(kinds[policy]).solve(
                args["model"].input, started=backend_started, model=args["model"], hint=args["hint"],
                budget_seconds=args["budget_seconds"],
            )
            complete_backend_seconds = time.perf_counter() - backend_started
            plan = replace(plan, report=replace(plan.report,
                                               total_seconds=complete_backend_seconds,
                                               worker_cpu_seconds=time.process_time() - backend_cpu_started))
            validation_started = time.perf_counter()
            errors = validate(plan, reconstruct_routes=False) if plan.report.status in {"OPTIMAL", "FEASIBLE"} else ()
            result = {"plan": plan, "validation_errors": errors,
                      "validation_seconds": time.perf_counter() - validation_started}
            status = "OK"
        elif command == "rh":
            adapter_started = time.perf_counter()
            from simulation.planning.backend import Backend
            from simulation.planning.contracts import ALL_RH_BACKENDS
            from simulation.planning.snapshot import prepare

            imported = time.perf_counter()
            snapshot = args["snapshot"]
            data = prepare(snapshot, deadline=payload["started"] + snapshot.settings.timeout)
            prepared = time.perf_counter()
            plan = Backend(ALL_RH_BACKENDS[policy]).solve(data, started=payload["started"])
            from simulation.decisions.adapters import propose

            auxiliary_started = time.perf_counter()
            auxiliary = propose(args["observation"], **args["options"])
            completed = time.perf_counter()
            plan = replace(
                plan,
                report=replace(
                    plan.report,
                    dispatch_to_worker_seconds=started - payload["started"],
                    prepare_seconds=prepared - imported,
                    total_seconds=completed - payload["started"],
                    worker_cpu_seconds=time.process_time() - cpu_started,
                    stage_seconds={
                        "worker_dispatch": started - payload["started"],
                        "backend_adapter_import": imported - adapter_started,
                        "route_prepare": prepared - imported,
                        **plan.report.stage_seconds,
                        "auxiliary_policy": completed - auxiliary_started,
                    },
                ),
            )
            result = {
                "plan": plan,
                "auxiliary": auxiliary,
            }
            status = "OK"
        else:
            raise ValueError("unsupported worker command")
        return (
            {
                **identity,
                "status": status,
                "value": result,
                "worker_seconds": time.perf_counter() - started,
                "dispatch_seconds": started - payload["started"],
            },
            ready_policy,
        )
    except Exception as exc:
        code = "UNAVAILABLE" if isinstance(exc, ImportError) else "ERROR"
        return (
            {
                **identity,
                "status": ("STARTUP_" if command == "ready" else "") + code,
                "value": None,
                "message": f"{type(exc).__name__}: {exc}",
                "worker_seconds": time.perf_counter() - started,
            },
            None if command == "ready" else ready_policy,
        )
