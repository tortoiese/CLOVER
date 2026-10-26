"""Strict JSON experiment import/replay without database access or executable data."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from simulation.experiments import (
    Demand,
    Intervention,
    run_experiment,
    source_manifest,
    trace_hash,
)
from simulation.planning.contracts import (
    MODEL_VERSION,
    OBJECTIVE_PROFILE,
    UNIT_PROFILE,
    Settings,
)
from utils.comparison_models import ComparisonConditions
from utils.config_models import FactoryConfig


def read_experiment(path: Path) -> dict[str, Any]:
    """Validate schema, versions and input digests, including GUI result wrappers."""
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("experiment JSON exceeds 64MiB input limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    data = data.get("experiment", data)
    for key, expected in (("schema", "clover-experiment-v2"), ("model_version", MODEL_VERSION),
                          ("objective_profile", OBJECTIVE_PROFILE), ("unit_profile", UNIT_PROFILE)):
        if data.get(key) != expected:
            raise ValueError(f"incompatible {key}")
    digest = hashlib.sha256(json.dumps(data["factory"], sort_keys=True).encode()).hexdigest()
    if digest != data["config_hash"]:
        raise ValueError("factory hash mismatch")
    traces = tuple(tuple(Demand(**{**d, "allowed": tuple(d["allowed"])}) for d in trace)
                   for trace in data["traces"])
    if [trace_hash(trace) for trace in traces] != data["trace_hashes"]:
        raise ValueError("demand trace hash mismatch")
    digest = hashlib.sha256(json.dumps(data["interventions"], sort_keys=True).encode()).hexdigest()
    if digest != data["intervention_hash"]:
        raise ValueError("intervention hash mismatch")
    return data


def replay_experiment(path: Path, wall_limit: float = 120) -> dict[str, Any]:
    """Run exactly the exported inputs; expose code/runtime drift in the new artifact."""
    data = read_experiment(path)
    traces = tuple(tuple(Demand(**{**d, "allowed": tuple(d["allowed"])}) for d in trace)
                   for trace in data["traces"])
    result = run_experiment(FactoryConfig.from_dict(data["factory"]),
                            ComparisonConditions.from_dict(data["conditions"]), tuple(data["policies"]),
                            Settings(**data["settings"]), traces=traces, wall_limit=wall_limit,
                            interventions=tuple(Intervention(**e) for e in data["interventions"]))
    result["replay_of"] = data["experiment_id"]
    result["replay_source_matches"] = data["source_hashes"] == source_manifest()
    result["replay_environment_matches"] = data["environment"] == result["environment"]
    result["notes"].append("Replay preserves inputs, not time-limited incumbent choices or wall times.")
    return result
