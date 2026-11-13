"""M1 differential checks against production selectors on complete observations."""

from __future__ import annotations

import copy
import json
import pickle
from dataclasses import asdict

import pytest

from simulation.decisions.adapters import propose
from simulation.decisions.observation import capture
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.test_snapshot import initialize


@pytest.mark.parametrize("policy", ("baseline", "optimized", "assignment_milp_legacy"))
@pytest.mark.parametrize("case", range(6))
def test_detached_selector_matches_live_choice_and_never_changes_plant(policy: str, case: int) -> None:
    scenario = examples()[case]
    engine = SimulationEngine(
        copy.deepcopy(scenario.factory),
        policy,
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
        initial_cabins=scenario.initial_cabins,
    )
    initialize(engine)
    for i, key in enumerate(engine.stations):
        engine.add_task("inspection", key, 2, external_uid=f"job{i}")
        engine.pending_tasks[-1].process_noise = 99
    before = [asdict(t) for t in engine.pending_tasks]
    observation = capture(engine, policy, "test")
    assert "process_noise" not in observation.payload and len(json.loads(observation.payload)["tasks"]) == len(before)
    assert pickle.loads(pickle.dumps(observation)) == observation
    proposal = propose(observation)
    assert [asdict(t) for t in engine.pending_tasks] == before
    assert all(r.current_task is None and not r.forced_charge for r in engine.robot_list())
    engine.scheduler.dispatch(engine)
    expected = {
        (r.robot_id, r.current_task.external_uid, r.current_task.station_id)
        for r in engine.robot_list()
        if r.current_task is not None
    }
    actual = {(i.robot, i.job, i.destination) for i in proposal.intents if i.kind == "assign"}
    assert actual == expected
    assert {i.robot for i in proposal.intents if i.kind == "charge"} == {
        r.robot_id for r in engine.robot_list() if r.forced_charge
    }
    state = json.loads(proposal.state)
    if policy == "baseline":
        assert state["pointer"] == engine.scheduler._pointer
    engine.close()


def test_all_jobs_and_charger_insertion_order_are_preserved() -> None:
    scenario = examples()[1]
    scenario.factory.chargers.reverse()
    engine = SimulationEngine(
        scenario.factory,
        "optimized",
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
    )
    initialize(engine)
    for i in range(45):
        engine.add_task("inspection", "S0", 2, external_uid=f"j{i}")
    observation = capture(engine, "optimized", "many")
    data = json.loads(observation.payload)
    assert len(data["tasks"]) == 45 and data["charger_order"] == list(engine.chargers)
    proposal = propose(observation, dispatch=False, charge_robots=("R0",))
    assert proposal.observation == observation and len(engine.pending_tasks) == 45
    engine.close()
