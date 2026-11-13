"""Representation-independent specification identity, including time resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from simulation.planning.contracts import PreparedInput


def semantic_spec_hash(data: PreparedInput) -> str:
    """Hash the same geometry, time domain and complete/bounded action scope."""
    payload = asdict(data)
    snapshot = payload["snapshot"]
    snapshot.pop("token")
    snapshot.pop("epoch")
    cfg = data.snapshot.settings
    horizon = int(cfg.horizon / cfg.time_unit + 1e-9)
    bound = min(cfg.native_actions, horizon) if cfg.formulation == "native" and cfg.native_actions else horizon
    snapshot["settings"] = {
        "horizon": cfg.horizon,
        "unit": cfg.time_unit,
        "sigma": cfg.sigma,
        "joint_cabins": cfg.joint_cabins,
        "action_scope": "complete" if bound == horizon else bound,
        "objective_grid_step": cfg.step,
        "tail_seconds": cfg.max_tail_ticks * cfg.step,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
