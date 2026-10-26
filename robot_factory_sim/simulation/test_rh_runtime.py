"""Failure isolation, stale results, replay, long jobs and experimental evidence."""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

from simulation.energy import measure
from simulation.engine import SimulationEngine
from simulation.experiments import (
    POLICIES,
    Demand,
    generate_trace,
    run_experiment,
    trace_hash,
)
from simulation.planning.backend import Backend
from simulation.planning.contracts import Settings
from simulation.planning.process import SolverProcess, solve_request
from simulation.planning.replay import export_result, read_snapshot
from simulation.planning.snapshot import capture
from simulation.planning.validation import validate
from simulation.test_rh_planning import factory, small_snapshot
from utils.comparison_models import ComparisonConditions, calculate_economics


def _sleeping_child(connection: Connection) -> None:
    time.sleep(30)
    connection.close()


def idle_engine(mode: str = "rh_cpsat") -> SimulationEngine:
    """Initialize without a job so a test controls the first planning boundary."""
    engine = SimulationEngine(factory(), mode, params={"task_rate": 0}, comparison_seed=3)
    engine.scheduler.settings = Settings(horizon=12, interval=2, scope="traffic")
    engine.robots["R"].x = engine.robots["R"].y = 0
    engine.env.run(until=.01)
    return engine


def test_real_subprocess_result_and_shutdown() -> None:
    engine = idle_engine()
    engine.add_task("P", "S", 1)
    request = capture(engine, engine.scheduler.settings, "process:1", (), set())
    process = SolverProcess("cpsat")
    try:
        result = process.solve(request)
        assert result.report.status in ("OPTIMAL", "FEASIBLE"), result.report
        assert not validate(result)
        assert process.process.is_alive()
    finally:
        process.close()
    assert process.process is None and process.pending is None


def test_hard_watchdog_kills_only_owned_process() -> None:
    engine = idle_engine()
    request = capture(engine, engine.scheduler.settings, "process:2", (), set())
    process = SolverProcess("milp")
    context = mp.get_context("spawn")
    process.connection, child = context.Pipe()
    process.process = context.Process(target=_sleeping_child, args=(child,))
    process.process.start()
    child.close()
    process.pending = request
    process.started = time.monotonic() - 11
    result = process.poll()
    assert result.report.status == "HARD_TIMEOUT"
    assert process.process is None


def test_cancel_subprocess_has_bounded_latency() -> None:
    engine = idle_engine()
    request = capture(engine, engine.scheduler.settings, "process:3", (), set())
    process = SolverProcess("cpsat")
    before = time.monotonic()
    with pytest.raises(InterruptedError):
        process.solve(request, lambda: True)
    assert time.monotonic() - before < 2
    assert process.process is None


def test_stale_job_cancellation_rejects_result() -> None:
    engine = idle_engine()
    task = engine.add_task("P", "S", 1)
    request = capture(engine, engine.scheduler.settings, "stale:1", (), set())
    result = solve_request(request, "cpsat")
    engine.pending_tasks.remove(task)
    engine.scheduler._accept(engine, request, result)
    assert engine.scheduler.stale == 1
    assert engine.robots["R"].current_task is None


def test_small_delay_is_revalidated_and_labeled() -> None:
    engine = idle_engine()
    engine.add_task("P", "S", 1)
    request = capture(engine, engine.scheduler.settings, "shift:1", (), set())
    result = solve_request(request, "cpsat")
    engine.scheduler._last_queue = tuple(t.task_id for t in engine.pending_tasks)
    engine.scheduler.next_solve = 100
    engine.env.run(until=.5)
    engine.scheduler._accept(engine, request, result)
    assert engine.scheduler.last_execution == "SHIFTED_FEASIBLE"
    assert engine.scheduler.accepted == 1


def test_long_job_uses_explicit_safe_prefix_fallback() -> None:
    engine = idle_engine()
    cfg = engine.factory
    cfg.stations[0].avg_process_time = 310
    engine.apply_config(cfg)
    engine.add_task("long", "S", 0)
    engine.env.run(until=320)
    assert engine.metrics.total_completed == 1
    assert engine.scheduler.fallbacks >= 1
    assert engine.robots["R"].battery > 0


def test_no_charger_holds_job_in_queue() -> None:
    cfg = factory()
    cfg.chargers = []
    engine = SimulationEngine(cfg, "rh_cpsat", params={"task_rate": 0}, comparison_seed=3)
    engine.scheduler.settings = Settings(horizon=12, interval=2)
    engine.add_task("P", "S", 0)
    engine.env.run(until=5)
    assert engine.metrics.total_completed == 0
    assert len(engine.pending_tasks) == 1
    assert any(a.get("reason") == "HOLD_NO_SAFE_ACTION" for a in engine.scheduler.audit)


def test_plan_export_replays_identical_model(tmp_path: Path) -> None:
    result = Backend("cpsat").solve(small_snapshot())
    path = tmp_path / "plan.json"
    export_result(result, path)
    replayed = Backend("milp").solve(read_snapshot(path))
    assert replayed.report.model_hash == result.report.model_hash
    assert replayed.report.phase1_cost == result.report.phase1_cost
    assert replayed.report.phase2_cost == result.report.phase2_cost


