"""Execute frozen design entries with their paired seed and explicit timing mode."""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.decisions.contracts import DecisionSettings
from simulation.evaluation.design import PHASES, PREPARED_SCHEMA, write_json
from simulation.evaluation.runner import run_suite, source_manifest
from simulation.evaluation.scenario import (
    POLICIES,
    Scenario,
    fingerprint,
    integer,
    number,
    read_scenario,
)
from simulation.planning.contracts import Settings


def _inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("prepared input path must stay inside its bundle")
    return path


def verify_prepared(directory: Path) -> tuple[dict[str, Any], list[tuple[dict[str, Any], Scenario]]]:
    """Check every input before running; reject tampering, duplicates and source drift."""
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("schema") != PREPARED_SCHEMA or manifest.get("status") != "PREPARED"
            or manifest.get("phase") not in PHASES or manifest.get("heldout_enabled") is not False):
        raise ValueError("expected a prepared E0/E1-candidate bundle, not a held-out protocol")
    if manifest["source"]["source_hash"] != source_manifest()["source_hash"]:
        raise ValueError("source differs from prepared bundle; prepare a new bundle before running")
    for name, expected in (("design.json", manifest["design_hash"]),
                           ("references.json", manifest["reference_hash"]),
                           ("math_settings.json", fingerprint(manifest["math_settings"])),
                           ("decision_settings.json", fingerprint(manifest["decision_settings"]))):
        actual = json.loads((directory / name).read_text(encoding="utf-8"))
        if fingerprint(actual) != expected:
            raise ValueError("prepared metadata changed: " + name)
    Settings(**manifest["math_settings"]).validate()
    DecisionSettings(**manifest["decision_settings"]).validate()
    entries, seen = [], set()
    for row in manifest["entries"]:
        if row["scenario"] in seen:
            raise ValueError("duplicate prepared scenario")
        seen.add(row["scenario"])
        integer(row["run_seed"], "run seed", 0, 2**32 - 1)
        if row["decision_soft_deadline_s"] is not None:
            number(row["decision_soft_deadline_s"], "decision budget", .000001)
        if (not row["policies"] or len(set(row["policies"])) != len(row["policies"])
                or set(row["policies"]) - set(POLICIES)):
            raise ValueError("invalid prepared policies")
        path = _inside(directory, row["path"])
        case = read_scenario(path)
        if (hashlib.sha256(path.read_bytes()).hexdigest() != row["file_sha256"]
                or case.id != row["scenario"] or fingerprint(case.to_dict()) != row["scenario_hash"]
                or fingerprint(case.workload["jobs"]) != row["trace_hash"]
                or fingerprint({"robots": {k: asdict(v) for k, v in case.initial_robots.items()},
                                "cabins": case.initial_cabins}) != row["initial_hash"]):
            raise ValueError("prepared scenario or trace changed: " + row["scenario"])
        entries.append((row, case))
    if not entries:
        raise ValueError("prepared bundle is empty")
    return manifest, entries


def run_prepared(directory: Path, output: Path, *, timing: str, cells: tuple[str, ...] | None = None,
                 policies: tuple[str, ...] | None = None, run_wall_limit: float = 240,
                 suite_wall_limit: float | None = None,
                 decision_settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use one case/seed per existing paired suite; never cross frozen demand seeds.

    Random demand and deterministic fixtures retain distinct sampling semantics.
    All failed/partial/budget-exhausted arms remain in the parent manifest.
    """
    if timing not in {"D0", "D1"}:
        raise ValueError("choose D0 or common staged D1 explicitly")
    number(run_wall_limit, "run wall limit", .000001)
    if suite_wall_limit is not None:
        number(suite_wall_limit, "suite wall limit", .000001)
    prepared, entries = verify_prepared(directory)
    if cells is not None:
        if not cells or len(set(cells)) != len(cells) or set(cells) - {r["cell"] for r, _ in entries}:
            raise ValueError("unknown, empty or duplicate cell selection")
        entries = [(r, s) for r, s in entries if r["cell"] in cells]
    decision = {**copy.deepcopy(prepared["decision_settings"]), **copy.deepcopy(decision_settings or {})}
    DecisionSettings(**decision).validate()
    for row, _ in entries:
        selected = policies if policies is not None else tuple(row["policies"])
        if not selected or len(set(selected)) != len(selected) or set(selected) - set(row["policies"]):
            raise ValueError("policy selection exceeds prepared evaluation scope")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"schema": "clover-prepared-runs-v1", "status": "RUNNING", "timing": timing,
                "phase": prepared["phase"], "layout_id": prepared["layout_id"],
                "provenance": prepared["provenance"], "calibration_status": prepared["calibration_status"],
                "prepared_hash": fingerprint(prepared), "source": prepared["source"], "entries": [],
                "planned_runs": sum(len(policies or r["policies"]) for r, _ in entries),
                "scope": "reference/pilot inputs; no inferential statistics or final recommendation"}

    def checkpoint() -> None:
        temporary = output / "manifest.pending.json"
        write_json(temporary, manifest)
        temporary.replace(output / "manifest.json")

    checkpoint()
    started = time.perf_counter()
    source_changed = False
    for row, case in entries:
        selected = policies or tuple(row["policies"])
        source_changed = source_changed or source_manifest()["source_hash"] != prepared["source"]["source_hash"]
        remaining = None if suite_wall_limit is None else suite_wall_limit - (time.perf_counter() - started)
        if source_changed or (remaining is not None and remaining <= 0):
            record = {"cell": row["cell"], "scenario": case.id, "run_seed": row["run_seed"],
                      "status": "SOURCE_CHANGED" if source_changed else "NOT_RUN_BUDGET",
                      "policies": list(selected), "fair_inputs": None}
        else:
            current = copy.deepcopy(decision)
            if row["decision_soft_deadline_s"] is not None:
                current["end_to_end_soft_deadline_seconds"] = row["decision_soft_deadline_s"]
            suite = run_suite((case,), (row["run_seed"],), output / case.id, policies=tuple(selected),
                              planning_settings=prepared["math_settings"],
                              decision_settings=current if timing == "D1" else None,
                              run_wall_limit=run_wall_limit, suite_wall_limit=remaining)
            expected = {key: row[key] for key in ("scenario_hash", "initial_hash", "trace_hash")}
            observed = [r for r in suite["runs"] if r["status"] != "NOT_RUN_BUDGET"]
            fair = (all(all(r.get(key) == value for key, value in expected.items()) for r in observed)
                    if observed else None)
            record = {"cell": row["cell"], "family": row["family"], "scenario": case.id,
                      "run_seed": row["run_seed"], "demand_seed": row["demand_seed"], "policies": list(selected),
                      "status": "INPUT_MISMATCH" if fair is False else suite["status"], "fair_inputs": fair,
                      "complete_policy_block": len(observed) == len(selected),
                      "suite": f"{case.id}/manifest.json", "runs": suite["runs"],
                      "decision_settings": current if timing == "D1" else None,
                      "D0_budget_note": "decision soft budget is not injected in D0" if timing == "D0" else None}
        manifest["entries"].append(record)
        checkpoint()
    manifest["status"] = "SUCCESS" if all(r["status"] == "SUCCESS" for r in manifest["entries"]) else "PARTIAL"
    manifest["wall_seconds"] = time.perf_counter() - started
    checkpoint()
    return manifest
