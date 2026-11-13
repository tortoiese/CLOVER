"""Checked useful incumbents, exact lower bounds and failures in phase two."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from simulation.audit import assert_invariants
from simulation.planning.backend import Backend, check_incumbent, cost
from simulation.planning.contracts import RH_BACKENDS, PreparedInput, Settings
from simulation.planning.execution import commit
from simulation.planning.incumbent import work_hint
from simulation.planning.model import build_model
from simulation.planning.process import SolverProcess
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_service_planning import joint
from simulation.planning.validation import validate


def observed(warm: bool = True) -> PreparedInput:
    """Detach one known asymmetric assignment problem."""
    engine = joint()
    data = prepare(
        capture(
            engine,
            Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7, warm_start=warm, warm_start_seconds=1),
            "incumbent",
        )
    )
    engine.close()
    return data


def test_hint_serves_both_jobs_with_correct_return_energy_and_unchanged_math() -> None:
    data = observed()
    model = build_model(data)
    hint = work_hint(model, time.perf_counter() + 5)
    assert hint.source == "WORK_WARM_START" and 1 <= hint.trials <= 8
    check_incumbent(model, hint.values)
    assert cost(hint.values, model.objective1) == 0
    chosen = {(a.robot, a.job) for v, a in enumerate(model.arcs) if hint.values[v] and a.job}
    assert chosen == {("R0", "second"), ("R1", "first")}
    disabled = replace(
        data, snapshot=replace(data.snapshot, settings=replace(data.snapshot.settings, warm_start=False))
    )
    old = build_model(disabled)
    assert (model.variables, model.rows, model.objective1, model.objective2, model.fingerprint) == (
        old.variables,
        old.rows,
        old.objective1,
        old.objective2,
        old.fingerprint,
    )
    assert work_hint(old, time.perf_counter() + 5).source == "SAFE_HOLD_HINT"


@pytest.mark.parametrize("backend", tuple(RH_BACKENDS.values()))
def test_native_timeout_keeps_valid_work_instead_of_silently_claiming_solver_output(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    solver = Backend(backend)
    monkeypatch.setattr(solver, "phase", lambda *args, **kwargs: (None, "NO_INCUMBENT_TIMEOUT", None))
    result = solver.solve(observed())
    assert result.report.status == "FEASIBLE"
    assert result.report.native_phase1_status == "SKIPPED_ZERO_LOWER_BOUND"
    assert result.report.phase1_proof == "NONNEGATIVE_ZERO_INCUMBENT" and result.report.phase1_bound == 0
    assert result.report.native_phase2_status == "NO_INCUMBENT_TIMEOUT"
    assert result.report.solution_source == "WORK_WARM_START"
    assert result.report.phase1_cost == 0 and not result.deferred and not validate(result)


def test_corrupt_phase_two_values_do_not_escape_after_validation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    solver = Backend()

    def corrupt(model: object, *args: object, **kwargs: object) -> tuple[tuple[int, ...], str, float]:
        return (2,) * len(model.variables), "FEASIBLE", 0.0

    monkeypatch.setattr(solver, "phase", corrupt)
    result = solver.solve(observed())
    assert result.report.phase2_status == "ERROR" and result.report.phase2_bound is None
    assert result.report.solution_source == "WORK_WARM_START"
    assert result.report.phase1_cost == 0 and not validate(result)


def test_missing_native_dependency_is_explicit_even_if_a_work_hint_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    solver = Backend()

    def unavailable(*args: object, **kwargs: object) -> None:
        raise ImportError("forced missing backend")

    monkeypatch.setattr(solver, "phase", unavailable)
    result = solver.solve(observed())
    assert result.report.status == "UNAVAILABLE" and not result.actions


def test_exhausted_hint_budget_keeps_checked_hold() -> None:
    model = build_model(observed())
    hint = work_hint(model, time.perf_counter() - 1)
    assert hint.expired and hint.source == "SAFE_HOLD_HINT"
    check_incumbent(model, hint.values)


def test_battery_and_resource_constraints_are_not_relaxed_by_hint() -> None:
    data = observed()
    actors = tuple(replace(r, battery=1) for r in data.snapshot.robots)
    model = build_model(replace(data, snapshot=replace(data.snapshot, robots=actors)))
    hint = work_hint(model, time.perf_counter() + 5)
    assert hint.source == "SAFE_HOLD_HINT"
    check_incumbent(model, hint.values)
    blocked = tuple(replace(f, occupied=f.capacity) if f.kind == "work" else f for f in data.snapshot.facilities)
    model = build_model(replace(data, snapshot=replace(data.snapshot, facilities=blocked)))
    hint = work_hint(model, time.perf_counter() + 5)
    assert hint.source == "SAFE_HOLD_HINT"
    check_incumbent(model, hint.values)


def test_warm_incumbent_executes_through_real_plant_when_native_returns_no_solution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = joint()
    data = prepare(capture(engine, Settings(horizon=20, step=2, sigma=0, warm_start_seconds=1), "physical"))
    solver = Backend()
    monkeypatch.setattr(solver, "phase", lambda *args, **kwargs: (None, "NO_INCUMBENT_TIMEOUT", None))
    result = solver.solve(data)
    assert commit(engine, result) == (True, "COMMITTED")
    engine.env.run(until=25)
    assert engine.metrics.total_completed == 2
    assert_invariants(engine)
    engine.close()


def test_real_worker_profile_separates_preparation_from_model_and_reuses_process() -> None:
    engine = joint()
    config = Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7)
    worker = SolverProcess()
    try:
        reports = []
        for i in range(2):
            result = worker.solve(capture(engine, config, str(i)), time.perf_counter(), "cpsat_linear")
            assert result.report.status in {"OPTIMAL", "FEASIBLE"}, result.report
            assert not validate(result)
            reports.append(result.report)
        assert not reports[0].worker_reused and reports[1].worker_reused
        for report in reports:
            assert report.worker_peak_rss_mib > 0 and report.worker_cpu_seconds > 0
            assert report.prepare_seconds > 0 and report.model_seconds > 0 and report.compile_seconds > 0
            assert report.parent_elapsed_seconds >= report.total_seconds
            assert (
                report.total_seconds
                >= report.dispatch_to_worker_seconds + report.prepare_seconds + report.model_seconds
            )
    finally:
        worker.close()
        engine.close()
