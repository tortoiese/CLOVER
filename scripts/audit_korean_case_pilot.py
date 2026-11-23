"""Verify frozen pilot files and run bounded, compressed development diagnostics."""

from __future__ import annotations

import argparse
import json
import math
import random
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from materialize_korean_case_pilot import (
    DecisionSettings,
    Scenario,
    Settings,
    file_sha,
    fingerprint,
    source_manifest,
    validate_design,
    validate_routes,
    write_json,
)

LOCK_PARENT = Path(tempfile.gettempdir())


def inside(root: Path, name: str) -> Path:
    """Resolve a bundle-owned path, rejecting absolute paths and escaped links."""
    relative = Path(name)
    result = (root / relative).resolve()
    if relative.is_absolute() or not result.is_relative_to(root):
        raise ValueError("bundle path escapes its root")
    return result


def audit_bundle(bundle: Path) -> dict[str, Any]:
    """Validate the entire declared matrix before any policy execution."""
    bundle = bundle.resolve()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "clover-korean-case-pilot-v2":
        raise ValueError(
            "regenerate the bundle with pilot v2; v1 did not seal settings"
        )
    required = {"rh_settings.json", "d1_settings.json", "design.json", "sources.json"}
    if set(manifest.get("support_files", {})) != required:
        raise ValueError("incomplete support file inventory")
    support = {}
    for name, expected in manifest["support_files"].items():
        path = inside(bundle, name)
        if file_sha(path) != expected:
            raise ValueError("support file hash mismatch: " + name)
        support[name] = json.loads(path.read_text(encoding="utf-8"))
    design, sources = support["design.json"], support["sources.json"]
    validate_design(design, sources)
    if (
        fingerprint(design) != manifest["design_content_hash"]
        or fingerprint(sources) != manifest["sources_content_hash"]
    ):
        raise ValueError("recipe identity mismatch")
    if support["rh_settings.json"] != asdict(Settings(**design["rh_settings"])):
        raise ValueError("math settings differ from declared design")
    if (
        support["d1_settings.json"]
        != DecisionSettings(**design["d1_settings"]).to_dict()
    ):
        raise ValueError("decision settings differ from declared design")
    if manifest["source_hash"] != source_manifest()["source_hash"]:
        raise ValueError(
            "application source differs from input bundle; regenerate separately"
        )
    if manifest["policies"] != design["policies"]:
        raise ValueError("policy matrix differs from design")
    expected_keys = {
        (c["id"], seed) for c in design["conditions"] for seed in design["seeds"]
    }
    entries = manifest["entries"]
    actual_keys = [(e["condition"], e["seed"]) for e in entries]
    if len(set(actual_keys)) != len(actual_keys) or set(actual_keys) != expected_keys:
        raise ValueError("missing, duplicate, or extra condition/seed pair")
    rows, cases, paths = [], {}, set()
    conditions = {c["id"]: c for c in design["conditions"]}
    families = {f["id"]: f for f in design["families"]}
    for entry in entries:
        path = inside(bundle, entry["scenario"])
        if path in paths:
            raise ValueError("one physical input is reused under different entries")
        paths.add(path)
        if file_sha(path) != entry["sha256"]:
            raise ValueError("scenario file hash mismatch: " + entry["scenario"])
        case = Scenario.from_dict(json.loads(path.read_text(encoding="utf-8")))
        condition = conditions[entry["condition"]]
        if (
            case.id != f"{entry['condition']}_{entry['seed']}"
            or condition["family"] != entry["family"]
            or families[entry["family"]]["source_id"] != entry["source_id"]
        ):
            raise ValueError("case/seed/source identity mismatch")
        data = case.to_dict()
        if (
            fingerprint(data) != entry["scenario_hash"]
            or fingerprint(case.workload["jobs"]) != entry["trace_hash"]
            or len(case.workload["jobs"]) != entry["jobs"]
        ):
            raise ValueError("scenario/trace identity mismatch")
        checks = validate_routes(case)
        for event in case.interventions:
            if event.kind == "edge_width":
                edge = next(e for e in case.factory.edges if str(e.id) == event.target)
                edge.width = event.value
                validate_routes(case)
        if case.interventions:
            # Re-read the immutable file after checking each transient layout.
            case = Scenario.from_dict(data)
        jobs = case.workload["jobs"]
        due = [j for j in jobs if case.warmup <= j["deadline"] < case.duration]
        rows.append(
            {
                "condition": entry["condition"],
                "seed": entry["seed"],
                "jobs": len(jobs),
                "due_jobs": len(due),
                "urgent_due_jobs": sum(j["priority"] == 0 for j in due),
                "route_checks": checks,
            }
        )
        cases[(entry["condition"], entry["seed"])] = case
    for condition in design["conditions"]:
        if condition["compare_to"] is None:
            continue
        for seed in design["seeds"]:
            parent = cases[(condition["compare_to"], seed)]
            child = cases[(condition["id"], seed)]
            axis = condition["changed_axis"]
            before, after = parent.workload["jobs"], child.workload["jobs"]
            if (
                axis
                not in {
                    "destination_distribution",
                    "arrival_shape_equal_realized_count",
                }
                and before != after
            ):
                raise ValueError("physical comparison changed demand")
            if axis == "destination_distribution":
                excluded = {"station", "allowed"}
            elif axis == "arrival_shape_equal_realized_count":
                excluded = {"at", "deadline"}
            else:
                excluded = set()
            for a, b in zip(before, after, strict=True):
                if {k: v for k, v in a.items() if k not in excluded} != {
                    k: v for k, v in b.items() if k not in excluded
                }:
                    raise ValueError("undeclared change to paired job attributes")
                if axis == "arrival_shape_equal_realized_count" and not math.isclose(
                    a["deadline"] - a["at"], b["deadline"] - b["at"], abs_tol=1e-9
                ):
                    raise ValueError("burst changed the input deadline slack")
            if (
                "floor_reallocation" not in axis
                and parent.initial_robots != child.initial_robots
            ):
                raise ValueError("physical comparison changed initial robot states")
    arm_count = len(entries) * len(manifest["policies"])
    if arm_count != manifest["planned_policy_runs_per_timing_mode"]:
        raise ValueError("planned policy denominator mismatch")
    from simulation.evaluation.main_protocol import hardware_id
    from simulation.evaluation.runner import environment_manifest

    return {
        "schema": "clover-korean-case-audit-v1",
        "status": "INPUTS_VALID",
        "bundle_manifest_sha256": file_sha(manifest_path),
        "source_hash": manifest["source_hash"],
        "execution_environment": environment_manifest(),
        "hardware_id": hardware_id(),
        "inputs": len(entries),
        "planned_policy_runs_per_timing_mode": arm_count,
        "input_checks": rows,
        "readiness": {
            "D0": "DEVELOPMENT_ONLY_RESOURCE_EXPOSURE_REVIEW_REQUIRED",
            "D1": "NOT_CERTIFIED_PUBLICATION_COVERAGE_REQUIRES_VALIDATION",
        },
        "scope": "file, recipe, pairing, and reachability checks; no statistical or operational approval",
    }