def test_energy_ledger_balances_during_partial_work_and_charge() -> None:
    engine = idle_engine()
    engine.add_task("P", "S", 1)
    for target in (.5, 2.5, 4.5, 8, 20):
        engine.env.run(until=target)
        assert abs(measure(engine)["energy_balance_error"]) < 1e-8


def test_missing_financial_inputs_differ_from_known_zero() -> None:
    assert calculate_economics(1, 1, 1, ComparisonConditions(), False)["total_cost"] is None
    known_zero = ComparisonConditions(revenue_per_task=0, wh_per_battery_unit=0, electricity_per_kwh=0)
    result = calculate_economics(1, 1, 1, known_zero, False)
    assert result["total_cost"] == result["revenue"] == result["net_profit"] == 0
    assert result["roi"] is None


def test_external_trace_identity_and_failure_retention() -> None:
    cfg = factory()
    assert trace_hash(generate_trace(cfg, 30, .1, 4)) == trace_hash(generate_trace(cfg, 30, .1, 4))
    conditions = ComparisonConditions(duration=30, repeats=2)
    trace = (Demand(0, 0, "S", ("S",), "P", 1, 0),)
    result = run_experiment(cfg, conditions, ("baseline_fifo", "weighted_heuristic"),
                            traces=(trace, trace), wall_limit=-1)
    assert all(s["failed_runs"] == 2 for s in result["summaries"].values())
    assert all(v is None for v in result["paired_intervals"]["weighted_heuristic"].values())


def test_all_five_policies_share_trace_and_include_unfinished() -> None:
    trace = (Demand(0, 0, "S", ("S",), "P", 1, 0), Demand(1, 29, "S", ("S",), "late", 1, 0))
    result = run_experiment(factory(), ComparisonConditions(duration=30, repeats=2), POLICIES,
                            Settings(horizon=12, interval=2, scope="traffic"), traces=(trace, trace), wall_limit=30)
    for policy, rows in result["samples"].items():
        assert len(rows) == 2 and all(row["status"] == "SUCCESS" for row in rows), (policy, rows)
        assert all(row["trace_hash"] == trace_hash(trace) for row in rows)
        assert all(row["unfinished"] >= 1 for row in rows)
    json.dumps(result, allow_nan=False)


def test_phase_two_failure_retains_first_incumbent(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Backend._phase
    calls = []

    def phase(self: Backend, *args: object, **kwargs: object) -> object:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("injected phase two failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Backend, "_phase", phase)
    result = Backend("cpsat", use_hint=False).solve(small_snapshot())
    assert result.report.status == "FEASIBLE"
    assert result.report.phase2_status == "ERROR"
    assert not validate(result)


def test_duplicate_prefix_cannot_assign_same_job_twice() -> None:
    from simulation.planning.execution import commit, select_prefix
    engine = idle_engine()
    engine.add_task("P", "S", 1)
    request = capture(engine, engine.scheduler.settings, "atomic:1", (), set())
    result = solve_request(request, "cpsat")
    prefix = select_prefix(result)
    assert commit(engine, prefix)
    count = len(engine.scheduler.reservations)
    assert not commit(engine, prefix)
    assert len(engine.scheduler.reservations) == count


def test_resource_identity_survives_database_id_renumber() -> None:
    cfg = factory()
    engine = SimulationEngine(cfg, params={"task_rate": 0}, comparison_seed=3)
    resource = engine.corridors.edge_resource(1)
    request = resource.request(900)
    renamed = replace(cfg, nodes=[replace(n, id=n.id + 10) for n in cfg.nodes],
                      edges=[replace(e, id=e.id + 99, from_node=e.from_node + 10, to_node=e.to_node + 10)
                             for e in cfg.edges])
    engine.apply_config(renamed)
    assert engine.corridors.edge_resource(100) is resource
    assert request in resource.users


def test_live_long_wait_is_not_counted_as_deadlock() -> None:
    engine = idle_engine()
    robot = engine.robots["R"]
    resource = engine.corridors.edge_resource(1)
    blocker = resource.request(900)
    engine.env.process(robot._cross_edge(1, 10, 0))
    engine.env.run(until=126)
    assert engine.kpis()["long_wait_count"] == 1
    assert engine.kpis()["deadlock_count"] == 0
    resource.release(blocker)
    engine.env.run(until=130)
    assert engine.metrics.corridor_waits[-1] >= 120


def test_elevator_surrogate_cost_is_not_battery_distance() -> None:
    import config
    from database.seed_data import build_default_config
    from simulation.path_graph import PathGraph
    graph = PathGraph(build_default_config())
    route = graph.route_to_facility(100, 150, "WS-05", 1)
    metres = graph.physical_distance_to_facility(100, 150, "WS-05", 1)
    assert route is not None
    assert metres < route.length * config.METERS_PER_PIXEL
