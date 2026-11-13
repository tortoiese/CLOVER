"""Known optima, physical prefixes, stale/atomic control and native containment."""

from __future__ import annotations

import copy
import json
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from simulation.audit import assert_invariants
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.evaluation.runner import replay, run_case
from simulation.planning.backend import Backend
from simulation.planning.contracts import Result, Settings
from simulation.planning.execution import commit
from simulation.planning.process import SolverProcess
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture, prepare
from simulation.planning.test_snapshot import initialize
from simulation.planning.validation import validate
from simulation.plant import PlantSettings, RobotInitial
from simulation.task_generator import TaskState

pytest.importorskip("ortools")


def joint() -> SimulationEngine:
    """Two asymmetric positions with a strict better joint assignment."""
    scenario = examples()[1]
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S1", 2, external_uid="first")
    engine.add_task("inspection", "S3", 2, external_uid="second")
    return engine


def solve(engine: SimulationEngine, **settings: float) -> Result:
    """Use a generous correctness-test budget, not a performance claim."""
    config = Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=6, **settings)
    result = Backend().solve(prepare(capture(engine, config, "test")))
    assert result.report.status in {"OPTIMAL", "FEASIBLE"}, result.report
    assert not validate(result)
    return result


def test_known_joint_assignment_and_full_physical_prefix() -> None:
    engine = joint()
    result = solve(engine)
    assert result.report.phase1_cost == 0 and result.report.phase2_status == "OPTIMAL"
    chosen = {(a.arc.robot, a.arc.job) for a in result.actions if a.arc.kind == "service"}
    assert chosen == {("R0", "second"), ("R1", "first")}
    assert commit(engine, result) == (True, "COMMITTED")
    for at in range(1, 21):
        engine.env.run(until=at)
        assert_invariants(engine)
    assert engine.metrics.total_completed == 2
    assert all(r.planned_action is None for r in engine.robots.values())
    engine.close()


def test_service_longer_than_horizon_is_linked_and_kept_in_tail() -> None:
    engine = joint()
    engine.cancel_task("second")
    engine.stations["S1"].cfg.avg_process_time = 100
    settings = Settings(horizon=4, interval=2, step=2, sigma=0, timeout=5, watchdog=6)
    result = Backend().solve(prepare(capture(engine, settings, "long")))
    assert result.report.phase1_cost == 0 and not validate(result)
    service = next(a for a in result.actions if a.arc.kind == "service")
    assert service.arc.start < 2 and service.arc.end > 2
    assert result.input.snapshot.settings.horizon == 4
    assert commit(engine, result)[0]
    engine.env.run(until=120)
    assert engine.metrics.total_completed == 1
    assert_invariants(engine)
    engine.close()


def test_independent_validator_rejects_wrong_energy_and_unit() -> None:
    engine = joint()
    result = solve(engine)
    index = next(i for i, a in enumerate(result.actions) if a.arc.kind == "service")
    actions = list(result.actions)
    original = actions[index]
    actions[index] = replace(original, arc=replace(original.arc, energy=original.arc.energy - 1))
    assert "incorrect energy or resource interval/unit" in validate(replace(result, actions=tuple(actions)))
    actions[index] = replace(
        original, arc=replace(original.arc, uses=(replace(original.arc.uses[0], unit="slot"), *original.arc.uses[1:]))
    )
    assert validate(replace(result, actions=tuple(actions)))
    slot = next(i for i, u in enumerate(original.arc.uses) if u.unit == "slot")
    altered = list(original.arc.uses)
    altered[slot] = replace(altered[slot], amount=True)
    actions[index] = replace(original, arc=replace(original.arc, uses=tuple(altered)))
    assert "invalid resource demand" in validate(replace(result, actions=tuple(actions)))
    engine.close()


def test_duplicate_job_and_tail_start_are_rejected() -> None:
    engine = joint()
    result = solve(engine)
    action = next(a for a in result.actions if a.arc.kind == "service")
    assert validate(replace(result, actions=(*result.actions, action)))
    changed = replace(action, arc=replace(action.arc, start=100, end=101))
    assert validate(replace(result, actions=(changed,)))
    engine.close()


def test_epoch_and_arrival_staleness_never_mutate_queues() -> None:
    engine = joint()
    result = solve(engine)
    other = joint()
    assert commit(other, result) == (False, "STALE")
    engine.add_task("inspection", "S0", 0)
    pending = list(engine.pending_tasks)
    assert commit(engine, result) == (False, "STALE")
    assert engine.pending_tasks == pending
    engine.close()
    other.close()


