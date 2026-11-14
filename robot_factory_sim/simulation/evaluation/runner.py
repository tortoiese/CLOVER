"""Paired, GUI-free runs with full event streams, invariants and replay receipts."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.metadata
import json
import platform
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

import numpy as np

import config
from simulation.audit import assert_invariants, audit_engine, energy_ledger
from simulation.engine import SimulationEngine
from simulation.evaluation.digest import PhysicalDigest
from simulation.evaluation.resource_metrics import ResourceMetrics
from simulation.evaluation.scenario import (
    FOUNDATION_POLICIES,
    POLICIES,
    RH_POLICIES,
    Demand,
    Scenario,
    fingerprint,
    integer,
    make_trace,
    number,
    validate_trace,
)
from simulation.evaluation.statistics import distribution
from simulation.resource_pool import TRAFFIC_CONTRACT
from simulation.robot import InterruptKind
from simulation.task_generator import TaskState

RESULT_SCHEMA = "clover-headless-run-v4"
DECISION_RESULT_SCHEMA = "clover-headless-run-v5"
CORE_VERSION = "common-model-v2.0-staged-d1"
BASE_COMMIT = "a28522d9dd9aee238c27fdc2404cab2b1b10c60d"


def source_manifest() -> dict[str, Any]:
    """Identify the actual local source independently of a missing Git checkout."""
    root = Path(__file__).resolve().parents[2]
    hashes = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*.py"))
        if "__pycache__" not in p.parts
    }
    return {
        "base_commit": BASE_COMMIT,
        "source_commit": None,
        "source_hash": fingerprint(hashes),
        "source_hashes": hashes,
        "core_version": CORE_VERSION,
        "traffic_contract": TRAFFIC_CONTRACT,
    }


def environment_manifest() -> dict[str, Any]:
    """Read package versions without importing GUI, RAG or solver modules."""
    optional = {}
    for name in ("ortools", "psutil", "scipy"):
        try:
            optional[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            optional[name] = None
    return {
        "wall_clock": {"name": "perf_counter",
                       "implementation": time.get_clock_info("perf_counter").implementation,
                       "resolution_seconds": time.get_clock_info("perf_counter").resolution,
                       "monotonic": time.get_clock_info("perf_counter").monotonic},
        "python": platform.python_version(),
        "os": platform.system(),
        "machine": platform.machine(),
        "packages": {name: importlib.metadata.version(name) for name in ("simpy", "numpy")} | optional,
    }


def _physical_state(engine: SimulationEngine) -> dict[str, Any]:
    return {
        "traffic": [asdict(snapshot) for snapshot in engine.corridors.snapshots()],
        "stations": [
            {
                "id": s.id,
                "occupied": s.resource.count,
                "waiting": s.waiting,
                "task": s.current_task.external_uid if s.current_task else None,
            }
            for s in engine.stations.values()
        ],
        "chargers": [
            {"id": c.id, "occupied": c.resource.count, "waiting": c.waiting, "robots": sorted(c.occupants)}
            for c in engine.chargers.values()
        ],
        "cabins": [
            {"id": e.id, "floor": e.floor_position(), "moving": e.moving, "occupied": e.occupied, "waiting": e.waiting}
            for e in engine.elevators.values()
        ],
        "robots": [
            {
                "id": r.robot_id,
                "state": r.state,
                "alive": r.alive,
                "position": r.position(),
                "floor": r.floor_position(),
                "target": r.target_id,
            }
            for r in [*engine.robots.values(), *engine.retired]
        ],
    }


class Observations:
    """Stream raw events and integrate WIP exactly at each logical transition."""

    def __init__(self, scenario: Scenario, stream: TextIO | None) -> None:
        self.start, self.end = scenario.warmup, scenario.duration
        self.stream = stream
        self.last = 0.0
        self.wip = 0
        self.area = 0.0
        self.samples: dict[str, list[float]] = defaultdict(list)
        self.dispatch_times: list[float] = []
        self.traffic_grants = 0
        self.faults = 0
        self.digest = hashlib.sha256()
        self.physical_digest = PhysicalDigest()
        self.resources = ResourceMetrics(scenario.warmup, scenario.duration, config.DEADLOCK_THRESHOLD_SEC)

    def integrate(self, now: float) -> None:
        """Integrate only the requested measurement window."""
        self.area += max(0, min(now, self.end) - max(self.last, self.start)) * self.wip
        self.last = now

    def record(self, row: dict[str, Any]) -> None:
        """Record the actual execution event and maintain independent statistics."""
        now = row["time"]
        self.integrate(now)
        self.resources.record(row)
        self.physical_digest.record(row)
        if row["event"] == "task_offered" and row["status"] != TaskState.REJECTED:
            self.wip += 1
        if row["event"] == "task_state" and row["status"] in TaskState.TERMINAL:
            self.wip -= 1
        if row["event"] == "metric_sample" and self.start <= now < self.end:
            self.samples[row["channel"]].append(row["value"])
        if row["event"] == "dispatch":
            self.dispatch_times.append(row["wall_seconds"])
        if row["event"] == "traffic" and row["action"] == "granted":
            self.traffic_grants += 1
            if any(r["occupied"] > r["capacity"] for r in row["bundle"]):
                raise AssertionError("physical traffic grant exceeded capacity")
        if row["event"] == "robot_fault":
            self.faults += 1
        raw = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if self.stream is not None:
            self.stream.write(raw + "\n")
        # Wall timings vary across machines; plant event identity must not.
        physical = {key: value for key, value in row.items() if key != "wall_seconds"}
        self.digest.update(json.dumps(physical, sort_keys=True, allow_nan=False).encode() + b"\n")


def _inject(engine: SimulationEngine, trace: tuple[Demand, ...]) -> Any:
    for job in trace:
        yield engine.env.timeout(max(0, job.at - engine.now))
        task = engine.add_task(
            job.operation,
            job.station,
            job.priority,
            job.allowed,
            deadline=job.deadline,
            external_uid=job.uid,
            operation=job.operation,
        )
        task.process_noise = job.noise


def _intervene(engine: SimulationEngine, scenario: Scenario) -> Any:
    for event in scenario.interventions:
        yield engine.env.timeout(max(0, event.at - engine.now))
        applied = True
        if event.kind == "cancel_task":
            applied = engine.cancel_task(event.target)
        elif event.kind == "robot_retire":
            robot = engine.robots.get(event.target)
            applied = robot is not None and robot.interrupt(InterruptKind.RETIRE)
        else:
            cfg = copy.deepcopy(engine.factory)
            group = cfg.stations if event.kind == "station_active" else cfg.chargers
            if event.kind == "edge_width":
                next(edge for edge in cfg.edges if str(edge.id) == event.target).width = event.value
            else:
                next(item for item in group if item.id == event.target).is_active = event.value
            engine.apply_config(cfg)
        engine.ledger.emit("intervention", **asdict(event), applied=bool(applied))


def run_case(
    scenario: Scenario,
    policy: str,
    seed: int,
    *,
    trace: tuple[Demand, ...] | None = None,
    output: Path | None = None,
    wall_limit: float = 60,
    cancelled: Callable[[], bool] | None = None,
    planning_settings: dict[str, Any] | None = None,
    replay_decisions: list[dict[str, Any]] | None = None,
    execution_epoch: str | None = None,
    decision_settings: dict[str, Any] | None = None,
    replay_controls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Execute one arm with its own watchdog; retain partial results on runtime failure."""
    scenario.validate()
    integer(seed, "seed")
    number(wall_limit, "wall_limit")
    if policy not in POLICIES:
        raise ValueError("unsupported evaluation policy")
    if decision_settings is not None:
        from simulation.decisions.contracts import DecisionSettings
        DecisionSettings(**decision_settings).validate()
        if planning_settings and planning_settings.get("timing_mode","D0") != "D0":
            raise ValueError("prototype timing conflicts with staged D1")
    if planning_settings and planning_settings.get("timing_mode", "D0") == "D1" and policy not in RH_POLICIES:
        raise ValueError("D1 delay injection currently supports RH policies only; do not mix D0 and D1 arms")
    trace = make_trace(scenario, seed) if trace is None else trace
    validate_trace(scenario, trace)
    source_at_start = source_manifest()
    case_id = f"{scenario.id}__{policy}__{seed}"
    stream = None
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
        stream = (output / f"{case_id}.events.jsonl").open("x", encoding="utf-8")
    observations = Observations(scenario, stream)
    engine = None
    external_processes = []
    status, failure = "SUCCESS", None
    checks, maximum_error = 0, 0.0
    started = time.perf_counter()
    window_energy: dict[str, Any] | None = None
    try:
        engine = SimulationEngine(
            copy.deepcopy(scenario.factory),
            policy,
            seed=seed,
            comparison_seed=seed,
            epoch=datetime(2026, 1, 1),
            demand_source="external",
            params={"task_rate": 0},
            plant=scenario.plant,
            initial_robots=scenario.initial_robots,
            initial_cabins=scenario.initial_cabins,
            event_sink=observations.record,
            planning_settings=planning_settings if policy in RH_POLICIES else None,
            replay_decisions=replay_decisions,
            execution_epoch=execution_epoch,
            decision_settings=decision_settings,
            replay_controls=replay_controls,
            roam=False,
        )
        if decision_settings is not None:
            from simulation.decisions.input_store import PlanningInputStore

            engine.scheduler.input_store = PlanningInputStore(output)
        external_processes = [
            engine.env.process(_inject(engine, trace)),
            engine.env.process(_intervene(engine, scenario)),
        ]
        if scenario.warmup == 0:
            window_energy = energy_ledger(engine)
        while engine.now < scenario.duration:
            if cancelled and cancelled():
                raise InterruptedError("run cancelled")
            if time.perf_counter() - started > wall_limit:
                raise TimeoutError("per-run wall limit")
            target = min(scenario.duration, engine.now + scenario.check_interval)
            if window_energy is None:
                target = min(target, scenario.warmup)
            engine.env.run(until=target)
            audit = assert_invariants(engine)
            live = sum(t.status not in TaskState.TERMINAL for t in engine.tasks.values())
            if observations.wip != live:
                raise AssertionError("event WIP differs from task registry")
            checks += 1
            maximum_error = max(maximum_error, audit["energy"]["max_balance_error"])
            expected = {job.uid for job in trace if job.at < engine.now}
            if set(engine._tasks_by_uid) != expected:
                raise AssertionError("offered demand trace does not match engine registry")
            if window_energy is None and engine.now >= scenario.warmup:
                window_energy = energy_ledger(engine)
    except Exception as exc:
        status = (
            "CANCELLED"
            if isinstance(exc, InterruptedError)
            else "TIMEOUT"
            if isinstance(exc, TimeoutError)
            else "FAILED"
        )
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if engine is not None:
            engine.ledger.sink = None
        observations.stream = None
        if stream is not None:
            stream.close()
    observed = engine.now if engine is not None else 0
    observations.integrate(observed)
    tasks = list(engine.tasks.values()) if engine is not None else []
    counts = Counter(t.status for t in tasks)
    cohort = [t for t in tasks if scenario.warmup <= t.created_at < observed]
    completed = [t for t in tasks if t.completed_at is not None and scenario.warmup <= t.completed_at < observed]
    waits = [t.first_started_at - t.created_at for t in cohort if t.first_started_at is not None]
    cycles = [t.completed_at - t.created_at for t in cohort if t.completed_at is not None]
    due = [
        t
        for t in tasks
        if t.deadline is not None
        and scenario.warmup <= t.deadline < observed
        and not (t.cancelled and t.terminal_at is not None and t.terminal_at <= t.deadline)
    ]
    late = sum(t.completed_at is None or t.completed_at > t.deadline for t in due)
    final_energy = energy_ledger(engine) if engine is not None else None
    energy_window = (
        {
            key: final_energy[key] - window_energy[key]
            for key in ("travel", "work", "consumed", "charged", "rescue", "distance_m")
        }
        if window_energy is not None and final_energy is not None
        else None
    )
    audit = audit_engine(engine) if engine is not None else None
    source_at_end = source_manifest()
    if source_at_start["source_hash"] != source_at_end["source_hash"]:
        status, failure = "SOURCE_CHANGED", "source changed during execution"
    span = max(0, observed - scenario.warmup)
    planning = None
    control = None
    if engine is not None and decision_settings is not None:
        scheduler = engine.scheduler
        control = {"settings":scheduler.settings.to_dict(),"math_settings":asdict(scheduler.math),
            "decisions":scheduler.records,"decisions_hash":fingerprint(scheduler.records),
            "lifecycle":scheduler.lifecycle,"lifecycle_hash":fingerprint(scheduler.lifecycle),
            "policy_state":scheduler.policy_state,"fallbacks":dict(scheduler.fallbacks),
            "coverage_ok":scheduler.coverage_ok,"restarts":scheduler.restarts}
        from simulation.decisions.contracts import digest as decision_digest

        control["planning_inputs"] = scheduler.input_store.records
        control["planning_inputs_hash"] = decision_digest(scheduler.input_store.records)
        from simulation.decisions.metrics import control_summary

        control["summary"] = control_summary(control, scenario.warmup, observed)
        if not scheduler.coverage_ok:
            status,failure="TIMING_COVERAGE_VIOLATION","publication cost exceeded prepaid allowance"
        control_errors=[r["reason"] for r in scheduler.records if r["reason"].startswith("DECISION_ERROR")]
        if control_errors:
            status,failure="FAILED","; ".join(control_errors)
        if replay_controls is not None and len(scheduler.records)!=len(replay_controls):
            status,failure="FAILED",(str(failure)+"; " if failure else "")+"unconsumed recorded control decisions"
    elif engine is not None and policy in RH_POLICIES:
        scheduler = engine.scheduler
        planning = {
            "settings": asdict(scheduler.settings),
            "decisions": scheduler.records,
            "decisions_hash": fingerprint(scheduler.records),
            "solver_statuses": dict(scheduler.statuses),
            "fallbacks": dict(scheduler.fallbacks),
            "accepted": sum(r["accepted"] for r in scheduler.records),
            "native_primary_statuses": dict(
                Counter(r["result"]["report"]["native_phase1_status"] for r in scheduler.records)
            ),
            "replay_mode": "recorded validated decisions; native solver is not rerun",
        }
        planning["timing_mode"] = scheduler.settings.timing_mode
        planning["pending_at_end"] = sum(r["time"] is None for r in scheduler.records)
        planning["latency_seconds"] = distribution([r["latency_seconds"] for r in scheduler.records])
        if replay_decisions is not None and len(scheduler.records) != len(replay_decisions):
            status, failure = "FAILED", "recorded planning decisions left unconsumed"
    result = {
        "schema": DECISION_RESULT_SCHEMA if decision_settings is not None else RESULT_SCHEMA,
        "case_id": case_id,
        "policy": policy,
        "seed": seed,
        "status": status,
        "failure": failure,
        "execution_epoch": engine.execution_epoch if engine is not None else execution_epoch,
        "planning": planning,
        "control": control,
        "scenario": scenario.to_dict(),
        "scenario_hash": fingerprint(scenario.to_dict()),
        "initial_hash": fingerprint(
            {"robots": {k: asdict(v) for k, v in scenario.initial_robots.items()}, "cabins": scenario.initial_cabins}
        ),
        "trace": [asdict(job) for job in trace],
        "trace_hash": fingerprint([asdict(job) for job in trace]),
        "observed_seconds": observed,
        "measurement_window": [min(scenario.warmup, observed), observed],
        "offered": len(tasks),
        "scheduled_arrivals": len(trace),
        "state_counts": dict(counts),
        "completed_total": counts[TaskState.COMPLETED],
        "completed_in_window": len(completed),
        "unfinished": sum(t.status not in TaskState.TERMINAL for t in tasks),
        "rejected": counts[TaskState.REJECTED],
        "cancelled": counts[TaskState.CANCELLED],
        "throughput_per_min": 60 * len(completed) / span if span > 0 else None,
        "due_jobs": len(due),
        "late_due_jobs": late,
        "late_due_rate": late / len(due) if due else None,
        "arrival_to_start": distribution(waits),
        "arrival_to_completion": distribution(cycles),
        "censored_start_count": sum(t.first_started_at is None and t.status not in TaskState.TERMINAL for t in cohort),
        "unfinished_ages": {
            t.external_uid: observed - t.created_at for t in cohort if t.status not in TaskState.TERMINAL
        },
        "wip_integral": observations.area,
        "mean_wip": observations.area / span if span > 0 else None,
        "queue_samples": {channel: distribution(values) for channel, values in observations.samples.items()},
        "resource_waits": observations.resources.waits_snapshot(observed),
        "cabin_phases": observations.resources.cabins_snapshot(observed),
        "energy": final_energy,
        "measurement_energy": energy_window,
        "energy_per_completed": energy_window["consumed"] / len(completed) if energy_window and completed else None,
        "faults": observations.faults,
        "traffic_grants": observations.traffic_grants,
        "long_wait_episodes_completed": engine.metrics.long_wait_count if engine is not None else 0,
        "robot_state_seconds": {r.robot_id: r.usage().state_time for r in [*engine.robots.values(), *engine.retired]}
        if engine is not None
        else {},
        "dispatch_wall": distribution(observations.dispatch_times),
        "wall_seconds": time.perf_counter() - started,
        "run_wall_limit": wall_limit,
        "event_digest": observations.digest.hexdigest(),
        "physical_event_digest": observations.physical_digest.hexdigest(),
        "physical_digest_scope": "exclude observation-only events, renumber sequence; retain all old event payloads",
        "invariant_checks": checks,
        "max_energy_error": maximum_error,
        "final_audit": audit,
        "final_physical_state": _physical_state(engine) if engine is not None else None,
        "timing_mode": (
            control["settings"]["timing_protocol"] + "; measured phases plus prepaid publication allowance"
            if control is not None else
            "D1-RH; measured proposal wall delay injected; apply revalidation wall time recorded separately"
            if planning and planning["timing_mode"] == "D1"
            else "D0; computation wall time is not plant decision latency"
        ),
        "legacy_planning": (
            engine.scheduler.solve_records if engine is not None and policy == "assignment_milp_legacy"
            and decision_settings is None else None
        ),
        "legacy_scope": (
            ("assignment only; soft 0.25s native limit; staged D1 replay uses recorded validated decisions"
             if control is not None else
             "assignment only; soft 0.25s native limit; replay reruns solver, not a recorded-decision guarantee")
            if policy == "assignment_milp_legacy"
            else None
        ),
        "deadlock_detection": "not_implemented; long waits are not proof of deadlock",
        "source": source_at_start,
        "source_hash_at_end": source_at_end["source_hash"],
        "environment": environment_manifest(),
        "tasks": [
            {
                "uid": t.external_uid,
                "status": t.status,
                "operation": t.operation,
                "created": t.created_at,
                "assigned": t.first_assigned_at,
                "started": t.first_started_at,
                "completed": t.completed_at,
                "terminal": t.terminal_at,
                "attempts": t.attempts,
                "robot": t.robot_id,
                "station": t.station_id,
                "reason": t.terminal_reason,
            }
            for t in tasks
        ],
    }
    for process in external_processes:
        if process.is_alive:
            process._generator.close()
    if engine is not None:
        engine.close()
        if control is not None:
            worker=engine.scheduler.worker
            control["disposed"]={"process":worker.process is None,"connection":worker.connection is None,
                "rpc_thread":worker.rpc_thread is None,
                "application_processes":all(not p.is_alive or p._generator.gi_frame is None
                    for p in engine.scheduler.processes)}
            if not all(control["disposed"].values()):
                result["status"],result["failure"]="FAILED","control resources not fully disposed"
    if output is not None:
        (output / f"{case_id}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
        )
    return result


