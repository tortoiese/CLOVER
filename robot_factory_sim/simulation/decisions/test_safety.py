"""Adversarial publication, charging, lifecycle and timing-coverage regressions."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from simulation.audit import assert_invariants
from simulation.decisions import admission
from simulation.decisions.adapters import propose
from simulation.decisions.contracts import DecisionSettings
from simulation.decisions.observation import capture
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples, joint_batch
from simulation.evaluation.runner import replay, run_case
from simulation.planning.process import SolverProcess
from simulation.planning.test_snapshot import initialize


def observed() -> tuple[SimulationEngine, Any]:
    scenario = examples()[0]
    engine = SimulationEngine(
        copy.deepcopy(scenario.factory), demand_source="disabled", initial_robots=scenario.initial_robots
    )
    initialize(engine)
    engine.add_task("inspection", "S0", 2, external_uid="one")
    return engine, propose(capture(engine, "optimized", "safe"))


@pytest.mark.parametrize("change", ("cancel", "epoch", "config", "duplicate", "battery", "retire", "critical"))
def test_changed_physical_dependency_rejects_before_any_live_mutation(change: str) -> None:
    engine, proposal = observed()
    if change == "cancel":
        engine.cancel_task("one")
    elif change == "epoch":
        engine.execution_epoch = "new"
    elif change == "config":
        engine.stations["S0"].cfg.is_active = False
    elif change == "duplicate":
        proposal = replace(proposal, intents=(*proposal.intents, proposal.intents[0]))
    elif change == "battery":
        robot = engine.robots[proposal.intents[0].robot]
        robot.battery -= 1
    elif change == "retire":
        engine.robots[proposal.intents[0].robot].alive = False
    else:
        engine.add_task("inspection", "S1", 0, external_uid="urgent")
    pending = list(engine.pending_tasks)
    receipt = admission.publish(engine, proposal, {})
    assert not receipt["accepted"] and not receipt["applied"]
    assert engine.pending_tasks == pending and not any(r.current_task for r in engine.robot_list())
    engine.close()


def test_unrelated_arrival_is_retained_and_not_added_to_past_proposal() -> None:
    engine, proposal = observed()
    engine.add_task("inspection", "S1", 3, external_uid="new")
    assert not admission.check(engine, proposal)
    receipt = admission.publish(engine, proposal, {})
    assert receipt["accepted"] and [t.external_uid for t in engine.pending_tasks] == ["new"]
    assert_invariants(engine)
    engine.close()


@pytest.mark.parametrize("policy", ("baseline", "optimized", "assignment_milp_legacy"))
def test_low_soc_charging_selection_matches_original_rules(policy: str) -> None:
    scenario = examples()[4]
    engine = SimulationEngine(
        scenario.factory, policy, demand_source="disabled", initial_robots=scenario.initial_robots
    )
    initialize(engine)
    for robot in engine.robot_list():
        robot.battery = robot.capacity * 0.1
        robot.initial_energy_units = robot.battery
    proposal = propose(capture(engine, policy, "charge"), charge_robots=tuple(engine.robot_ids()))
    choices = [i for i in proposal.intents if i.kind == "charge_choice"]
    assert choices
    for choice in choices:
        robot = engine.robots[choice.robot]
        assert choice.destination == engine.scheduler.choose_charger(robot, engine).id
        assert choice.target == engine.scheduler.charge_target(robot, engine)
    assert not any(r.forced_charge for r in engine.robot_list())
    engine.close()


def test_startup_failure_retries_are_bounded_and_recorded_replay_matches(tmp_path: Path) -> None:
    scenario = joint_batch()
    scenario.duration = 10
    config = DecisionSettings(startup_timeout_seconds=0.001, max_worker_restarts=1, restart_backoff_seconds=(0.1,))
    result = run_case(scenario, "optimized", 1001, output=tmp_path, decision_settings=config.to_dict())
    assert result["status"] == "SUCCESS", result["failure"]
    assert result["control"]["restarts"] == 1
    assert len(result["control"]["lifecycle"]) == 2
    assert all(r["reply"]["status"] == "STARTUP_TIMEOUT" for r in result["control"]["lifecycle"])
    assert any(r["fallback"] is not None for r in result["control"]["decisions"])
    assert replay(result)["matches"]


def test_publication_overrun_invalidates_run_without_retroactive_shift(monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = joint_batch()
    scenario.duration = 5
    settings = DecisionSettings(publication_allowance_seconds=0.000001)
    result = run_case(scenario, "optimized", 1001, decision_settings=settings.to_dict())
    assert result["status"] == "TIMING_COVERAGE_VIOLATION"
    assert not result["control"]["coverage_ok"] and result["final_audit"]["ok"]
    assert any(not r["coverage_ok"] for r in result["control"]["decisions"])
    assert replay(result)["matches"]


def test_operational_crash_is_reaped_then_restarted_with_new_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    original = SolverProcess.request
    injected = [False]

    def request(self: SolverProcess, command: str, *args: Any, **kwargs: Any) -> dict:
        if command == "propose" and not injected[0]:
            injected[0] = True
            self.process.kill()
            self.process.join(timeout=1)
        return original(self, command, *args, **kwargs)

    monkeypatch.setattr(SolverProcess, "request", request)
    scenario = joint_batch()
    scenario.duration = 25
    result = run_case(scenario, "optimized", 1001, decision_settings=DecisionSettings().to_dict())
    assert result["status"] == "SUCCESS", result["failure"]
    ready = [r for r in result["control"]["lifecycle"] if "restart" in r]
    assert result["control"]["restarts"] == 1 and len(ready) == 2
    generations = [r["reply"]["generation"] for r in ready]
    assert generations == [1, 2]
    assert any(r.get("compute", {}).get("status") == "WORKER_CRASH" for r in result["control"]["decisions"])
    assert replay(result)["matches"]
