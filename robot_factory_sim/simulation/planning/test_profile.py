"""Failure profiling and same-domain construction optimizations."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from typing import Any

import pytest

from simulation.planning.backend import Backend
from simulation.planning.contracts import Settings
from simulation.planning.native import NativeModel
from simulation.planning.process import SolverProcess
from simulation.planning.profiling import PhaseTimer
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_cabin_flow import opposing_engine
from simulation.planning.test_inflight import inflight_engine
from simulation.planning.test_service_planning import joint
from simulation.planning.validation import validate


def config(**changes: Any) -> Settings:
    """A small bounded native model, with physical settings held fixed."""
    return replace(
        Settings(
            horizon=40, step=2, sigma=0, timeout=10, watchdog=12, formulation="native", native_step=2, native_actions=4
        ),
        **changes,
    )


def test_exclusive_timer_survives_interrupted_stage_without_fabricating_search() -> None:
    now = [0.0]
    timer = PhaseTimer(lambda: now[0])
    timer.start("import")
    now[0] = 2
    timer.start("model")
    now[0] = 7
    assert timer.snapshot() == {"import": 2, "model": 5}
    assert timer.snapshot() == {"import": 2, "model": 5}
    timer.stop()
    now[0] = 10
    assert timer.last == "model" and timer.snapshot() == {"import": 2, "model": 5}
    assert "search" not in timer.snapshot()


def test_timeout_during_builder_keeps_elapsed_stages_and_partial_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(self: NativeModel, robot: Any) -> None:
        self.integer("partial", 0, 1)
        raise TimeoutError("injected builder timeout")

    monkeypatch.setattr(NativeModel, "_robot", interrupted)
    engine = joint()
    data = prepare(capture(engine, config(), "build-timeout"))
    result = Backend("cpsat_native").solve(data)
    report = result.report
    assert report.status == "NO_INCUMBENT_TIMEOUT" and not result.actions and result.input == data
    assert report.timing_stage == "native_robot_model" and not report.timing_complete
    assert report.import_seconds > 0 and report.model_seconds > 0 and report.build_seconds > 0
    assert report.stage_seconds["native_robot_model"] > 0
    assert report.variables == 1 and report.work_counters["variables"] == 1
    assert "native_primary_search" not in report.stage_seconds
    assert decode(json.loads(json.dumps(encode(result)))) == result
    engine.close()


def test_import_failure_has_partial_timing_without_claiming_a_constructed_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(self: NativeModel, data: Any, deadline: float, timer: PhaseTimer) -> None:
        timer.start("native_import")
        raise ImportError("injected missing solver")

    monkeypatch.setattr(NativeModel, "__init__", unavailable)
    engine = joint()
    result = Backend("cpsat_native").solve(prepare(capture(engine, config(), "import-failure")))
    assert result.report.status == "UNAVAILABLE" and result.report.import_seconds > 0
    assert result.report.timing_stage == "native_import" and not result.report.timing_complete
    assert not result.report.work_counters and not result.actions
    engine.close()


def test_native_budget_limit_records_partial_model_without_execution() -> None:
    engine = joint()
    result = Backend("cpsat_native").solve(prepare(capture(engine, config(max_arcs=1), "limit")))
    assert result.report.status == "BUILD_LIMIT" and not result.actions
    assert result.report.timing_stage == "native_robot_model"
    assert result.report.stage_seconds["native_robot_model"] > 0 and not result.report.timing_complete
    assert "native_primary_search" not in result.report.stage_seconds
    engine.close()


def test_return_cache_keeps_exact_model_hash_and_both_objectives() -> None:
    engine = joint()
    snap = capture(engine, config(), "cache")
    results = [
        Backend("cpsat_native").solve(prepare(replace(snap, settings=replace(snap.settings, native_cache=flag))))
        for flag in (False, True)
    ]
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in results)
    assert len({r.report.model_hash for r in results}) == 1
    assert len({r.report.semantic_spec_hash for r in results}) == 1
    assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in results}) == 1
    counts = results[1].report.work_counters
    assert (
        counts["return_cache_hits"] > 0
        and counts["return_queries"] == counts["return_cache_hits"] + counts["return_cache_misses"]
    )
    assert results[1].report.timing_complete
    assert results[1].report.stage_seconds["native_solution_validate"] > 0
    engine.close()


@pytest.mark.parametrize("inflight", (False, True))
def test_cabin_pruning_reduces_links_without_changing_scope_or_optima(inflight: bool) -> None:
    engine = inflight_engine() if inflight else opposing_engine()
    snap = capture(engine, config(), "prune")
    results = [
        Backend("cpsat_native").solve(prepare(replace(snap, settings=replace(snap.settings, native_pruning=flag))))
        for flag in (False, True)
    ]
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in results), [r.report for r in results]
    assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in results}) == 1
    assert len({r.report.semantic_spec_hash for r in results}) == 1
    a, b = [r.report.work_counters for r in results]
    assert b["cabin_links_pruned"] > 0 and b["cabin_links_kept"] < a["cabin_links_kept"]
    assert a["cabin_pairs_considered"] == b["cabin_pairs_considered"]
    assert results[1].report.variables < results[0].report.variables
    engine.close()


def test_parent_kill_does_not_invent_worker_phase_measurements() -> None:
    engine = joint()
    worker = SolverProcess()
    try:
        result = worker.solve(capture(engine, config(memory_limit_mib=1), "rss"), time.monotonic(), "cpsat_native")
        assert result.report.status == "MEMORY_LIMIT" and worker.process is None
        assert result.report.timing_stage == "parent_memory_limit" and not result.report.timing_complete
        assert not result.report.stage_seconds
    finally:
        worker.close()
        engine.close()


def test_worker_route_preparation_failure_is_distinct_from_native_construction() -> None:
    engine = joint()
    worker = SolverProcess()
    try:
        result = worker.solve(capture(engine, config(max_routes=1), "route-limit"), time.monotonic(), "cpsat_native")
        assert result.report.status == "BUILD_LIMIT" and result.report.timing_stage == "route_prepare"
        assert result.report.stage_seconds["route_prepare"] > 0
        assert "native_robot_model" not in result.report.stage_seconds
    finally:
        worker.close()
        engine.close()
