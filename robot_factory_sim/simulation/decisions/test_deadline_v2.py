"""Primary deadline enforcement, pre-worker evidence and deadline cohorts."""

from __future__ import annotations

from pathlib import Path

import pytest
import simpy

from simulation.decisions.contracts import DEADLINE_PROTOCOL, DecisionSettings, digest
from simulation.decisions.controller import DecisionScheduler
from simulation.decisions.input_store import load_planning_input
from simulation.decisions.metrics import control_summary
from simulation.decisions.timing import DelayLedger
from simulation.evaluation.fixtures import joint_batch
from simulation.evaluation.frozen_snapshot import freeze_snapshot
from simulation.evaluation.runner import replay, run_case
from simulation.planning.process import SolverProcess


def settings(**changes: object) -> dict:
    """Use a research-only profile while leaving legacy defaults intact."""
    return DecisionSettings(**{
        "timing_protocol": DEADLINE_PROTOCOL, "primary_deadline_policy": "reject_late_primary",
        "admission_reserve_seconds": .1, "record_planning_inputs": True,
        "end_to_end_soft_deadline_seconds": 2, "publication_allowance_seconds": .2, **changes,
    }).to_dict()


@pytest.mark.parametrize("start,prepare_cost,accepted", (
    (1.6, .1, True), (1.7, .15, False), (1.8, 0, True), (1.80000001, 0, False), (1.9, .01, False)))
def test_primary_cannot_publish_late(start: float, prepare_cost: float, accepted: bool) -> None:
    class Engine:
        mode = "optimized"
        env = simpy.Environment()

        @property
        def now(self) -> float:
            return self.env.now

    engine = Engine()
    scheduler = DecisionScheduler(engine, settings())
    engine.env.run(until=start)
    now, applied = [0.0], []
    ledger = DelayLedger(engine.env, .2, lambda: now[0])

    def prepare() -> tuple[bool, str]:
        now[0] += prepare_cost
        return True, ""

    def publish() -> dict:
        applied.append(engine.now)
        return {"accepted": True, "reason": "COMMITTED", "applied": []}

    event = engine.env.process(scheduler._admit_process(ledger, {"triggered_at": 0}, prepare, publish))
    engine.env.run()
    assert event.value["accepted"] == accepted
    assert all(t <= 2 + 1e-9 for t in applied)
    assert bool(applied) == accepted
    scheduler.close()


@pytest.mark.parametrize("changes", ({"record_planning_inputs": False}, {"admission_reserve_seconds": 0},
                                      {"publication_allowance_seconds": 2}, {"primary_deadline_policy": "soft"},
                                      {"admission_reserve_seconds": float("nan")}))
def test_v2_rejects_incomplete_contract(changes: dict) -> None:
    with pytest.raises(ValueError):
        DecisionSettings(**settings(**changes)).validate()
    legacy = DecisionSettings()
    legacy.validate()
    assert legacy.primary_deadline_policy == "soft" and not legacy.record_planning_inputs


