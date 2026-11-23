"""Audit executable static study inputs, including regenerated semantic identities."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from audit_korean_case_pilot import inside
from materialize_static_scenario_study import (
    POLICIES,
    DecisionSettings,
    Scenario,
    Settings,
    case_for,
    file_sha,
    fingerprint,
    planned_inputs,
    source_manifest,
    tool_hashes,
    validate_design,
    validate_routes,
)


def audit_bundle(bundle: Path) -> dict[str, Any]:
    """Reject missing arms, changed tools/settings, path escapes and rehashed input edits."""
    bundle = bundle.resolve()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != "clover-static-study-bundle-v1":
        raise ValueError("static study bundle required")
    if set(manifest["support_files"]) != {"design.json", "rh_settings.json", "d1_settings.json"}:
        raise ValueError("incomplete support inventory")
    support = {}
    for name, expected in manifest["support_files"].items():
        path = inside(bundle, name)
        if file_sha(path) != expected:
            raise ValueError("support identity mismatch: " + name)
        support[name] = json.loads(path.read_text(encoding="utf-8"))
    design = support["design.json"]
    validate_design(design)
    if (
        support["rh_settings.json"] != asdict(Settings(**design["rh_settings"]))
        or support["d1_settings.json"] != DecisionSettings(**design["d1_settings"]).to_dict()
    ):
        raise ValueError("support settings differ from design")
    if manifest["source"]["source_hash"] != source_manifest()["source_hash"] or manifest["tools"] != tool_hashes():
        raise ValueError("source or orchestration tools changed; create a separate campaign")
    expected = planned_inputs(design)
    entries = manifest["entries"]
    if (
        len(entries) != len(expected)
        or manifest["planned_runs"] != len(expected) * 4
        or manifest["policies"] != POLICIES
    ):
        raise ValueError("planned matrix denominator mismatch")
    seen, routes, due, urgent = set(), 0, 0, 0
    for index, (row, declaration) in enumerate(zip(entries, expected, strict=True)):
        if {k: row[k] for k in declaration} != declaration or row["index"] != index:
            raise ValueError("entry identity or order differs from registered matrix")
        path = inside(bundle, row["scenario"])
        if path in seen or file_sha(path) != row["sha256"]:
            raise ValueError("duplicate or changed physical input path")
        seen.add(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        case = Scenario.from_dict(raw)
        regenerated = case_for(design, declaration)
        if raw != regenerated.to_dict() or case.interventions:
            raise ValueError("scenario differs from deterministic static recipe")
        checks = validate_routes(case)
        jobs = case.workload["jobs"]
        if (
            fingerprint(raw) != row["scenario_hash"]
            or fingerprint(jobs) != row["trace_hash"]
            or fingerprint(raw["initial_robots"]) != row["initial_hash"]
            or row["jobs"] != len(jobs)
            or row["route_checks"] != checks
        ):
            raise ValueError("scenario/trace/initial/route identity mismatch")
        routes += checks
        due += sum(case.warmup <= j["deadline"] < case.duration for j in jobs)
        urgent += sum(case.warmup <= j["deadline"] < case.duration and j["priority"] == 0 for j in jobs)
    return {
        "schema": "clover-static-study-audit-v1",
        "status": "INPUTS_VALID",
        "manifest_sha256": file_sha(bundle / "manifest.json"),
        "source_hash": manifest["source"]["source_hash"],
        "tools": manifest["tools"],
        "inputs": len(entries),
        "planned_runs": manifest["planned_runs"],
        "phase_runs": {phase: count * 4 for phase, count in Counter(r["phase"] for r in entries).items()},
        "route_checks": routes,
        "due_jobs_across_input_entries": due,
        "urgent_due_jobs_across_input_entries": urgent,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit_bundle(args.bundle), ensure_ascii=False, indent=2))
