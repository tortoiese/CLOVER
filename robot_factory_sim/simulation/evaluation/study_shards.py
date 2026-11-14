"""Immutable study shards and a complete, duplicate-free analysis collection."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from simulation.evaluation.comparison_inputs import file_sha256, inside, read_json
from simulation.evaluation.study import RUN_SCHEMA, load_frozen, save


def _collection(paths: tuple[Path, ...]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    parents = []
    for path in paths:
        path = path.resolve()
        parent, sha = read_json(path)
        if parent.get("schema") != RUN_SCHEMA or "parts" in parent:
            raise ValueError("collection requires original study shards")
        parents.append((path, sha, parent))
    if not parents:
        raise ValueError("collection needs shards")
    first = parents[0][2]
    frozen, sha = load_frozen(Path(first["frozen"]))
    if sha != first["frozen_sha256"]:
        raise ValueError("frozen input changed")
    cases = [c for c in frozen["cases"] if c["phase"] == first["phase"]]
    expected_ids = [c["id"] for c in cases]
    count = first["shard_count"]
    if len(parents) != count or {p[2]["shard_index"] for p in parents} != set(range(count)):
        raise ValueError("missing or duplicate shard index")
    entries, parts = {}, []
    for path, digest, parent in parents:
        for key in ("phase", "stage", "frozen", "frozen_sha256", "study_hash", "shard_count", "phase_case_ids",
                    "run_wall_limit"):
            if parent.get(key) != first.get(key):
                raise ValueError("mixed shard registration: " + key)
        selected = [c for i, c in enumerate(cases) if i % count == parent["shard_index"]]
        if ([e["id"] for e in parent["entries"]] != [c["id"] for c in selected]
                or parent["phase_case_ids"] != expected_ids
                or parent["planned_runs"] != sum(len(c["policies"]) for c in selected)):
            raise ValueError("shard matrix differs from frozen plan")
        for entry in parent["entries"]:
            if entry["id"] in entries:
                raise ValueError("duplicate actual case across shards")
            row = copy.deepcopy(entry)
            row["suite_origin"] = {"manifest": str(path), "sha256": digest}
            entries[row["id"]] = row
        parts.append({"manifest": str(path), "sha256": digest})
    if set(entries) != set(expected_ids):
        raise ValueError("incomplete collected phase")
    return first, [entries[cid] for cid in expected_ids], sorted(parts, key=lambda p: p["manifest"])


def collect_study(paths: tuple[Path, ...], output: Path) -> dict[str, Any]:
    """Collect every declared shard while retaining failed and unstarted cases."""
    if output.exists():
        raise ValueError("collection output already exists")
    first, entries, parts = _collection(paths)
    result = {k: first[k] for k in ("schema", "phase", "stage", "frozen", "frozen_sha256", "study_hash",
                                   "phase_case_ids", "run_wall_limit")}
    result.update(entries=entries, parts=parts, planned_runs=sum(len(e["arms"]) for e in entries),
                  status="SUCCESS" if all(e["status"] == "SUCCESS" for e in entries) else "PARTIAL")
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, result)
    return result


def verify_collection(path: Path, parent: dict[str, Any]) -> None:
    """Rebuild a collection so changed/missing/overlapping shards cannot hide arms."""
    paths = []
    for part in parent["parts"]:
        target = Path(part["manifest"])
        if file_sha256(target) != part["sha256"]:
            raise ValueError("collected shard changed")
        paths.append(target)
    first, entries, parts = _collection(tuple(paths))
    if (entries != parent["entries"] or parts != parent["parts"]
            or parent["phase_case_ids"] != first["phase_case_ids"]
            or parent["planned_runs"] != sum(len(e["arms"]) for e in entries)):
        raise ValueError("collection differs from source shards")


def collected_suite(entry: dict[str, Any]) -> Path | None:
    """Resolve an original shard's confined suite, never trust an arbitrary absolute path."""
    origin = entry["suite_origin"]
    parent, sha = read_json(Path(origin["manifest"]))
    if sha != origin["sha256"]:
        raise ValueError("suite origin shard changed")
    original = next(e for e in parent["entries"] if e["id"] == entry["id"])
    if {k: v for k, v in entry.items() if k != "suite_origin"} != original:
        raise ValueError("collected case differs from origin")
    return inside(Path(origin["manifest"]).parent, original["suite"]) if original["suite"] else None