def test_timeout_input_survives_and_exports_and_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    budgets = []

    def unavailable(self: SolverProcess, command: str, policy: str, args: dict, **kwargs: object) -> dict:
        if command == "ready":
            return {"status": "READY", "value": {}, "elapsed_seconds": 0, "peak_rss_mib": 0}
        if command == "rh":
            budgets.append((kwargs["timeout"], args["snapshot"].settings.timeout))
        return {"status": "HARD_TIMEOUT", "value": None, "elapsed_seconds": 0, "peak_rss_mib": 0}

    monkeypatch.setattr(SolverProcess, "request", unavailable)
    case = joint_batch()
    case.duration = 30
    for job in case.workload["jobs"]:
        job["at"] = 3
        job["deadline"] += 3
    result = run_case(case, "rh_milp_grid", 1001, output=tmp_path,
                      planning_settings={"horizon": 10, "interval": 2, "step": 2, "sigma": 0},
                      decision_settings=settings())
    assert result["status"] == "SUCCESS", result["failure"]
    rows = result["control"]["decisions"]
    index = next(i for i, row in enumerate(rows) if row.get("planning_input_ref")
                 and row.get("compute", {}).get("status") == "HARD_TIMEOUT")
    row = rows[index]
    assert row["rh_result"] is None and row["planning_input_ref"]
    snap = load_planning_input(result["control"], row["planning_input_ref"], tmp_path)
    assert snap.jobs and snap.token == row["token"]
    assert budgets and all(request <= 1.7 and math <= 1.7 for request, math in budgets)
    path = tmp_path / f"{result['case_id']}.json"
    frozen = freeze_snapshot(path, index, tmp_path / "timeout-input.json")
    assert frozen["origin"]["input_origin"] == "pre_rpc_snapshot"
    assert not frozen["origin"]["worker_plan_present"]
    assert replay(result, input_root=tmp_path)["matches"]
    stored = tmp_path / result["control"]["planning_inputs"][row["planning_input_ref"]]["path"]
    raw = stored.read_bytes()
    stored.write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="file hash mismatch"):
        replay(result, input_root=tmp_path)
    stored.write_bytes(raw)
    ref = row["planning_input_ref"]
    entry = result["control"]["planning_inputs"][ref]
    entry["path"] = "../escape.json"
    result["control"]["planning_inputs_hash"] = digest(result["control"]["planning_inputs"])
    with pytest.raises(ValueError, match="escapes"):
        load_planning_input(result["control"], ref, tmp_path)


def test_deadline_cohort_censors_late_window_and_separates_auxiliary() -> None:
    def row(at: float, end: float | None, accepted: bool = False, kind: str = "dispatch") -> dict:
        return {"triggered_at": at, "time": end, "accepted": accepted,
                "coverage_ok": True, "request_kind": kind, "fallback": None}

    control = {"settings": {"end_to_end_soft_deadline_seconds": 2},
               "decisions": [row(0, 1, True), row(2, 4.1, True), row(4, None), row(8, None),
                             row(1, 1.1, True, "auxiliary")]}
    summary = control_summary(control, 0, 10)["by_kind"]
    assert summary["dispatch"]["eligible"] == 3
    assert summary["dispatch"]["timely_accepted"] == 1
    assert summary["dispatch"]["late_accepted"] == 1
    assert summary["dispatch"]["deadline_exceeded"] == 2
    assert summary["dispatch"]["right_censored_deadline"] == 1
    assert summary["dispatch"]["fallback_rate"] is None
    assert summary["auxiliary"]["eligible"] == 1


def test_observation_deadline_falls_back_and_replays_without_fabricating_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from simulation.decisions import controller

    actual_capture = controller.capture

    def expired(*args: object, **kwargs: object) -> object:
        if args[1] == "rh_milp_grid":
            raise TimeoutError("policy observation exceeded budget")
        return actual_capture(*args, **kwargs)

    def ready_only(self: SolverProcess, command: str, *args: object, **kwargs: object) -> dict:
        assert command == "ready", "no solver input exists after an observation timeout"
        return {"status": "READY", "value": {}, "elapsed_seconds": 0, "peak_rss_mib": 0}

    monkeypatch.setattr(controller, "capture", expired)
    monkeypatch.setattr(SolverProcess, "request", ready_only)
    case = joint_batch()
    case.duration = 30
    result = run_case(case, "rh_milp_grid", 1001, output=tmp_path, decision_settings=settings())
    assert result["status"] == "SUCCESS", result["failure"]
    timed_out = [r for r in result["control"]["decisions"] if r.get("capture_timeout")]
    assert timed_out and all(r["reason"] == "OBSERVATION_BUDGET_EXCEEDED" for r in timed_out)
    assert all(r["fallback"]["cause"] == "OBSERVATION_BUDGET_EXCEEDED" for r in timed_out)
    assert not result["control"]["planning_inputs"]
    monkeypatch.setattr(controller, "capture", actual_capture)
    assert replay(result, input_root=tmp_path)["matches"]
