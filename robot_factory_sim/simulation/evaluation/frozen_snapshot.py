"""Versioned grid observations and one shared, checked solver starting point."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from simulation.decisions.input_store import load_planning_input
from simulation.evaluation.runner import source_manifest
from simulation.evaluation.scenario import fingerprint, integer, number
from simulation.planning.backend import check_incumbent, cost
from simulation.planning.contracts import PreparedInput, Report, Result
from simulation.planning.incumbent import Hint, work_hint
from simulation.planning.model import Model, build_model
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import prepare

SCHEMA = "clover-frozen-grid-snapshot-v1"


def sha256(path: Path) -> str:
    """Hash the complete original file, independently of JSON formatting."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_model(data: PreparedInput, limit: float) -> Model:
    """Check geometry before constructing one canonical grid model."""
    data.snapshot.settings.validate()
    if data.snapshot.settings.formulation != "grid":
        raise ValueError("frozen comparison requires grid, not the native action domain")
    deadline = time.perf_counter() + number(limit, "preparation limit", .001, 300)
    if prepare(data.snapshot, deadline=deadline) != data:
        raise ValueError("prepared route coefficients differ from snapshot geometry")
    return build_model(data, deadline)


def freeze_snapshot(run_path: Path, decision: int, output: Path, *, preparation_limit: float = 30) -> dict[str, Any]:
    """Freeze an explicit worker input, retaining rejected-return provenance.

    The old run's source remains its original source. Only the new artifact's
    construction and later reconstruction require the current source hash.
    """
    if output.exists():
        raise ValueError("snapshot output already exists")
    source = source_manifest()
    original_hash = sha256(run_path)
    from simulation.evaluation.comparison_inputs import read_json

    run, _ = read_json(run_path)
    if run.get("schema") != "clover-headless-run-v5" or not run.get("control"):
        raise ValueError("freeze requires a saved staged-D1 v5 run")
    control = run["control"]
    for value, expected in ((control["decisions"], control["decisions_hash"]),
                            (control["lifecycle"], control["lifecycle_hash"]),
                            (run["scenario"], run["scenario_hash"]), (run["trace"], run["trace_hash"])):
        if fingerprint(value) != expected:
            raise ValueError("original run record hash mismatch")
    if run["source"]["source_hash"] != run["source_hash_at_end"]:
        raise ValueError("original run changed source")
    integer(decision, "decision index", 0, len(control["decisions"]) - 1)
    record = control["decisions"][decision]
    worker_plan = ((record.get("compute") or {}).get("value") or {}).get("plan")
    plan = worker_plan or record.get("rh_result")
    captured = record.get("planning_input_ref")
    if plan is None and captured is None:
        raise ValueError("selected decision has no returned planning input")
    if worker_plan and record.get("rh_result") and worker_plan != record["rh_result"]:
        raise ValueError("worker and controller planning records differ")
    if captured is not None:
        snapshot = load_planning_input(control, captured, run_path.parent)
        if "effective_solver_timeout_seconds" in record:
            snapshot = replace(snapshot, settings=replace(
                snapshot.settings, timeout=record["effective_solver_timeout_seconds"]))
        data = prepare(snapshot, deadline=time.perf_counter() + preparation_limit)
        if plan is not None and decode(plan).input != data:
            raise ValueError("returned plan differs from pre-request observation")
    else:
        data = decode(plan).input
    if (data.snapshot.token != record["token"] or data.snapshot.epoch != run["execution_epoch"]
            or not 0 <= data.snapshot.now <= run["observed_seconds"]):
        raise ValueError("planning observation identity differs from the run")
    before = time.perf_counter()
    model = checked_model(data, preparation_limit)
    model_seconds = time.perf_counter() - before
    before = time.perf_counter()
    hint = work_hint(model, time.perf_counter() + data.snapshot.settings.warm_start_seconds)
    hint_seconds = time.perf_counter() - before
    check_incumbent(model, hint.values)
    body = {
        "schema": SCHEMA, "source": source,
        "origin": {"run_path": str(run_path.resolve()), "run_sha256": original_hash,
                   "source_hash": run["source"]["source_hash"], "case_id": run["case_id"],
                   "run_status": run["status"], "policy": run["policy"], "decision_index": decision,
                   "decision_reason": record["reason"], "accepted": record["accepted"],
                   "worker_return_used": worker_plan is not None and captured is None,
                   "worker_plan_present": worker_plan is not None,
                   "input_origin": "pre_rpc_snapshot" if captured is not None else "returned_plan"},
        "prepared": encode(Result(data, Report("NOT_SOLVED"))),
        "input_hash": fingerprint(asdict(data)), "model_hash": model.fingerprint,
        "variables": len(model.variables), "constraints": len(model.rows),
        "hint": asdict(hint), "hint_hash": fingerprint(asdict(hint)),
        "hint_objectives": [cost(hint.values, model.objective1), cost(hint.values, model.objective2)],
        "common_preparation_seconds": model_seconds, "common_hint_seconds": hint_seconds,
        "scope": "one observed grid input; shared model and hint; not a new demand replicate",
    }
    if source_manifest()["source_hash"] != source["source_hash"] or sha256(run_path) != original_hash:
        raise ValueError("source or original record changed during freeze")
    body["artifact_hash"] = fingerprint(body)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return body


def load_snapshot(path: Path, *, preparation_limit: float = 30) -> tuple[dict[str, Any], Model, Hint]:
    """Reconstruct and verify one common model before starting any backend."""
    body = json.loads(path.read_text(encoding="utf-8"))
    payload = {key: value for key, value in body.items() if key != "artifact_hash"}
    if body.get("schema") != SCHEMA or fingerprint(payload) != body.get("artifact_hash"):
        raise ValueError("frozen snapshot artifact hash/schema mismatch")
    if body["source"]["source_hash"] != source_manifest()["source_hash"]:
        raise ValueError("source differs from frozen snapshot; freeze a new artifact")
    data = decode(body["prepared"]).input
    if fingerprint(asdict(data)) != body["input_hash"] or fingerprint(body["hint"]) != body["hint_hash"]:
        raise ValueError("frozen input or hint hash mismatch")
    model = checked_model(data, preparation_limit)
    if (model.fingerprint != body["model_hash"] or len(model.variables) != body["variables"]
            or len(model.rows) != body["constraints"]):
        raise ValueError("reconstructed model differs from frozen model")
    hint = Hint(**{**body["hint"], "values": tuple(body["hint"]["values"])})
    check_incumbent(model, hint.values)
    if [cost(hint.values, model.objective1), cost(hint.values, model.objective2)] != body["hint_objectives"]:
        raise ValueError("frozen hint objectives differ")
    return body, model, hint
