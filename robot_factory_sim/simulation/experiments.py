"""Paired policy evaluation using an external, policy-independent demand trace."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from simulation.comparison import paired_interval
from simulation.energy import measure
from simulation.engine import SimulationEngine
from simulation.planning.contracts import PLAN_SCHEMA, Settings
from simulation.planning.process import SolverProcess
from simulation.wait_metrics import wait_distributions
from utils.comparison_models import ComparisonConditions, calculate_economics
from utils.config_models import FactoryConfig

POLICIES = ("baseline_fifo", "weighted_heuristic", "assignment_milp_legacy", "rh_milp", "rh_cpsat")
SOURCE_BASE = "841af64c2ef4684053d0aadeed0b5fee8df36ec7"


@dataclass(frozen=True)
class Demand:
    """A pre-generated exogenous job; noise is exposed only on actual execution."""

    id: int
    at: float
    station: str
    allowed: tuple[str, ...]
    product: str
    priority: int
    noise: float
    deadline: float | None = None
    order: str | None = None


@dataclass(frozen=True)
class Intervention:
    """An exogenous event revealed only when its simulation timestamp occurs."""

    at: float
    kind: str
    target: str
    value: float | bool = True


def _interventions(engine: SimulationEngine, events: tuple[Intervention, ...]) -> Any:
    for event in sorted(events, key=lambda e: e.at):
        yield engine.env.timeout(max(0, event.at - engine.now))
        applied = True
        if event.kind == "cancel_task":
            applied = engine.cancel_external_task(event.target)
        else:
            factory = copy.deepcopy(engine.factory)
            collection = (factory.stations if event.kind == "station_active" else factory.chargers
                          if event.kind in ("charger_active", "charger_capacity") else factory.robots
                          if event.kind == "robot_active" else None)
            if collection is None:
                raise ValueError(f"unsupported intervention: {event.kind}")
            target = next((item for item in collection if item.id == event.target), None)
            if target is None:
                raise ValueError(f"unknown intervention target: {event.target}")
            if event.kind == "charger_capacity":
                if int(event.value) != event.value or event.value < 1:
                    raise ValueError("charger capacity intervention must be a positive integer")
                target.capacity = int(event.value)
            else:
                target.is_active = bool(event.value)
            engine.apply_config(factory)
        engine.planning_event("external_intervention", **asdict(event), applied=applied)


def environment_manifest() -> dict[str, Any]:
    """Record runtime versions without credentials, user names or absolute paths."""
    dependencies = {}
    for package in ("simpy", "numpy", "scipy", "ortools", "PySide6", "pyqtgraph", "psutil", "chromadb"):
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = None
    return {"python": platform.python_version(), "os": platform.system(), "os_release": platform.release(),
            "architecture": platform.machine(), "logical_cpus": os.cpu_count(), "dependencies": dependencies}


def trace_hash(trace: tuple[Demand, ...]) -> str:
    """Return the exact workload identity shared across all evaluated policies."""
    return hashlib.sha256(json.dumps([asdict(d) for d in trace], sort_keys=True).encode()).hexdigest()


def generate_trace(factory: FactoryConfig, duration: float, rate: float, seed: int) -> tuple[Demand, ...]:
    """Draw arrivals and per-job noise before any policy is executed."""
    if not math.isfinite(rate) or rate <= 0 or duration <= 0:
        raise ValueError("duration and demand rate must be positive")
    stations = tuple(sorted(s.id for s in factory.stations if s.is_active))
    if not stations:
        return ()
    rng = np.random.default_rng(seed)
    when = float(rng.exponential(1 / rate))
    trace = []
    while when < duration:
        index = len(trace)
        if index >= 100_000:
            raise ValueError("external trace exceeds job limit")
        noise = float(np.random.default_rng(np.random.SeedSequence([seed, index, 719])).normal())
        trace.append(Demand(index, when, str(rng.choice(stations)), stations, "external", 3, noise))
        when += float(rng.exponential(1 / rate))
    return tuple(trace)


def _inject(engine: SimulationEngine, trace: tuple[Demand, ...]) -> Any:
    for demand in trace:
        yield engine.env.timeout(max(0, demand.at - engine.now))
        task = engine.add_task(demand.product, demand.station, demand.priority, demand.allowed,
                               demand.order, demand.deadline, external_uid=f"trace:{demand.id}")
        task.process_noise = demand.noise


def validate_external_inputs(factory: FactoryConfig, duration: float,
                             traces: tuple[tuple[Demand, ...], ...],
                             interventions: tuple[Intervention, ...]) -> None:
    """Reject malformed replay data before starting engines or native processes."""
    stations = {s.id for s in factory.stations}
    for trace in traces:
        if len(trace) > 100_000:
            raise ValueError("external trace exceeds job limit")
        identifiers = set()
        previous = -1.0
        for demand in trace:
            if (isinstance(demand.id, bool) or not isinstance(demand.id, int) or demand.id < 0 or
                    demand.id in identifiers or not math.isfinite(demand.at) or
                    not previous <= demand.at < duration or demand.at < 0 or
                    not math.isfinite(demand.noise) or
                    (demand.deadline is not None and not math.isfinite(demand.deadline)) or
                    not demand.allowed or demand.station not in demand.allowed or
                    any(s not in stations for s in demand.allowed) or
                    isinstance(demand.priority, bool) or not isinstance(demand.priority, int) or
                    not 0 <= demand.priority <= 4):
                raise ValueError("invalid external demand trace")
            identifiers.add(demand.id)
            previous = demand.at
    if len(interventions) > 10_000:
        raise ValueError("too many external interventions")
    targets = {"station_active": stations, "charger_active": {c.id for c in factory.chargers},
               "charger_capacity": {c.id for c in factory.chargers},
               "robot_active": {r.id for r in factory.robots}}
    for event in interventions:
        if not math.isfinite(event.at) or not 0 <= event.at < duration:
            raise ValueError("invalid intervention timestamp")
        if event.kind == "cancel_task":
            if not isinstance(event.target, str) or not event.target.startswith("trace:"):
                raise ValueError("invalid cancelled task identity")
        elif event.kind not in targets or event.target not in targets[event.kind]:
            raise ValueError("unknown intervention target or kind")
        elif event.kind == "charger_capacity":
            if (isinstance(event.value, bool) or not isinstance(event.value, (int, float)) or
                    not math.isfinite(event.value) or int(event.value) != event.value or not 1 <= event.value <= 1000):
                raise ValueError("invalid charger capacity intervention")
        elif not isinstance(event.value, bool):
            raise ValueError("active intervention requires a boolean")


def source_manifest() -> dict[str, str]:
    """Hash source contents so a locally modified benchmark remains identifiable."""
    root = Path(__file__).resolve().parents[1]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py")) if "__pycache__" not in path.parts}


def run_experiment(factory: FactoryConfig, conditions: ComparisonConditions,
                   policies: tuple[str, ...] = POLICIES, settings: Settings | None = None,
                   rate: float = .15, traces: tuple[tuple[Demand, ...], ...] | None = None,
                   cancelled: Callable[[], bool] | None = None, wall_limit: float = 120,
                   interventions: tuple[Intervention, ...] = (),
                   ) -> dict[str, Any]:
    """Keep every policy/seed, including failures, unfinished jobs and raw samples."""
    conditions.validate()
    if not policies or len(set(policies)) != len(policies) or any(p not in POLICIES for p in policies):
        raise ValueError("invalid policy set")
    settings = settings or Settings(horizon=math.ceil(conditions.horizon), interval=math.ceil(conditions.replan),
                                    scope="traffic")
    settings.validate()
    if traces is None:
        traces = tuple(generate_trace(factory, conditions.duration, rate, int(conditions.seed) + i)
                       for i in range(int(conditions.repeats)))
    if len(traces) != conditions.repeats:
        raise ValueError("trace count must equal repeats")
    validate_external_inputs(factory, conditions.duration, traces, interventions)
    samples = {p: [] for p in policies}
    started = time.monotonic()
    for repeat, trace in enumerate(traces):
        for policy in policies:
            seed = int(conditions.seed) + repeat
            sample = {"seed": seed, "trace_hash": trace_hash(trace), "status": "SUCCESS", "failure": None}
            engine = None
            try:
                if time.monotonic() - started > wall_limit:
                    raise TimeoutError("experiment wall budget exhausted")
                engine = SimulationEngine(copy.deepcopy(factory), policy, seed, datetime(2026, 1, 1),
                                          params={"task_rate": 0}, comparison_seed=seed)
                if hasattr(engine.scheduler, "settings"):
                    engine.scheduler.settings = settings
                    engine.scheduler.blocking_process = SolverProcess(engine.scheduler.backend)
                    engine.scheduler.cancelled = cancelled
                engine.env.process(_inject(engine, trace))
                engine.env.process(_interventions(engine, interventions))
                while engine.now < conditions.duration:
                    if cancelled and cancelled():
                        raise InterruptedError("experiment cancelled")
                    if time.monotonic() - started > wall_limit:
                        raise TimeoutError("experiment wall budget exhausted")
                    engine.env.run(until=min(conditions.duration, engine.now + 1))
            except Exception as exc:  # noqa: BLE001 — retain failed seeds in the result denominator
                sample["status"] = "CANCELLED" if isinstance(exc, InterruptedError) else "FAILED"
                sample["failure"] = f"{type(exc).__name__}: {exc}"
            finally:
                if engine is not None:
                    ledger = measure(engine)
                    metrics = {k: float(v) if math.isfinite(v) else None for k, v in engine.kpis().items()}
                    completed = engine.metrics.total_completed
                    arrived = sum(d.at < engine.now for d in trace)
                    deadlines = [e for e in engine.task_events if e["event"] == "completed" and e["deadline"] is not None]
                    waits = list(engine.metrics.wait_times)
                    active = sum(r.current_task is not None and not r.current_task.cancelled for r in engine.robot_list())
                    sample.update(metrics, completed=completed, arrived=arrived,
                                  cancelled_tasks=engine.cancelled_tasks,
                                  unfinished=max(0, arrived - completed - engine.cancelled_tasks),
                                  pending=len(engine.pending_tasks), observed_seconds=engine.now,
                                  active_tasks=active,
                                  conservation_error=arrived - completed - engine.cancelled_tasks
                                  - len(engine.pending_tasks) - active,
                                  wait_p95=float(np.quantile(waits, .95)) if waits else None,
                                  wait_sample_count=len(waits),
                                  deadline_completed=len(deadlines),
                                  late_completed=sum(e["time"] > e["deadline"] for e in deadlines),
                                  on_time_completed=sum(e["time"] <= e["deadline"] for e in deadlines),
                                  pending_age_sum=sum(engine.now - t.created_at for t in engine.pending_tasks),
                                  **calculate_economics(completed, ledger["battery_units"], ledger["distance_m"],
                                                        conditions, policy != "baseline_fifo",
                                                        {k.removeprefix("robot_energy:"): v for k, v in ledger.items()
                                                         if k.startswith("robot_energy:")}))
                    if hasattr(engine.scheduler, "planning_state"):
                        sample["planning"] = engine.scheduler.planning_state()
                        sample["planning_reports"] = [asdict(r) for r in engine.scheduler.reports]
                        sample["acceptance_reports"] = list(engine.scheduler.acceptance_events)
                        sample["fallback_reports"] = list(engine.scheduler.audit)
                        if engine.scheduler.last_result is not None:
                            sample["last_plan"] = {"schema": PLAN_SCHEMA, **asdict(engine.scheduler.last_result)}
                    sample["task_events"] = list(engine.task_events)
                    sample["wait_distributions"] = wait_distributions(engine)
                    sample["planning_events"] = list(engine.planning_events)
                    sample["deadlock_events"] = list(engine.deadlock_events)
                    sample["log_truncated"] = engine.log_truncated
                    if hasattr(engine.scheduler, "close"):
                        engine.scheduler.close()
                samples[policy].append(sample)
    summaries = {}
    for policy, rows in samples.items():
        summary = {"runs": len(rows), "failed_runs": sum(r["status"] != "SUCCESS" for r in rows)}
        for key in ("completed", "unfinished", "pending", "net_profit", "total_cost", "battery_units",
                    "distance_m", "energy_wh", "avg_wait", "pending_age_sum", "throughput", "charge_wait",
                    "profit_per_task", "profit_per_wh", "wait_p95", "wait_sample_count", "on_time_completed",
                    "late_completed", "conservation_error"):
            values = [row.get(key) for row in rows]
            summary[key] = sum(values) / len(rows) if all(v is not None for v in values) else None
        # Pooled ROI uses summed profit/cost; seed ROI values are retained above.
        profit, cost = summary["net_profit"], summary["total_cost"]
        summary["roi"] = profit / cost * 100 if profit is not None and cost is not None and cost > 0 else None
        summaries[policy] = summary
    intervals = {}
    reference = "baseline_fifo" if "baseline_fifo" in samples else "weighted_heuristic" if "weighted_heuristic" in samples else policies[0]
    if reference in samples:
        for policy in policies:
            if policy == reference:
                continue
            intervals[policy] = {}
            for key in ("completed", "net_profit", "total_cost", "wait_p95", "on_time_completed", "battery_units"):
                differences = [c.get(key) - b.get(key) for b, c in
                               zip(samples[reference], samples[policy], strict=True)
                               if b["status"] == c["status"] == "SUCCESS" and
                               b.get(key) is not None and c.get(key) is not None]
                intervals[policy][key] = paired_interval(differences) if len(differences) == len(traces) else None
    from simulation.planning.contracts import (
        MODEL_VERSION,
        OBJECTIVE_PROFILE,
        UNIT_PROFILE,
    )
    config_hash = hashlib.sha256(json.dumps(factory.to_dict(), sort_keys=True).encode()).hexdigest()
    return {"schema": "clover-experiment-v2", "base_commit": SOURCE_BASE, "source_commit": SOURCE_BASE,
            "source_dirty": True, "source_hashes": source_manifest(), "config_hash": config_hash,
            "experiment_id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat(),
            "environment": environment_manifest(), "model_version": MODEL_VERSION,
            "objective_profile": OBJECTIVE_PROFILE, "unit_profile": UNIT_PROFILE,
            "solver_seed": 42, "worker_count": 1, "seed_list": [int(conditions.seed) + i for i in range(len(traces))],
            "interventions": [asdict(e) for e in interventions],
            "intervention_hash": hashlib.sha256(json.dumps([asdict(e) for e in interventions], sort_keys=True).encode()).hexdigest(),
            "run_status": "complete" if all(r["status"] == "SUCCESS" for rows in samples.values() for r in rows) else "partial",
            "aggregation": {"roi": "pooled profit / pooled cost", "metrics": "per-seed means; failed runs retained",
                            "wait_quantile": "observed starts; bounded collector; censored pending ages also exported",
                            "log_limit": 50_000, "truncation": "oldest 1000 records; count exported"},
            "conditions": asdict(conditions), "settings": asdict(settings), "scope": settings.scope,
            "factory": factory.to_dict(), "policies": policies, "samples": samples, "summaries": summaries,
            "paired_intervals": intervals, "reference_policy": reference,
            "traces": [[asdict(d) for d in trace] for trace in traces],
            "trace_hashes": [trace_hash(trace) for trace in traces], "seconds": time.monotonic() - started,
            "notes": ["고정 경로 후보와 이산화된 계획 구간에 대한 최적화입니다.",
                      "실패/미완료 작업을 포함합니다. 실패가 있으면 해당 신뢰구간을 산출하지 않습니다.",
                      "완료 작업 대기시간과 미완료 작업의 누적 대기 나이를 함께 확인하세요."]}


def order_traces(factory: FactoryConfig, conditions: ComparisonConditions,
                 context: dict[str, Any]) -> tuple[tuple[Demand, ...], ...] | None:
    """Expose remaining order units at time zero, identically to every policy."""
    import config
    from utils.event_types import OrderStatus
    raw = context.get("comparison_orders", [])
    if len(raw) > 100:
        raise ValueError("at most 100 orders may be evaluated")
    active = [o for o in raw if o.get("status") in OrderStatus.ACTIVE and
              int(o["quantity"]) > int(o.get("completed", 0))]
    if not active:
        return None
    epoch = datetime.fromisoformat(context["sim_datetime"])
    traces = []
    for repeat in range(int(conditions.repeats)):
        jobs = []
        for order in active:
            allowed = tuple(order.get("required_stations") or sorted(s.id for s in factory.stations if s.is_active))
            if not allowed or any(factory.station(s) is None for s in allowed):
                raise ValueError("order references missing stations")
            quantity = int(order["quantity"]) - int(order.get("completed", 0))
            if quantity + len(jobs) > 100_000:
                raise ValueError("order quantity exceeds trace limit")
            deadline = ((datetime.fromisoformat(order["deadline"]) - epoch).total_seconds()
                        if order.get("deadline") else None)
            for _ in range(quantity):
                index = len(jobs)
                noise = float(np.random.default_rng(np.random.SeedSequence(
                    [int(conditions.seed) + repeat, index, 719])).normal())
                jobs.append(Demand(index, 0, allowed[0], allowed, order["product_type"],
                                   config.PRIORITY_LEVELS[order["priority"]], noise, deadline, order["order_id"]))
        traces.append(tuple(jobs))
    return tuple(traces)


def run_policy_comparison(conditions: ComparisonConditions, context: dict[str, Any],
                          cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Adapt paired policy evidence to the existing GUI report contract."""
    from simulation.comparison import snapshot_fingerprint, validate_factory
    scenario = None
    if context.get("comparison_scenario", "current") != "current":
        from simulation.scenario_suite import scenarios
        scenario = next((s for s in scenarios() if s.id == context["comparison_scenario"]), None)
        if scenario is None:
            raise ValueError("unknown comparison scenario")
        scenario = replace(scenario, duration=conditions.duration)
    factory = scenario.factory if scenario else FactoryConfig.from_dict(context["factory_config"])
    validate_factory(factory)
    policies = tuple(context["comparison_policies"])
    candidate = context.get("comparison_candidate", policies[-1])
    if "baseline_fifo" not in policies or len(policies) < 2 or candidate == "baseline_fifo" or candidate not in policies:
        raise ValueError("comparison requires FIFO and an evaluated candidate")
    traces = (tuple(scenario.trace(int(conditions.seed) + i) for i in range(int(conditions.repeats))) if scenario
              else order_traces(factory, conditions, context))
    settings = replace(scenario.settings, horizon=conditions.horizon, interval=conditions.replan) if scenario else None
    evidence = run_experiment(factory, conditions, policies, rate=float(context.get("params", {}).get("task_rate", .15)),
                              traces=traces, settings=settings, cancelled=cancelled, wall_limit=120)
    baseline, proposed = evidence["summaries"]["baseline_fifo"], evidence["summaries"][candidate]
    intervals = evidence["paired_intervals"][candidate]
    profit = intervals.get("net_profit")
    failed = any(row["failed_runs"] for row in evidence["summaries"].values())
    recommendation = "생산량·미완료 작업·비용을 함께 확인하세요. 개선을 단정할 근거가 부족합니다."
    if not failed and profit is not None and profit["low"] > 0 and proposed["completed"] >= baseline["completed"]:
        recommendation = "평가한 모델과 수요에서 순이익 증가가 관측됐습니다. 실제 공장에서는 실측 검증이 필요합니다."
    if failed:
        recommendation = "실패한 실험이 있어 추천을 보류합니다. 원시 결과에서 실패 원인을 확인하세요."
    reports = [r for row in evidence["samples"][candidate] for r in row.get("planning_reports", [])]
    return {"experiment_id": str(uuid.uuid4()), "fingerprint": snapshot_fingerprint(context),
            "conditions": asdict(conditions), "baseline": baseline, "candidate": proposed,
            "candidate_policy": candidate, "samples": {"baseline": evidence["samples"]["baseline_fifo"],
                                                        "candidate": evidence["samples"][candidate]},
            "intervals": intervals, "recommendation": recommendation,
            "solver": {"calls": len(reports), "optimal_calls": sum(r["status"] == "OPTIMAL" for r in reports),
                       "max_dispatch_gap": None,
                       "scope": "RH는 작업장·충전기·통로·교차점·단일 캐빈 계획; 전체 운영기간 최적성 미보장"},
            "workload": scenario.id if scenario else "남은 주문 전량을 0초에 공개한 동일 수요" if traces else "동일 외부 포아송 수요",
            "notes": evidence["notes"] + [f"미완료 작업: FIFO {baseline['unfinished']}, 선택 정책 {proposed['unfinished']}"],
            "experiment": evidence}
