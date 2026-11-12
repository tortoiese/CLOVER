"""Explicit versioned plan persistence; earlier execution schemas are rejected."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from simulation.planning.contracts import (
    PLAN_SCHEMA,
    SNAPSHOT_SCHEMA,
    Action,
    Arc,
    CabinInput,
    CabinUse,
    ExcludedJob,
    FacilityInput,
    JobInput,
    PhysicalClaim,
    PlannedHold,
    PreparedInput,
    Report,
    ResourceHold,
    ResourceInput,
    Result,
    RobotInput,
    RouteInput,
    Settings,
    Snapshot,
    Use,
)
from simulation.traffic_rules import TrafficReservation


def encode(result: Result) -> dict[str, Any]:
    """Export full decision evidence, including actual input and returned actions."""
    return {"schema": PLAN_SCHEMA, **asdict(result)}


def decode(raw: dict[str, Any]) -> Result:
    """Restore only the explicit current schema, normalizing immutable tuples."""
    if raw.get("schema") != PLAN_SCHEMA or raw["input"]["snapshot"].get("schema") != SNAPSHOT_SCHEMA:
        raise ValueError("unsupported recorded planning schema")
    data = raw["input"]
    row = dict(data["snapshot"])
    row["settings"] = Settings(**row["settings"])
    for key, cls in (
        ("robots", RobotInput),
        ("facilities", FacilityInput),
        ("resources", ResourceInput),
        ("claims", PhysicalClaim),
        ("cabins", CabinInput),
        ("excluded", ExcludedJob),
        ("committed", PlannedHold),
        ("holds", ResourceHold),
    ):
        row[key] = tuple(cls(**value) for value in row[key])
    row["jobs"] = tuple(JobInput(**{**j, "stations": tuple(j["stations"])}) for j in row["jobs"])
    routes = []
    for route in data["routes"]:
        item = dict(route)
        item["points"] = tuple(tuple(p) for p in item["points"])
        item["use_sections"] = tuple(tuple(section) for section in item["use_sections"])
        for key in ("floors", "lifts", "nodes", "previous_nodes", "segment_starts", "segment_ends"):
            item[key] = tuple(item[key])
        item["uses"] = tuple(TrafficReservation(**u) for u in item["uses"])
        item["cabins"] = tuple(CabinUse(**c) for c in item["cabins"])
        routes.append(RouteInput(**item))
    prepared = PreparedInput(Snapshot(**row), tuple(routes), tuple(tuple(v) for v in data["unavailable_routes"]))
    actions = []
    for item in raw["actions"]:
        arc = dict(item["arc"])
        arc["uses"] = tuple(Use(**u) for u in arc["uses"])
        arc["cabins"] = tuple(arc["cabins"])
        actions.append(Action(Arc(**arc), item["gain"]))
    return Result(prepared, Report(**raw["report"]), tuple(actions), tuple(raw["deferred"]))