def replay(result: dict[str, Any], *, require_same_source: bool = True,
           input_root: Path | None = None) -> dict[str, Any]:
    """Re-run exact recorded plant input and trace, checking physical event identity."""
    if result.get("schema") not in {RESULT_SCHEMA, DECISION_RESULT_SCHEMA}:
        raise ValueError("unsupported replay schema")
    if require_same_source and result["source"]["source_hash"] != source_manifest()["source_hash"]:
        raise ValueError("replay source differs from original run")
    scenario = Scenario.from_dict(result["scenario"])
    if fingerprint(scenario.to_dict()) != result["scenario_hash"]:
        raise ValueError("replay scenario hash mismatch")
    trace = tuple(Demand.from_dict(row) for row in result["trace"])
    if fingerprint([asdict(job) for job in trace]) != result["trace_hash"]:
        raise ValueError("replay trace hash mismatch")
    planning = result.get("planning")
    control = result.get("control")
    if control and "planning_inputs" in control:
        from simulation.decisions.contracts import digest as decision_digest
        from simulation.decisions.input_store import load_planning_input

        index = control["planning_inputs"]
        if decision_digest(index) != control["planning_inputs_hash"]:
            raise ValueError("recorded planning input index hash mismatch")
        for row in control["decisions"]:
            ref = row.get("planning_input_ref")
            if ref is None:
                continue
            if ref not in index:
                raise ValueError("recorded planning input reference is missing")
            if input_root is not None or "inline" in index[ref]:
                load_planning_input(control, ref, input_root)
    if control is not None:
        if (fingerprint(control["decisions"])!=control["decisions_hash"]
                or fingerprint(control["lifecycle"])!=control["lifecycle_hash"]):
            raise ValueError("recorded control/lifecycle hash mismatch")
    if planning is not None and fingerprint(planning["decisions"]) != planning["decisions_hash"]:
        raise ValueError("recorded planning decision hash mismatch")
    rerun = run_case(
        scenario,
        result["policy"],
        result["seed"],
        trace=trace,
        wall_limit=result.get("run_wall_limit", 60),
        planning_settings=control["math_settings"] if control and result["policy"] in RH_POLICIES else (
            planning["settings"] if planning else None),
        replay_decisions=planning["decisions"] if planning else None,
        execution_epoch=result["execution_epoch"],
        decision_settings=control["settings"] if control else None,
        replay_controls=control["decisions"] if control else None,
    )
    return {
        "matches": rerun["event_digest"] == result["event_digest"] and rerun["status"] == result["status"],
        "expected_digest": result["event_digest"],
        "actual_digest": rerun["event_digest"],
        "result": rerun,
    }