def _execute(
    bundle: Path,
    output: Path,
    *,
    stage: str,
    limit: int = 1,
    run_wall_limit: float = 900,
    job_wall_limit: float = 3600,
) -> dict[str, Any]:
    """Execute each concrete input with its own seed, retaining the full denominator."""
    if (
        stage not in {"d0-pilot", "d1-diagnostic"}
        or type(limit) is not int
        or limit < 1
    ):
        raise ValueError("explicit development stage and a positive job limit required")
    audit = audit_bundle(bundle)
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text())
    if (
        shutil.disk_usage(output.parent if output.parent.exists() else bundle).free
        < 2_000_000_000
    ):
        raise ValueError("less than2GB free for diagnostic output")
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "audit.json", audit)
    jobs = [dict(e, status="NOT_STARTED") for e in manifest["entries"]]
    random.Random(20261117).shuffle(jobs)
    record = {
        "schema": "clover-korean-development-batch-v1",
        "stage": stage,
        "input_manifest_sha256": audit["bundle_manifest_sha256"],
        "planned_policy_runs": audit["planned_policy_runs_per_timing_mode"],
        "status": "RUNNING",
        "order_seed": 20261117,
        "jobs": jobs,
        "job_limit": limit,
        "run_wall_limit": run_wall_limit,
        "job_wall_limit": job_wall_limit,
    }

    def checkpoint() -> None:
        path = output / "batch.json"
        temporary = output / "batch.json.tmp"
        temporary.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    checkpoint()
    from simulation.evaluation.runner import run_suite

    math_settings = json.loads((bundle / "rh_settings.json").read_text())
    decision = (
        json.loads((bundle / "d1_settings.json").read_text())
        if stage == "d1-diagnostic"
        else None
    )
    try:
        for index, entry in enumerate(jobs[:limit]):
            if (
                file_sha(bundle / "manifest.json") != record["input_manifest_sha256"]
                or source_manifest()["source_hash"] != audit["source_hash"]
            ):
                raise ValueError(
                    "source or input registration changed during execution"
                )
            for name, expected in manifest["support_files"].items():
                if file_sha(inside(bundle, name)) != expected:
                    raise ValueError("settings/recipe changed during execution")
            path = inside(bundle, entry["scenario"])
            if file_sha(path) != entry["sha256"]:
                raise ValueError("case changed during execution")
            entry["status"] = "RUNNING"
            checkpoint()
            started = time.monotonic()
            suite_dir = output / f"job_{index:04d}"
            result = run_suite(
                (Scenario.from_dict(json.loads(path.read_text())),),
                (entry["seed"],),
                suite_dir,
                policies=tuple(manifest["policies"]),
                planning_settings=math_settings,
                decision_settings=decision,
                run_wall_limit=run_wall_limit,
                suite_wall_limit=job_wall_limit,
                compress_artifacts=True,
                archive_codec="xz",
            )
            entry.update(
                status=result["status"],
                suite=f"job_{index:04d}/manifest.json",
                wall_seconds=time.monotonic() - started,
                arm_statuses=[
                    {
                        "policy": r["policy"],
                        "status": r["status"],
                        "failure": r.get("failure"),
                    }
                    for r in result["runs"]
                ],
            )
            if (
                file_sha(bundle / "manifest.json") != record["input_manifest_sha256"]
                or file_sha(path) != entry["sha256"]
                or source_manifest()["source_hash"] != audit["source_hash"]
                or any(
                    file_sha(inside(bundle, name)) != expected
                    for name, expected in manifest["support_files"].items()
                )
            ):
                raise ValueError(
                    "source, input, or settings changed during the recorded job"
                )
            checkpoint()
        record["status"] = (
            "RECORDING_COMPLETE" if limit >= len(jobs) else "PARTIAL_PLAN_RECORDED"
        )
    except BaseException as exc:
        record["status"], record["error"] = "INTERRUPTED_OR_ERROR", str(exc)
        checkpoint()
        raise
    checkpoint()
    return record


