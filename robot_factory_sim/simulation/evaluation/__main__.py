"""Command-line validation, paired suite execution and exact run replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import replay, run_case, run_suite
from simulation.evaluation.scenario import FOUNDATION_POLICIES, POLICIES, fingerprint, read_scenario


def main(argv: list[str] | None = None) -> int:
    """Execute explicit CLI actions, returning nonzero for invalid/partial outcomes."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("examples", help="write six editable scenario JSON files")
    generate.add_argument("--output", type=Path, required=True)
    validate = commands.add_parser("validate", help="check scenario units and physical/model support")
    validate.add_argument("--scenario", type=Path, required=True)
    single = commands.add_parser("run", help="run one policy/seed")
    single.add_argument("--scenario", type=Path, required=True)
    single.add_argument("--policy", choices=POLICIES, default="optimized")
    single.add_argument("--seed", type=int, default=1001)
    single.add_argument("--output", type=Path, required=True)
    single.add_argument("--wall-limit", type=float, default=60)
    single.add_argument("--rh-settings", type=Path)
    single.add_argument("--decision-settings",type=Path)
    suite = commands.add_parser("suite", help="run paired cases; defaults to the six foundation examples")
    suite.add_argument("--scenario", type=Path, nargs="+")
    suite.add_argument("--seeds", type=int, nargs="+", default=[1001, 1002, 1003])
    suite.add_argument("--policies", choices=POLICIES, nargs="+", default=list(FOUNDATION_POLICIES))
    suite.add_argument("--rh-settings", type=Path)
    suite.add_argument("--decision-settings",type=Path)
    suite.add_argument("--duration", type=float)
    suite.add_argument("--run-wall-limit", type=float, default=60)
    suite.add_argument("--suite-wall-limit", type=float)
    suite.add_argument("--output", type=Path, required=True)
    repeat = commands.add_parser("replay", help="replay a saved run with its exact trace and source hash")
    repeat.add_argument("--run", type=Path, required=True)
    inspect = commands.add_parser("analyze", help="rebuild resource metrics from one saved raw stream")
    inspect.add_argument("--run", type=Path, required=True)
    inspect.add_argument("--events", type=Path)
    inspect.add_argument("--output", type=Path)
    timing = commands.add_parser("profile", help="summarize timing/fallback records without mixing budgets or modes")
    timing.add_argument("--manifest", type=Path, required=True)
    timing.add_argument("--output", type=Path)
    prepare = commands.add_parser("prepare-design", help="freeze design cells as executable reference scenarios")
    prepare.add_argument("--design", type=Path, required=True)
    reference = prepare.add_mutually_exclusive_group(required=True)
    reference.add_argument("--synthetic-reference", action="store_true")
    reference.add_argument("--references", type=Path)
    prepare.add_argument("--phase", choices=("e0", "e1-candidate"), default="e0")
    prepare.add_argument("--seeds", type=int, nargs="+", default=[1001])
    prepare.add_argument("--cells", nargs="+")
    prepare.add_argument("--rh-settings", type=Path)
    prepare.add_argument("--decision-settings", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("run-prepared", help="run paired frozen entries without multiplying demand seeds")
    execute.add_argument("--prepared", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--timing", choices=("D0", "D1"), required=True)
    execute.add_argument("--cells", nargs="+")
    execute.add_argument("--policies", choices=POLICIES, nargs="+")
    execute.add_argument("--decision-settings", type=Path)
    execute.add_argument("--run-wall-limit", type=float, default=240)
    execute.add_argument("--suite-wall-limit", type=float)
    comparison_plan = commands.add_parser("comparison-plan", help="draft an exploratory paired-analysis plan")
    comparison_plan.add_argument("--manifest", type=Path, required=True)
    comparison_plan.add_argument("--control", choices=POLICIES, default="optimized")
    comparison_plan.add_argument("--candidates", choices=POLICIES, nargs="+",
                                 default=["rh_milp_grid", "rh_cpsat_linear"])
    comparison_plan.add_argument("--output", type=Path, required=True)
    comparison = commands.add_parser("compare", help="verify saved arms and compute gated paired statistics")
    comparison.add_argument("--plan", type=Path, required=True)
    comparison.add_argument("--output", type=Path, required=True, help="new directory for JSON/CSV/notes")
    freeze = commands.add_parser("freeze-snapshot", help="freeze one returned grid input and common hint")
    freeze.add_argument("--run", type=Path, required=True)
    freeze.add_argument("--decision", type=int, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--preparation-limit", type=float, default=30)
    benchmark = commands.add_parser("benchmark-snapshot", help="compare compilers of one frozen grid model")
    benchmark.add_argument("--snapshot", type=Path, required=True)
    benchmark.add_argument("--output", type=Path, required=True)
    benchmark.add_argument("--budgets", type=float, nargs="+", default=[.25, 2])
    benchmark.add_argument("--repeats", type=int, default=3)
    benchmark.add_argument("--order-seed", type=int, default=20261114)
    benchmark.add_argument("--startup-limit", type=float, default=10)
    benchmark.add_argument("--request-allowance", type=float, default=3)
    benchmark.add_argument("--memory-limit-mib", type=float, default=1024)
    benchmark.add_argument("--suite-wall-limit", type=float, default=300)
    calibration = commands.add_parser("calibrate-window", help="audit fixed-window and completed-charge exposure")
    calibration.add_argument("--run", type=Path, required=True)
    calibration.add_argument("--window", type=Path, required=True)
    calibration.add_argument("--output", type=Path, required=True)
    study_freeze = commands.add_parser("freeze-study", help="seal phase inputs, code, settings and analysis")
    study_freeze.add_argument("--spec", type=Path, required=True)
    study_freeze.add_argument("--output", type=Path, required=True)
    study_run = commands.add_parser("run-study", help="execute every preregistered arm of one phase")
    study_run.add_argument("--frozen", type=Path, required=True)
    study_run.add_argument("--phase", choices=("training", "validation", "heldout", "stress"), required=True)
    study_run.add_argument("--output", type=Path, required=True)
    study_run.add_argument("--run-wall-limit", type=float, default=240)
    study_run.add_argument("--suite-wall-limit", type=float, default=3600)
    study_analysis = commands.add_parser("study-analysis-plan", help="derive exactly the registered phase analysis")
    study_analysis.add_argument("--manifest", type=Path, required=True)
    study_analysis.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"freeze-study", "run-study", "study-analysis-plan", "calibrate-window"}:
            from simulation.evaluation.calibration import calibrate
            from simulation.evaluation.study import freeze_study, run_study, study_plan

            if args.output.exists():
                raise ValueError("output already exists; preserve prior evidence")
            if args.command == "freeze-study":
                result = freeze_study(args.spec, args.output)
                status = "FROZEN_" + result["stage"].upper()
            elif args.command == "run-study":
                result = run_study(args.frozen, args.output, phase=args.phase,
                                   run_wall_limit=args.run_wall_limit, suite_wall_limit=args.suite_wall_limit)
                status = result["status"]
            else:
                result = (study_plan(args.manifest) if args.command == "study-analysis-plan" else
                          calibrate(args.run, json.loads(args.window.read_text(encoding="utf-8"))))
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
                status = result.get("status", "REGISTERED_ANALYSIS_PLAN")
            print(json.dumps({"status": status, "output": str(args.output)}))
            return 1 if status in {"FAIL", "PARTIAL"} else 0
        if args.command == "freeze-snapshot":
            from simulation.evaluation.frozen_snapshot import freeze_snapshot

            result = freeze_snapshot(args.run, args.decision, args.output, preparation_limit=args.preparation_limit)
            print(json.dumps({"status": "FROZEN", "model_hash": result["model_hash"],
                              "variables": result["variables"]}))
            return 0
        if args.command == "benchmark-snapshot":
            from simulation.evaluation.snapshot_benchmark import benchmark_snapshot

            result = benchmark_snapshot(
                args.snapshot, args.output, budgets=tuple(args.budgets), repeats=args.repeats,
                order_seed=args.order_seed, startup_limit=args.startup_limit,
                request_allowance=args.request_allowance, memory_limit_mib=args.memory_limit_mib,
                suite_wall_limit=args.suite_wall_limit,
            )
            print(json.dumps({"status": result["status"], "runs": len(result["runs"]),
                              "statuses": result["statuses"]}))
            return 0 if result["status"] == "SUCCESS" else 1
        if args.command in {"comparison-plan", "compare"}:
            from simulation.evaluation.comparison import compare_plan, validate_plan, write_comparison
            from simulation.evaluation.comparison_inputs import draft_plan

            if args.output.exists():
                raise ValueError("output already exists; preserve prior analysis")
            if args.command == "comparison-plan":
                result = draft_plan(args.manifest, control=args.control, candidates=tuple(args.candidates))
                validate_plan(result)
                status = "DRAFT_DESCRIPTIVE_PLAN"
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
            else:
                result = compare_plan(args.plan)
                write_comparison(result, args.output)
                status = "EXPLORATORY_ANALYSIS"
            print(json.dumps({"status": status, "output": str(args.output)}))
            return 0
        if args.command == "prepare-design":
            from simulation.evaluation.design import prepare_design
            from simulation.evaluation.design_references import synthetic_references

            design = json.loads(args.design.read_text(encoding="utf-8"))
            references = synthetic_references() if args.synthetic_reference else json.loads(
                args.references.read_text(encoding="utf-8"))
            result = prepare_design(design, references, args.output, phase=args.phase, seeds=tuple(args.seeds),
                                    cells=tuple(args.cells) if args.cells is not None else None,
                                    planning_settings=json.loads(args.rh_settings.read_text(encoding="utf-8"))
                                    if args.rh_settings else None,
                                    decision_settings=json.loads(args.decision_settings.read_text(encoding="utf-8"))
                                    if args.decision_settings else None)
            print(json.dumps({"status": result["status"], "inputs": len(result["entries"]),
                              "phase": result["phase"], "provenance": result["provenance"]}))
            return 0
        if args.command == "run-prepared":
            from simulation.evaluation.prepared import run_prepared

            result = run_prepared(args.prepared, args.output, timing=args.timing,
                                  cells=tuple(args.cells) if args.cells is not None else None,
                                  policies=tuple(args.policies) if args.policies is not None else None,
                                  run_wall_limit=args.run_wall_limit, suite_wall_limit=args.suite_wall_limit,
                                  decision_settings=json.loads(args.decision_settings.read_text(encoding="utf-8"))
                                  if args.decision_settings else None)
            print(json.dumps({"status": result["status"], "entries": len(result["entries"]),
                              "planned_runs": result["planned_runs"]}))
            return 0 if result["status"] == "SUCCESS" else 1
        if args.command == "profile":
            from simulation.evaluation.profiling import profile_manifest

            result = profile_manifest(args.manifest)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            print(json.dumps({"status": "PROFILED", "runs": result["loaded_runs"], "groups": len(result["groups"])}))
            return 0
        if args.command == "analyze":
            from simulation.evaluation.analysis import analyze

            result = analyze(args.run, args.events)
            if args.output is not None:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            print(
                json.dumps(
                    {
                        "status": "VERIFIED" if result["stored_metrics_match"] else "METRICS_MISMATCH",
                        "case_id": result["case_id"],
                        "events": result["events"],
                    }
                )
            )
            return 0 if result["stored_metrics_match"] else 1
        if args.command == "examples":
            args.output.mkdir(parents=True, exist_ok=False)
            for scenario in examples():
                (args.output / f"{scenario.id}.json").write_text(
                    json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
                )
            print(json.dumps({"status": "SUCCESS", "scenarios": [s.id for s in examples()]}))
            return 0
        if args.command == "validate":
            scenario = read_scenario(args.scenario)
            print(json.dumps({"status": "VALID", "scenario": scenario.id, "hash": fingerprint(scenario.to_dict())}))
            return 0
        if args.command == "run":
            scenario = read_scenario(args.scenario)
            settings = json.loads(args.rh_settings.read_text()) if args.rh_settings else None
            result = run_case(
                scenario,
                args.policy,
                args.seed,
                output=args.output,
                wall_limit=args.wall_limit,
                planning_settings=settings,
                decision_settings=json.loads(args.decision_settings.read_text()) if args.decision_settings else None,
            )
            print(json.dumps({"status": result["status"], "case_id": result["case_id"], "failure": result["failure"]}))
            return 0 if result["status"] == "SUCCESS" else 1
        if args.command == "suite":
            cases = tuple(read_scenario(p) for p in args.scenario) if args.scenario else examples()
            if args.duration is not None:
                for scenario in cases:
                    scenario.duration = args.duration
                    scenario.validate()
            result = run_suite(
                cases,
                tuple(args.seeds),
                args.output,
                policies=tuple(args.policies),
                run_wall_limit=args.run_wall_limit,
                suite_wall_limit=args.suite_wall_limit,
                planning_settings=json.loads(args.rh_settings.read_text()) if args.rh_settings else None,
                decision_settings=json.loads(args.decision_settings.read_text()) if args.decision_settings else None,
            )
            print(json.dumps({"status": result["status"], "runs": len(result["runs"])}))
            return 0 if result["status"] == "SUCCESS" else 1
        result = replay(json.loads(args.run.read_text(encoding="utf-8")), input_root=args.run.resolve().parent)
        print(json.dumps({key: value for key, value in result.items() if key != "result"}))
        return 0 if result["matches"] else 1
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
