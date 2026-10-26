"""Fixed scenario suite with held-out validation and disclosed negative results."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from simulation.comparison import paired_interval
from simulation.experiments import run_experiment, source_manifest
from simulation.scenario_suite import SCREENING_SEEDS, VALIDATION_SEEDS, scenarios
from utils.comparison_models import ComparisonConditions


def assess(result: dict, candidate: str = "rh_milp") -> dict:
    """Apply predeclared noninferiority and tail-wait improvement gates."""
    base, proposed = result["samples"]["weighted_heuristic"], result["samples"][candidate]
    failures = any(row["status"] != "SUCCESS" for row in base + proposed)
    complete = [100 * (c["completed"] - b["completed"]) / b["completed"] for b, c in zip(base, proposed, strict=True)
                if b.get("completed", 0) > 0 and c.get("completed") is not None]
    waiting = [100 * (c["wait_p95"] - b["wait_p95"]) / b["wait_p95"] for b, c in zip(base, proposed, strict=True)
               if b.get("wait_p95", 0) and c.get("wait_p95") is not None]
    counts_ok = len(complete) == len(waiting) == len(base) and not failures
    completed_ci = paired_interval(complete) if counts_ok else None
    wait_ci = paired_interval(waiting) if counts_ok else None
    return {"candidate": candidate, "failed_runs_present": failures,
            "completed_relative_difference_ci_pct": completed_ci,
            "wait_p95_relative_difference_ci_pct": wait_ci,
            "passes_predeclared_gate": bool(counts_ok and completed_ci["low"] >= -5 and wait_ci["high"] <= -10),
            "gate": "95% paired t CI: completed lower >= -5%; wait-P95 upper <= -10%"}


def main() -> int:
    """Save the protocol before running any evaluated seed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cohort", choices=("screening", "validation"), default="screening")
    parser.add_argument("--scenarios", nargs="+", default=[s.id for s in scenarios()])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-cpsat", action="store_true")
    args = parser.parse_args()
    selected = [s for s in scenarios() if s.id in args.scenarios]
    if set(args.scenarios) != {s.id for s in selected}:
        parser.error("unknown scenario")
    seeds = SCREENING_SEEDS if args.cohort == "screening" else VALIDATION_SEEDS
    policies = ("weighted_heuristic", "rh_milp", "rh_cpsat") if args.include_cpsat else ("weighted_heuristic", "rh_milp")
    args.output.mkdir(parents=True, exist_ok=True)
    design = {"schema": "clover-scenario-protocol-v1", "cohort": args.cohort, "seeds": seeds,
              "screening_seeds": SCREENING_SEEDS, "validation_seeds": VALIDATION_SEEDS,
              "policies": policies, "source_hashes": source_manifest(),
              "primary_hypothesis": "alternating_dispatch improves joint assignment, not every workload",
              "primary_metrics": ["completed", "unfinished", "wait_p95"],
              "secondary_metrics": ["on_time_completed", "late_completed", "battery_units"],
              "gate": "completed >= -5%, wait-P95 <= -10%, using paired 95% CI",
              "cases": [{"id": s.id, "hypothesis": s.hypothesis, "factory": s.factory.to_dict(),
                         "settings": asdict(s.settings), "duration": s.duration,
                         "wave_period": s.wave_period} for s in selected],
              "rules": ["No heuristic weight changes", "No future arrivals or process noise in planner input",
                        "Keep all seed outcomes and fallback counts", "Publish unsuccessful scenarios too"]}
    (args.output / "protocol.json").write_text(json.dumps(design, ensure_ascii=False, indent=2), encoding="utf-8")
    assessments = {}
    for scenario in selected:
        conditions = ComparisonConditions(duration=scenario.duration, repeats=len(seeds), seed=seeds[0],
                                          horizon=scenario.settings.horizon, replan=scenario.settings.interval)
        result = run_experiment(scenario.factory, conditions, policies, scenario.settings,
                                traces=tuple(scenario.trace(seed) for seed in seeds), wall_limit=900)
        result["scenario_id"] = scenario.id
        result["cohort"] = args.cohort
        result["hypothesis"] = scenario.hypothesis
        (args.output / f"{scenario.id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                                                    allow_nan=False), encoding="utf-8")
        assessments[scenario.id] = {p: assess(result, p) for p in policies if p != "weighted_heuristic"}
        print(json.dumps({"scenario": scenario.id, "summaries": result["summaries"],
                          "assessment": assessments[scenario.id]}, ensure_ascii=False), flush=True)
    (args.output / "assessment.json").write_text(json.dumps(assessments, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
