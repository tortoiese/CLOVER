"""Run reproducible policy comparisons or replay a saved planning input.

Examples:
    python -m simulation.benchmark_planning --output /tmp/clover.json
    python -m simulation.benchmark_planning --replay /tmp/plan.json --backend cpsat
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from database.seed_data import build_default_config
from simulation.experiment_replay import replay_experiment
from simulation.experiments import POLICIES, run_experiment
from simulation.planning.backend import Backend
from simulation.planning.contracts import Settings
from simulation.planning.replay import export_result, read_snapshot
from simulation.planning.validation import validate
from utils.comparison_models import ComparisonConditions
from utils.config_models import FactoryConfig


def main() -> int:
    """Write a portable evidence artifact without changing production DB or Git."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--factory", type=Path)
    parser.add_argument("--replay", type=Path)
    parser.add_argument("--replay-experiment", type=Path)
    parser.add_argument("--backend", choices=("milp", "cpsat"), default="cpsat")
    parser.add_argument("--duration", type=float, default=60)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--budget", type=float, default=2)
    parser.add_argument("--wall-limit", type=float, default=120)
    parser.add_argument("--scope", choices=("station_charge", "traffic"), default="traffic")
    parser.add_argument("--policies", nargs="+", choices=POLICIES, default=POLICIES)
    args = parser.parse_args()
    if args.replay and args.replay_experiment:
        parser.error("select one replay input")
    if args.replay_experiment:
        result = replay_experiment(args.replay_experiment, args.wall_limit)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        print(json.dumps(result["summaries"], ensure_ascii=False))
        return int(result["run_status"] != "complete")
    if args.replay:
        # Match the process READY contract: library initialization is not solve time.
        if args.backend == "milp":
            from ortools.linear_solver import pywraplp
            pywraplp.Solver.CreateSolver("SCIP")
        else:
            from ortools.sat.python import cp_model
            cp_model.CpModel()
        result = Backend(args.backend).solve(read_snapshot(args.replay))
        export_result(result, args.output)
        print(json.dumps({"status": result.report.status, "validation": validate(result)}, ensure_ascii=False))
        return 0 if result.report.status in ("OPTIMAL", "FEASIBLE") and not validate(result) else 1
    factory = (FactoryConfig.from_dict(json.loads(args.factory.read_text(encoding="utf-8")))
               if args.factory else build_default_config())
    conditions = ComparisonConditions(duration=args.duration, repeats=args.repeats, seed=args.seed,
                                      horizon=args.horizon, replan=args.interval)
    settings = Settings(horizon=args.horizon, interval=args.interval, timeout=args.budget,
                        watchdog=max(3, args.budget + 1), scope=args.scope)
    result = run_experiment(factory, conditions, tuple(args.policies), settings, wall_limit=args.wall_limit)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result["summaries"], ensure_ascii=False, indent=2))
    return int(any(s["failed_runs"] for s in result["summaries"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