def test_staging_failure_rolls_back_tasks_metrics_and_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = joint()
    result = solve(engine)
    events = []
    engine.ledger.sink = events.append
    sequence, counts = engine.ledger.sequence, engine.ledger.counts.copy()
    before = copy.deepcopy([asdict(t) for t in engine.pending_tasks])
    assign = engine.assign
    calls = 0

    def faulty(task: object, robot: object, *, wake: bool = True) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected staging failure")
        return assign(task, robot, wake=wake)

    monkeypatch.setattr(engine, "assign", faulty)
    accepted, reason = commit(engine, result)
    assert not accepted and reason.startswith("COMMIT_ROLLED_BACK")
    assert [asdict(t) for t in engine.pending_tasks] == before
    assert not events and engine.ledger.sequence == sequence and engine.ledger.counts == counts
    assert all(
        r.current_task is None and r.planned_action is None and not r._wake.triggered for r in engine.robots.values()
    )
    assert_invariants(engine)
    engine.close()


def test_cancel_after_commit_is_safe() -> None:
    engine = joint()
    result = solve(engine)
    assert commit(engine, result)[0]
    assert engine.cancel_task("first")
    for at in range(1, 21):
        engine.env.run(until=at)
        assert_invariants(engine)
    assert engine._tasks_by_uid["first"].status == TaskState.CANCELLED
    assert engine.metrics.total_completed == 1
    engine.close()


def test_real_worker_wall_and_memory_limits_reap_child() -> None:
    engine = joint()
    worker = SolverProcess()
    tiny = Settings(horizon=20, step=2, timeout=0.001, watchdog=0.01)
    result = worker.solve(capture(engine, tiny, "timeout"), time.perf_counter())
    assert result.report.status == "HARD_TIMEOUT" and worker.process is None
    small = Settings(horizon=20, step=2, timeout=2, watchdog=4, memory_limit_mib=1)
    result = worker.solve(capture(engine, small, "memory"), time.perf_counter())
    assert result.report.status == "MEMORY_LIMIT" and worker.process is None
    worker.close()
    engine.close()


def test_explicit_plan_round_trip_and_reject_old_schema() -> None:
    engine = joint()
    result = solve(engine)
    assert decode(json.loads(json.dumps(encode(result)))) == result
    with pytest.raises(ValueError, match="schema"):
        decode({"schema": "clover-plan-v2"})
    engine.close()


def test_real_headless_rh_run_and_recorded_replay(tmp_path: Path) -> None:
    scenario = examples()[1]
    scenario.duration = 20
    scenario.workload = {
        "kind": "trace",
        "jobs": [
            {"uid": "first", "at": 0, "station": "S1", "allowed": ["S1"], "operation": "inspection", "noise": 0},
            {"uid": "second", "at": 0, "station": "S3", "allowed": ["S3"], "operation": "inspection", "noise": 0},
        ],
    }
    settings = asdict(Settings(horizon=20, step=2, sigma=0, timeout=5, watchdog=6))
    result = run_case(scenario, "rh_milp_grid", 1, planning_settings=settings, output=tmp_path)
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["completed_total"] == 2 and result["planning"]["accepted"] > 0
    loaded = json.loads((tmp_path / "joint_dispatch__rh_milp_grid__1.json").read_text())
    assert replay(loaded)["matches"]
    loaded["planning"]["decisions"][0]["accepted"] = False
    with pytest.raises(ValueError, match="decision hash"):
        replay(loaded)


def test_charge_then_service_is_feasible_and_charge_prefix_preserves_energy() -> None:
    scenario = examples()[0]
    scenario.factory.robots = scenario.factory.robots[:1]
    scenario.factory.robots[0].battery_capacity = 30
    scenario.factory.robots[0].charge_threshold = 0
    engine = SimulationEngine(
        scenario.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(200, 200, soc_pct=10)}
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.add_task("inspection", "S0", 2)
    result = solve(engine)
    assert result.report.phase1_cost == 0
    assert any(a.arc.kind == "charge" and a.gain > 0 for a in result.actions)
    assert any(a.arc.kind == "service" for a in result.actions)
    assert commit(engine, result)[0]
    engine.env.run(until=8)
    assert engine.robots["R0"].charged_energy_units >= 10.4 - 1e-7
    assert engine.metrics.total_completed == 0  # Only the charge prefix was committed.
    assert_invariants(engine)
    engine.close()


def test_opposite_narrow_corridor_requests_are_scheduled_without_overlap() -> None:
    scenario = examples()[2]
    engine = SimulationEngine(scenario.factory, demand_source="disabled", initial_robots=scenario.initial_robots)
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.robots["R0"].emergency_order = "east"
    engine.robots["R1"].emergency_order = "west"
    engine.add_task("inspection", "S1", 2, order_id="east")
    engine.add_task("inspection", "S0", 2, order_id="west")
    settings = Settings(horizon=30, step=2, sigma=0, timeout=5, watchdog=6)
    result = Backend().solve(prepare(capture(engine, settings, "narrow")))
    assert result.report.phase1_cost == 0 and not validate(result)
    services = [a for a in result.actions if a.arc.kind == "service"]
    assert len(services) == 2
    uses = sorted((u.start, u.end) for a in services for u in a.arc.uses if u.unit == "mm")
    assert uses[0][1] <= uses[1][0]
    assert max(a.arc.end for a in services) > 15  # The second service retains its tail.
    engine.close()


