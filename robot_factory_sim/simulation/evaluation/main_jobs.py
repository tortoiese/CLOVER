"""Serial main-study launch/resume without overwriting or selecting successful retries."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from simulation.evaluation.comparison_inputs import file_sha256, read_json
from simulation.evaluation.study import load_frozen, run_study, save
from simulation.evaluation.study_shards import collect_study


def _execute_jobs(bundle: Path, phase: str, *, limit: int | None = None) -> dict[str, Any]:
    """Execute new shards serially, retaining every prior terminal/interrupted outcome."""
    if phase not in {"heldout", "stress"} or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError("invalid main phase/limit")
    ready, _ = read_json(bundle / "readiness.json")
    if ready.get("status") != "READY" or ready["jobs_sha256"] != file_sha256(bundle / "jobs.json"):
        raise ValueError("main jobs require verified READY and unchanged job matrix")
    plan, _ = read_json(bundle / "jobs.json")
    frozen, sha = load_frozen(Path(plan["frozen"]))
    if sha != ready["frozen_sha256"] or frozen["study_hash"] != plan["study_hash"]:
        raise ValueError("main job registration changed")
    log_path = bundle / "launches" / f"{phase}_{time.time_ns()}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {"schema": "clover-main-launch-v1", "phase": phase, "status": "RUNNING",
                              "records": [], "new_shards": 0}
    save(log_path, result)
    for job in [j for j in plan["jobs"] if j["phase"] == phase]:
        target = Path(job["output"])
        if target.exists():
            parent, _ = read_json(target / "manifest.json")
            if parent["study_hash"] != plan["study_hash"] or parent["shard_index"] != job["shard_index"]:
                raise ValueError("existing shard belongs to a different registration")
            if parent["status"] == "RUNNING" and parent.get("owner_pid"):
                import psutil

                try:
                    owner = psutil.Process(parent["owner_pid"])
                    if owner.create_time() == parent.get("owner_created_at"):
                        raise ValueError("shard owner is still active; parallel native runs are forbidden")
                except psutil.NoSuchProcess:
                    pass
            result["records"].append({"shard_index": job["shard_index"], "action": "PRESERVED_EXISTING",
                                      "status": parent["status"]})
            continue
        if limit is not None and result["new_shards"] >= limit:
            break
        if shutil.disk_usage(bundle).free < 2_000_000_000:
            result["status"] = "STOPPED_DISK_RESERVE"
            break
        parent = run_study(Path(plan["frozen"]), target, phase=phase, shard_index=job["shard_index"],
                           shard_count=job["shard_count"], suite_wall_limit=6000)
        result["new_shards"] += 1
        result["records"].append({"shard_index": job["shard_index"], "action": "EXECUTED",
                                  "status": parent["status"]})
        save(log_path, result)
    if result["status"] == "RUNNING":
        result["status"] = "BATCH_FINISHED"
    save(log_path, result)
    return result


def collect_jobs(bundle: Path, phase: str, output: Path) -> dict[str, Any]:
    """Collect all predeclared shards, retaining interrupted and failed denominators."""
    plan, _ = read_json(bundle / "jobs.json")
    paths = tuple(Path(j["output"]) / "manifest.json" for j in plan["jobs"] if j["phase"] == phase)
    if not paths or any(not p.exists() for p in paths):
        raise ValueError("phase has missing registered shards; continue execution before collecting")
    return collect_study(paths, output)


@contextmanager
def _host_lock(bundle: Path) -> Iterator[None]:
    """Prevent simultaneous held-out/stress launchers on the locked host."""
    import psutil

    folder = bundle / "active_launch.lock"
    if folder.exists():
        owner, _ = read_json(folder / "owner.json")
        try:
            process = psutil.Process(owner["pid"])
            if process.create_time() == owner["created_at"]:
                raise ValueError("another main launcher is active on this host")
        except psutil.NoSuchProcess:
            pass
        (folder / "owner.json").unlink()
        folder.rmdir()
    folder.mkdir()
    owner = {"pid": os.getpid(), "created_at": psutil.Process().create_time()}
    save(folder / "owner.json", owner)
    try:
        yield
    finally:
        recorded, _ = read_json(folder / "owner.json")
        if recorded == owner:
            (folder / "owner.json").unlink()
            folder.rmdir()


def execute_jobs(bundle: Path, phase: str, *, limit: int | None = None) -> dict[str, Any]:
    """Run a bounded batch serially; repeated calls continue without overwriting evidence."""
    with _host_lock(bundle):
        return _execute_jobs(bundle, phase, limit=limit)
