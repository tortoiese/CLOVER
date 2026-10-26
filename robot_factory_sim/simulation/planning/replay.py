"""Portable JSON plan export and exact immutable-input reconstruction."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.planning.contracts import (
    MODEL_VERSION,
    OBJECTIVE_PROFILE,
    PLAN_SCHEMA,
    UNIT_PROFILE,
    CabinArc,
    CabinInput,
    Facility,
    JobInput,
    Reservation,
    ResourceInput,
    ResourceUse,
    Result,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
)


def snapshot_from_dict(value: dict[str, Any]) -> Snapshot:
    """Decode typed tuples; JSON never executes serialized Python code."""
    for key, expected in (("model_version", MODEL_VERSION), ("objective_profile", OBJECTIVE_PROFILE),
                          ("unit_profile", UNIT_PROFILE)):
        if value.get(key) != expected:
            raise ValueError(f"incompatible {key}: {value.get(key)!r}; expected {expected}")
    routes = []
    for raw in value["routes"]:
        row = dict(raw)
        for key in ("points", "floors", "lifts", "nodes", "prev_nodes", "segment_starts"):
            row[key] = tuple(tuple(p) if key == "points" else p for p in row.get(key, []))
        row["uses"] = tuple(ResourceUse(**r) for r in row.get("uses", []))
        row["rides"] = tuple(CabinArc(**r) for r in row.get("rides", []))
        routes.append(RouteInput(**row))
    jobs = tuple(JobInput(**{**j, "stations": tuple(j["stations"])}) for j in value["jobs"])
    cabins = tuple(CabinInput(**{**c, "floors": tuple(c["floors"])}) for c in value.get("cabins", []))
    return Snapshot(value["token"], value["topology"], value["now"], Settings(**value["settings"]),
                    tuple(RobotInput(**r) for r in value["robots"]), jobs,
                    tuple(Facility(**f) for f in value["facilities"]), tuple(routes),
                    tuple(Reservation(**r) for r in value["fixed"]), value["work_drain"], value["scope"],
                    tuple(ResourceInput(**r) for r in value.get("resources", [])), cabins,
                    value["model_version"], value["objective_profile"], value["unit_profile"],
                    tuple(tuple(p) for p in value.get("positions", [])),
                    tuple(tuple(n) for n in value.get("geometry", [])),
                    tuple(tuple(e) for e in value.get("edges", [])), value.get("clearance", 0),
                    tuple(tuple(o) for o in value.get("obstacles", [])))


def export_result(result: Result, path: Path) -> None:
    """Write input, plan, model hash, scope, incumbent and phase bounds together."""
    path.write_text(json.dumps({"schema": PLAN_SCHEMA, **asdict(result)}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def read_snapshot(path: Path) -> Snapshot:
    """Load a saved planning input for replay using either backend."""
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("plan JSON exceeds 64MiB input limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != PLAN_SCHEMA:
        raise ValueError("unsupported plan schema")
    return snapshot_from_dict(data["snapshot"])
