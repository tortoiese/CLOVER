"""Prove small-instance parity and exercise delayed admission through the plant."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples, joint_batch
from simulation.evaluation.runner import replay, run_case, run_suite
from simulation.planning.backend import Backend
from simulation.planning.contracts import RH_BACKENDS, Settings
from simulation.planning.controller import RollingHorizonScheduler
from simulation.planning.replay import decode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_service_planning import joint
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.plant import RobotInitial
from simulation.scheduler import create_scheduler

pytest.importorskip("ortools")


@pytest.mark.parametrize("case", ("joint", "long", "charge", "narrow", "cabin", "empty"))
def test_three_compilers_prove_same_lexicographic_optimum(case: str) -> None:
    settings = Settings(horizon=20, step=2, sigma=0, timeout=10, watchdog=12)
    if case in {"joint", "long", "empty"}:
        engine = joint()
        if case == "long":
            engine.cancel_task("second")
            engine.stations["S1"].cfg.avg_process_time = 100
            settings = Settings(horizon=4, interval=2, step=2, sigma=0, timeout=10, watchdog=12)
        elif case == "empty":
            engine.cancel_task("first")
            engine.cancel_task("second")
    else:
        scenario = examples()[{"charge": 0, "narrow": 2, "cabin": 5}[case]]
        if case == "charge":
            scenario.factory.robots = scenario.factory.robots[:1]
            scenario.factory.robots[0].battery_capacity = 30
            scenario.factory.robots[0].charge_threshold = 0
            scenario.initial_robots = {"R0": RobotInitial(200, 200, soc_pct=10)}
        engine = SimulationEngine(
            scenario.factory,
            demand_source="disabled",
            initial_robots=scenario.initial_robots,
            initial_cabins=scenario.initial_cabins,
        )
        initialize(engine)
        engine.scheduler.dispatch = lambda plant: None
        if case == "charge":
            engine.add_task("inspection", "S0", 2)
        else:
            engine.robots["R0"].emergency_order = "a"
            engine.robots["R1"].emergency_order = "b"
            engine.add_task("inspection", "S1", 2, order_id="a")
            engine.add_task("inspection", "S0", 2, order_id="b")
            settings = Settings(horizon=30, step=2, sigma=0, timeout=10, watchdog=12)
    try:
        if case == "cabin":
            settings = replace(settings, joint_cabins=False)
        data = prepare(capture(engine, settings, "parity"))
        results = [Backend(kind).solve(data) for kind in RH_BACKENDS.values()]
        for result in results:
            assert result.report.status == "OPTIMAL", result.report
            assert not validate(result)
        assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in results}) == 1
        assert len({r.report.semantic_hash for r in results}) == 1
        assert results[0].report.model_hash == results[1].report.model_hash
        assert results[0].report.model_hash != results[2].report.model_hash
        if case == "charge":
            assert all(any(a.arc.kind == "charge" for a in r.actions) for r in results)
        if case == "cabin":
            assert all(sum(len(a.arc.cabins) for a in r.actions) <= 1 for r in results)
    finally:
        engine.close()


@pytest.mark.parametrize("policy", tuple(RH_BACKENDS))
@pytest.mark.parametrize("timing", ("D0", "D1"))
def test_real_workers_and_recorded_physical_replay(policy: str, timing: str, tmp_path: Path) -> None:
    scenario = joint_batch()
    result = run_case(
        scenario,
        policy,
        17,
        planning_settings=asdict(Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7, timing_mode=timing)),
        output=tmp_path,
    )
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["completed_total"] == 2 and result["planning"]["accepted"] > 0
    first = result["planning"]["decisions"][0]
    assert first["result"]["report"]["backend"] == RH_BACKENDS[policy]
    assert first["time"] - first["requested_at"] == pytest.approx(first["latency_seconds"])
    assert (first["latency_seconds"] > 0) == (timing == "D1")
    if timing == "D1":
        applied = decode(first["applied_result"])
        assert applied.report.phase1_bound is None and applied.report.phase1_cost is None
    assert replay(result)["matches"]


def delayed_controller(engine: SimulationEngine, monkeypatch: pytest.MonkeyPatch) -> RollingHorizonScheduler:
    """Inject a deterministic clock for ordering tests; no fake solver solution."""
    scheduler = RollingHorizonScheduler(Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=7, timing_mode="D1"))
    engine.scheduler = scheduler
    ticks = iter((100.0, 100.75, 101.0, 101.1))
    scheduler._clock = lambda: next(ticks)
    monkeypatch.setattr(
        scheduler.worker, "solve", lambda snapshot, started, backend: Backend(backend).solve(prepare(snapshot))
    )
    return scheduler


@pytest.mark.parametrize("change", ("none", "arrival", "cancel", "retire", "layout"))
def test_delayed_plan_applies_only_after_delay_and_rejects_changed_inputs(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = joint()
    scheduler = delayed_controller(engine, monkeypatch)
    scheduler.dispatch(engine)
    assert len(engine.pending_tasks) == 2 and not scheduler.records[0]["accepted"]
    engine.env.run(until=0.25)
    if change == "arrival":
        engine.add_task("inspection", "S0", 0, external_uid="new")
    elif change == "cancel":
        engine.cancel_task("first")
    elif change == "retire":
        engine.robots["R0"].alive = False
    elif change == "layout":
        engine.layout_version += 1
    engine.env.run(until=0.7)
    assert scheduler.records[0]["time"] is None and scheduler.pending_return
    engine.env.run(until=0.8)
    record = scheduler.records[0]
    assert record["time"] == 0.75 and not scheduler.pending_return
    assert record["accepted"] == (change == "none")
    if change != "none":
        assert record["reason"].startswith("STALE"), record
        assert scheduler.fallbacks[record["reason"]] == 1
    assert [j.uid for j in decode(record["result"]).input.snapshot.jobs] == ["first", "second"]
    assert_invariants(engine)
    engine.close()


def test_d1_request_still_pending_at_end_is_retained_and_replayed() -> None:
    scenario = joint_batch()
    scenario.duration = 0.51
    result = run_case(
        scenario,
        "rh_cpsat_linear",
        17,
        planning_settings=asdict(Settings(horizon=20, step=2, timeout=0.001, watchdog=0.05, timing_mode="D1")),
    )
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["planning"]["pending_at_end"] == 1
    assert result["planning"]["decisions"][0]["reason"] == "PENDING_AT_END"
    assert replay(result)["matches"]


def test_busy_robot_keeps_moving_while_other_robots_wait_for_delayed_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = joint()
    engine.assign(engine.pending_tasks[0], engine.robots["R1"])
    engine.env.run(until=0.25)
    before = engine.robots["R1"].position()
    scheduler = delayed_controller(engine, monkeypatch)
    scheduler.dispatch(engine)
    engine.env.run(until=0.9)
    assert engine.robots["R1"].position() != before
    assert scheduler.records[0]["time"] is None
    engine.env.run(until=1.01)
    assert scheduler.records[0]["time"] == 1.0
    assert_invariants(engine)
    engine.close()


@pytest.mark.parametrize("backend", ("cpsat_linear", "cpsat_interval"))
def test_cp_timeout_preserves_safe_incumbent_and_backend_identity(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = joint()
    solver = Backend(backend)
    monkeypatch.setattr(solver, "phase", lambda *args, **kwargs: (None, "NO_INCUMBENT_TIMEOUT", None))
    result = solver.solve(prepare(capture(engine, Settings(horizon=20, step=2, warm_start=False), "timeout")))
    assert result.report.backend == backend and result.report.status == "FEASIBLE"
    assert result.report.native_phase1_status == "NO_INCUMBENT_TIMEOUT"
    assert result.report.phase1_incumbent_source == "SAFE_HOLD_HINT" and not validate(result)
    engine.close()


def test_d1_mixed_mode_comparison_rejected_before_creating_outputs(tmp_path: Path) -> None:
    output = tmp_path / "bad"
    with pytest.raises(ValueError, match="mixed D0"):
        run_suite(
            (joint_batch(),),
            (1,),
            output,
            policies=("optimized", "rh_cpsat_linear"),
            planning_settings={"timing_mode": "D1"},
        )
    assert not output.exists()


def test_legacy_assignment_has_distinct_identity_and_runs_in_headless() -> None:
    pytest.importorskip("scipy")
    assert create_scheduler("assignment_milp_legacy").name == "assignment_milp_legacy"
    result = run_case(joint_batch(), "assignment_milp_legacy", 17)
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["completed_total"] == 2 and result["legacy_planning"]
    assert result["planning"] is None  # This is not the service-flow RH model.
    assert replay(result)["matches"]