def test_restricted_cabin_path_never_selects_two_compound_rides() -> None:
    scenario = examples()[5]
    engine = SimulationEngine(
        scenario.factory,
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
        initial_cabins=scenario.initial_cabins,
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    engine.robots["R0"].emergency_order = "up"
    engine.robots["R1"].emergency_order = "down"
    engine.add_task("inspection", "S1", 2, order_id="up")
    engine.add_task("inspection", "S0", 2, order_id="down")
    settings = Settings(horizon=30, step=2, sigma=0, timeout=5, watchdog=6, joint_cabins=False)
    result = Backend().solve(prepare(capture(engine, settings, "cabin")))
    assert result.report.status in {"FEASIBLE", "OPTIMAL"} and not validate(result)
    assert sum(len(a.arc.cabins) for a in result.actions) <= 1
    assert len(result.deferred) >= 1
    engine.close()


def test_build_limit_is_explicit_and_keeps_input() -> None:
    engine = joint()
    config = Settings(horizon=20, step=2, max_arcs=1)
    data = prepare(capture(engine, config, "limit"))
    result = Backend().solve(data)
    assert result.report.status == "BUILD_LIMIT" and result.input == data and not result.actions
    engine.close()


def test_timeout_fallback_is_retained_and_replayed() -> None:
    scenario = examples()[1]
    scenario.duration = 20
    scenario.workload = {
        "kind": "trace",
        "jobs": [{"uid": "fallback", "at": 0, "station": "S1", "allowed": ["S1"], "operation": "inspection"}],
    }
    config = asdict(Settings(horizon=20, step=2, timeout=0.001, watchdog=0.01))
    result = run_case(scenario, "rh_milp_grid", 1, planning_settings=config)
    assert result["status"] == "SUCCESS" and result["completed_total"] == 1
    assert result["planning"]["accepted"] == 0 and result["planning"]["fallbacks"]
    assert replay(result)["matches"]


def test_configured_slow_charge_rate_is_not_clamped_upward() -> None:
    scenario = examples()[0]
    scenario.factory.robots = scenario.factory.robots[:1]
    scenario.factory.chargers[0].charge_rate = 0.001
    engine = SimulationEngine(
        scenario.factory, demand_source="disabled", initial_robots={"R0": RobotInitial(200, 200, soc_pct=10)}
    )
    engine.env.run(until=10)
    assert engine.robots["R0"].live_battery() == pytest.approx(10.01)
    assert_invariants(engine)
    engine.close()


def test_native_timeout_is_visible_when_validated_hold_hint_is_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = joint()
    backend = Backend()
    monkeypatch.setattr(backend, "phase", lambda *args, **kwargs: (None, "NO_INCUMBENT_TIMEOUT", None))
    result = backend.solve(prepare(capture(engine, Settings(horizon=20, step=2, warm_start=False), "timeout")))
    assert result.report.status == "FEASIBLE" and result.report.native_phase1_status == "NO_INCUMBENT_TIMEOUT"
    assert result.report.phase1_incumbent_source == "SAFE_HOLD_HINT" and not validate(result)
    engine.close()


def test_unforeseen_work_depletion_does_not_report_a_finished_service() -> None:
    scenario = examples()[0]
    scenario.factory.robots = scenario.factory.robots[:1]
    engine = SimulationEngine(
        scenario.factory,
        demand_source="disabled",
        plant=PlantSettings(1, 5, 15),
        initial_robots={"R0": RobotInitial(100, 200, soc_pct=25)},
    )
    initialize(engine)
    engine.scheduler.dispatch = lambda plant: None
    task = engine.add_task("inspection", "S0", 2)
    task.process_noise = 100  # Plant-only realization; capture never reads it.
    events = []
    engine.ledger.sink = events.append
    result = solve(engine)
    assert commit(engine, result)[0]
    engine.env.run(until=31)
    assert task.status == TaskState.QUEUED and engine.metrics.total_completed == 0
    assert engine.robots["R0"].planned_action is None
    assert any(e["event"] == "planning_action_end" and not e["finished"] for e in events)
    assert_invariants(engine)
    engine.close()


def stalled_worker(connection: object) -> None:
    """A real child that does not read its large request or return a response."""
    time.sleep(5)


def test_watchdog_interrupts_blocked_large_ipc_send(monkeypatch: pytest.MonkeyPatch) -> None:
    import simulation.planning.process as process_module

    engine = joint()
    settings = Settings(horizon=20, step=2, timeout=0.01, watchdog=0.1)
    snapshot = capture(engine, settings, "large-ipc")
    snapshot = replace(snapshot, factory_json=" " * (1024 * 1024) + snapshot.factory_json)
    monkeypatch.setattr(process_module, "_worker", stalled_worker)
    worker = SolverProcess()
    started = time.perf_counter()
    result = worker.solve(snapshot, started)
    assert result.report.status == "HARD_TIMEOUT" and time.perf_counter() - started < 3
    assert worker.process is None and worker.rpc_thread is None
    engine.close()