def run_suite(
    scenarios: tuple[Scenario, ...],
    seeds: tuple[int, ...],
    output: Path,
    *,
    policies: tuple[str, ...] = FOUNDATION_POLICIES,
    run_wall_limit: float = 60,
    suite_wall_limit: float | None = None,
    planning_settings: dict[str, Any] | None = None,
    decision_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run paired arms in randomized block order and checkpoint every outcome."""
    if not scenarios or len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("suite requires unique scenarios")
    if not seeds or len(set(seeds)) != len(seeds):
        raise ValueError("suite requires unique seeds")
    if not policies or len(set(policies)) != len(policies) or any(p not in POLICIES for p in policies):
        raise ValueError("invalid policy selection")
    for scenario in scenarios:
        scenario.validate()
    if decision_settings is not None:
        from simulation.decisions.contracts import DecisionSettings
        DecisionSettings(**decision_settings).validate()
        if planning_settings and planning_settings.get("timing_mode","D0") != "D0":
            raise ValueError("prototype timing conflicts with staged D1")
    if (
        planning_settings
        and planning_settings.get("timing_mode", "D0") == "D1"
        and any(policy not in RH_POLICIES for policy in policies)
    ):
        raise ValueError("mixed D0/non-RH and D1 comparisons are unsupported")
    number(run_wall_limit, "run_wall_limit")
    if suite_wall_limit is not None:
        number(suite_wall_limit, "suite_wall_limit")
    for seed in seeds:
        integer(seed, "seed")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "schema": "clover-headless-suite-v1",
        "status": "RUNNING",
        "runs": [],
        "seeds": seeds,
        "policies": policies,
        "source": source_manifest(),
        "scenario_hashes": {s.id: fingerprint(s.to_dict()) for s in scenarios},
        "planned_runs": len(scenarios) * len(seeds) * len(policies),
        "scope": "foundation correctness smoke; no algorithm superiority conclusion",
    }
    started = time.perf_counter()

    def checkpoint() -> None:
        temp = output / "manifest.pending.json"
        temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(output / "manifest.json")

    checkpoint()
    for scenario in scenarios:
        for seed in seeds:
            trace = make_trace(scenario, seed)
            initial = int(fingerprint(scenario.to_dict())[:8], 16)
            rng = np.random.default_rng(np.random.SeedSequence([seed, initial, 303]))
            for policy in rng.permutation(policies).tolist():
                case_id = f"{scenario.id}__{policy}__{seed}"
                if suite_wall_limit is not None and time.perf_counter() - started >= suite_wall_limit:
                    row = {
                        "case_id": case_id,
                        "scenario": scenario.id,
                        "policy": policy,
                        "seed": seed,
                        "status": "NOT_RUN_BUDGET",
                        "failure": "suite wall budget exhausted before start",
                    }
                else:
                    result = run_case(
                        scenario,
                        policy,
                        seed,
                        trace=trace,
                        output=output,
                        wall_limit=run_wall_limit,
                        planning_settings=planning_settings,
                        decision_settings=decision_settings,
                    )
                    row = {
                        key: result[key]
                        for key in (
                            "case_id",
                            "policy",
                            "seed",
                            "status",
                            "failure",
                            "scenario_hash",
                            "initial_hash",
                            "trace_hash",
                            "offered",
                            "completed_total",
                            "unfinished",
                            "rejected",
                            "cancelled",
                            "throughput_per_min",
                            "late_due_rate",
                            "mean_wip",
                            "faults",
                            "event_digest",
                            "max_energy_error",
                            "wall_seconds",
                        )
                    }
                    row.update(scenario=scenario.id, result=f"{case_id}.json", events=f"{case_id}.events.jsonl")
                    row["same_source"] = result["source"]["source_hash"] == manifest["source"]["source_hash"]
                manifest["runs"].append(row)
                checkpoint()
    valid = all(r["status"] == "SUCCESS" and r.get("same_source", True) for r in manifest["runs"])
    manifest["status"] = "SUCCESS" if valid else "PARTIAL"
    manifest["wall_seconds"] = time.perf_counter() - started
    checkpoint()
    fields = sorted({key for row in manifest["runs"] for key in row})
    with (output / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fields)
        writer.writeheader()
        writer.writerows(manifest["runs"])
    return manifest
