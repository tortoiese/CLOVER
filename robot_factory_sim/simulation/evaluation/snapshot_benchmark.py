"""Serial, isolated compilation/search comparisons of one frozen grid model."""

from __future__ import annotations

import csv
import random
import time
from collections import Counter
from pathlib import Path
from typing import Any

from simulation.evaluation.design import write_json
from simulation.evaluation.frozen_snapshot import load_snapshot, sha256
from simulation.evaluation.runner import environment_manifest, source_manifest
from simulation.evaluation.scenario import integer, number
from simulation.planning.process import SolverProcess
from simulation.planning.replay import encode

BACKENDS = ("rh_milp_grid", "rh_cpsat_linear")


def benchmark_snapshot(
    snapshot: Path, output: Path, *, budgets: tuple[float, ...] = (.25, 2), repeats: int = 3,
    order_seed: int = 20261114, startup_limit: float = 10, request_allowance: float = 3,
    memory_limit_mib: float = 1024, suite_wall_limit: float = 300,
) -> dict[str, Any]:
    """Keep every planned arm and distinguish soft budgets from hard guards.

    Model/hint construction, worker bootstrap, transfer and independent plan
    validation are reported separately from the worker's timed solve scope.
    Repeats reuse one observation and are never statistical demand replicates.
    """
    if output.exists():
        raise ValueError("benchmark output already exists")
    if not budgets or len(budgets) > 8 or len(set(budgets)) != len(budgets):
        raise ValueError("provide one to eight unique compute budgets")
    for budget in budgets:
        number(budget, "compute budget", .001, 60)
    integer(repeats, "technical repeats", 1, 100)
    integer(order_seed, "order seed")
    number(startup_limit, "startup limit", .001, 60)
    number(request_allowance, "request allowance", .001, 60)
    number(memory_limit_mib, "worker memory", 1, 8192)
    number(suite_wall_limit, "suite wall limit", .001, 3600)
    source, original_hash = source_manifest(), sha256(snapshot)
    started = time.perf_counter()
    frozen, model, hint = load_snapshot(snapshot)
    shared_load_seconds = time.perf_counter() - started
    planned = [{"backend": backend, "budget_seconds": budget, "repeat": repeat}
               for budget in budgets for repeat in range(repeats) for backend in BACKENDS]
    random.Random(order_seed).shuffle(planned)
    manifest = {
        "schema": "clover-frozen-grid-benchmark-v1", "status": "RUNNING", "source": source,
        "environment": environment_manifest(), "snapshot_path": str(snapshot.resolve()),
        "snapshot_sha256": original_hash, "artifact_hash": frozen["artifact_hash"],
        "input_hash": frozen["input_hash"], "model_hash": frozen["model_hash"],
        "hint_hash": frozen["hint_hash"], "origin": frozen["origin"],
        "settings": {"budgets": budgets, "repeats": repeats, "order_seed": order_seed,
                     "startup_limit": startup_limit, "request_allowance": request_allowance,
                     "memory_limit_mib": memory_limit_mib, "suite_wall_limit": suite_wall_limit,
                     "solver_seed": 42, "solver_workers": 1},
        "shared_load_seconds": shared_load_seconds, "planned_runs": len(planned),
        "runs": [{**spec, "index": index, "result": None, "status": "NOT_STARTED"}
                 for index, spec in enumerate(planned)],
        "parity_mismatches": [],
        "timed_scope": "prebuilt grid solve: hint check, compile, two objectives, extraction; soft limit",
        "excluded_scope": "common model/hint preparation, selected-backend bootstrap, IPC, plan validation",
        "scope": "technical repeats of one observation; no D1 guarantee, CI, p-value or policy recommendation",
    }
    output.mkdir(parents=True)
    write_json(output / "snapshot.json", frozen)

    def checkpoint() -> None:
        temporary = output / "manifest.pending.json"
        write_json(temporary, manifest)
        temporary.replace(output / "manifest.json")

    def remaining() -> float:
        return suite_wall_limit - (time.perf_counter() - started)

    def drift() -> str | None:
        if source_manifest()["source_hash"] != source["source_hash"]:
            return "SOURCE_CHANGED"
        if not snapshot.is_file() or sha256(snapshot) != original_hash:
            return "SNAPSHOT_CHANGED"
        return None

    checkpoint()
    stopped = None
    optimum: tuple[int | None, int | None] | None = None
    for row in manifest["runs"]:
        index = row["index"]
        spec = {key: row[key] for key in ("backend", "budget_seconds", "repeat")}
        stopped = stopped or drift()
        row["status"] = stopped or "NOT_RUN_BUDGET"
        if stopped or remaining() <= 0:
            checkpoint()
            continue
        row["status"] = "RUNNING"
        checkpoint()
        worker = SolverProcess()
        artifact: dict[str, Any] = {"spec": spec, "source_hash": source["source_hash"],
                                    "input_hash": frozen["input_hash"], "model_hash": frozen["model_hash"],
                                    "hint_hash": frozen["hint_hash"]}
        before = time.perf_counter()
        try:
            startup = worker.request("ready", spec["backend"], {}, request_id="ready", epoch="snapshot-benchmark",
                                     timeout=min(startup_limit, max(.001, remaining())),
                                     memory_limit_mib=memory_limit_mib)
            artifact["startup"] = startup
            row["status"] = startup["status"]
            row["startup_wall_seconds"] = startup["elapsed_seconds"]
            row["worker_peak_rss_mib"] = startup["peak_rss_mib"]
            stopped = stopped or drift()
            if stopped:
                row["status"] = stopped
            elif startup["status"] == "READY" and remaining() > 0:
                guard = min(spec["budget_seconds"] + request_allowance, max(.001, remaining()))
                reply = worker.request(
                    "frozen_grid", spec["backend"], {"model": model, "hint": hint,
                                                     "budget_seconds": spec["budget_seconds"]},
                    request_id=f"arm-{index}", epoch="snapshot-benchmark", timeout=guard,
                    memory_limit_mib=memory_limit_mib,
                )
                artifact["reply"] = {key: value for key, value in reply.items() if key != "value"}
                artifact["request_watchdog_seconds"] = guard
                row["status"] = reply["status"]
                row["request_wall_seconds"] = reply["elapsed_seconds"]
                row["worker_peak_rss_mib"] = max(row["worker_peak_rss_mib"], reply["peak_rss_mib"])
                if reply["status"] == "OK":
                    value = reply["value"]
                    result, errors = value["plan"], value["validation_errors"]
                    artifact.update(result=encode(result), validation_errors=list(errors),
                                    validation_seconds=value["validation_seconds"])
                    report = result.report
                    matching = (result.input == model.input
                                and report.backend == {"rh_milp_grid": "milp", "rh_cpsat_linear": "cpsat_linear"}[
                                    spec["backend"]] and (
                        report.status not in {"OPTIMAL", "FEASIBLE"} or report.model_hash == model.fingerprint))
                    valid = matching and not errors and report.status in {"OPTIMAL", "FEASIBLE"}
                    within = report.total_seconds <= spec["budget_seconds"]
                    row.update(backend_status=report.status, valid_plan=valid,
                               within_soft_budget=within, timed_eligible=valid and within,
                               phase1_cost=report.phase1_cost, phase2_cost=report.phase2_cost,
                               phase1_bound=report.phase1_bound, phase2_bound=report.phase2_bound,
                               backend_seconds=report.total_seconds, compile_seconds=report.compile_seconds,
                               search_seconds=report.search_seconds, backend_cpu_seconds=report.worker_cpu_seconds,
                               validation_seconds=value["validation_seconds"])
                    row["status"] = ("MODEL_MISMATCH" if not matching else "INVALID_PLAN" if errors else
                                     "SOFT_BUDGET_EXCEEDED" if valid and not within else report.status)
                    if valid and report.status == "OPTIMAL":
                        key = (spec["budget_seconds"], spec["repeat"])
                        objective = (report.phase1_cost, report.phase2_cost)
                        if optimum is not None and optimum != objective:
                            manifest["parity_mismatches"].append({"budget_repeat": key,
                                                                  "objectives": [optimum, objective]})
                        optimum = objective if optimum is None else optimum
            elif startup["status"] == "READY":
                row["status"] = "NOT_RUN_BUDGET"
        except Exception as exc:
            row["status"] = "EVALUATION_ERROR"
            row["failure"] = f"{type(exc).__name__}: {exc}"
            artifact["error"] = row["failure"]
            stopped = "NOT_RUN_EVALUATION_ERROR"
        finally:
            close_completed = False
            try:
                worker.close()
                close_completed = True
            except Exception as exc:
                artifact["disposal_error"] = f"{type(exc).__name__}: {exc}"
            artifact["disposed"] = {"process": worker.process is None, "connection": worker.connection is None,
                                    "rpc_thread": worker.rpc_thread is None, "close_completed": close_completed}
            if not all(artifact["disposed"].values()):
                row["status"] = "DISPOSAL_FAILED"
                stopped = "NOT_RUN_DISPOSAL_FAILED"
        row["wall_seconds"] = time.perf_counter() - before
        row["status"] = drift() or row["status"]
        artifact["summary"] = dict(row)
        relative = f"arm-{index:04d}.json"
        write_json(output / relative, artifact)
        row["result"] = relative
        row["result_sha256"] = sha256(output / relative)
        checkpoint()
    manifest["status"] = ("PARITY_MISMATCH" if manifest["parity_mismatches"] else
                          "SUCCESS" if all(r["status"] in {"OPTIMAL", "FEASIBLE"} for r in manifest["runs"])
                          else "PARTIAL")
    manifest["final_drift"] = drift()
    if manifest["final_drift"]:
        manifest["status"] = "PARTIAL"
    manifest["wall_seconds"] = time.perf_counter() - started
    manifest["statuses"] = dict(Counter(row["status"] for row in manifest["runs"]))
    checkpoint()
    fields = list(dict.fromkeys(key for row in manifest["runs"] for key in row))
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(manifest["runs"])
    return manifest
