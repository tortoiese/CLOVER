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
    args = parser.parse_args(argv)
    try:
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
        result = replay(json.loads(args.run.read_text(encoding="utf-8")))
        print(json.dumps({key: value for key, value in result.items() if key != "result"}))
        return 0 if result["matches"] else 1
    except (ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"status": "INVALID_INPUT", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