@contextmanager
def execution_lock() -> Iterator[None]:
    """Serialize this launcher across bundles on the same host."""
    from simulation.evaluation.main_jobs import _host_lock
    from simulation.evaluation.main_protocol import hardware_id

    root = LOCK_PARENT / ("clover-korean-execution-" + hardware_id())
    root.mkdir(exist_ok=True)
    with _host_lock(root):
        yield


def execute(
    bundle: Path,
    output: Path,
    *,
    stage: str,
    limit: int = 1,
    run_wall_limit: float = 900,
    job_wall_limit: float = 3600,
) -> dict[str, Any]:
    """Acquire the host launcher lock before recording a development batch."""
    with execution_lock():
        return _execute(
            bundle,
            output,
            stage=stage,
            limit=limit,
            run_wall_limit=run_wall_limit,
            job_wall_limit=job_wall_limit,
        )


def main() -> int:
    """Default to read-only validation; policy execution requires a named stage."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=("validate", "d0-pilot", "d1-diagnostic"), default="validate"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="concrete input jobs, each with exactly its own seed",
    )
    args = parser.parse_args()
    try:
        if args.stage == "validate":
            result = audit_bundle(args.bundle)
            write_json(args.output, result)
        else:
            result = execute(
                args.bundle, args.output, stage=args.stage, limit=args.limit
            )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "scope": "development only; no D1 certification",
                }
            )
        )
        if args.stage != "validate" and any(
            j["status"] not in {"SUCCESS", "NOT_STARTED"} for j in result["jobs"]
        ):
            return 1
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
