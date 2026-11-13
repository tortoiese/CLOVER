"""Rebuild resource observations from a complete saved raw execution stream."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import config
from simulation.evaluation.digest import PhysicalDigest
from simulation.evaluation.resource_metrics import ResourceMetrics
from simulation.evaluation.runner import DECISION_RESULT_SCHEMA, RESULT_SCHEMA


def analyze(run_path: Path, events_path: Path | None = None) -> dict[str, Any]:
    """Verify stream identity and recompute queue/cabin metrics without a solver.

    Args:
        run_path: One saved v4 result, including successful or failed runs.
        events_path: Raw stream; defaults to the matching .events.jsonl file.

    Returns:
        Complete recomputed metrics and comparison with stored observations.
    """
    content = run_path.read_bytes()
    result = json.loads(content)
    if result.get("schema") not in {RESULT_SCHEMA,DECISION_RESULT_SCHEMA}:
        raise ValueError("unsupported resource analysis schema; use the matching source version")
    scenario = result["scenario"]
    # The threshold is part of the saved result, not the machine's current config.
    threshold = result.get("resource_waits", {}).get("threshold_seconds", config.DEADLOCK_THRESHOLD_SEC)
    metrics = ResourceMetrics(scenario["warmup"], scenario["duration"], threshold)
    digest, raw_digest, physical = hashlib.sha256(), hashlib.sha256(), PhysicalDigest()
    previous, count = 0.0, 0
    with (events_path or run_path.with_suffix(".events.jsonl")).open("rb") as stream:
        for line in stream:
            raw_digest.update(line)
            row = json.loads(line)
            if row["sequence"] != count or not previous <= row["time"] <= result["observed_seconds"]:
                raise ValueError("noncontiguous or out-of-order execution stream")
            previous = row["time"]
            count += 1
            if (
                row["event"] == "traffic"
                and row["action"] == "granted"
                and any(u["occupied"] > u["capacity"] for u in row["bundle"])
            ):
                raise ValueError("recorded traffic overcapacity")
            metrics.record(row)
            physical.record(row)
            payload = {k: v for k, v in row.items() if k != "wall_seconds"}
            digest.update(json.dumps(payload, sort_keys=True, allow_nan=False).encode() + b"\n")
    if digest.hexdigest() != result["event_digest"] or physical.hexdigest() != result["physical_event_digest"]:
        raise ValueError("raw stream digest mismatch or truncated stream")
    waits = metrics.waits_snapshot(result["observed_seconds"])
    cabins = metrics.cabins_snapshot(result["observed_seconds"])
    return {
        "schema": "clover-resource-analysis-v1",
        "case_id": result["case_id"],
        "run_status": result["status"],
        "events": count,
        "source": result["source"],
        "input_result_sha256": hashlib.sha256(content).hexdigest(),
        "input_events_sha256": raw_digest.hexdigest(),
        "event_digest": digest.hexdigest(),
        "physical_event_digest": physical.hexdigest(),
        "stored_metrics_match": waits == result["resource_waits"] and cabins == result["cabin_phases"],
        "resource_waits": waits,
        "cabin_phases": cabins,
        "scope": "raw observation reconstruction; no native solve, statistical policy comparison or recommendation",
    }
