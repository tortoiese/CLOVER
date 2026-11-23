"""A static experiment must detect changes without changing normal plant behavior."""

import pytest

from simulation.evaluation import runner
from simulation.evaluation.fixtures import examples


def test_static_audit_and_replay() -> None:
    case = examples()[0]
    case.duration = 6
    result = runner.run_case(case, "baseline", 31, static_layout=True)
    assert result["status"] == "SUCCESS", result["failure"]
    audit = result["static_layout_audit"]
    assert audit["ok"] and audit["checks"] == 7
    assert audit["initial_factory_hash"] == audit["final_factory_hash"]
    assert runner.replay(result)["matches"]


def test_static_suite_propagates_guard_and_retains_archived_result(tmp_path) -> None:
    import json

    from simulation.artifacts import read_saved

    case = examples()[0]
    case.duration = 6
    result = runner.run_suite((case,), (31,), tmp_path / "suite", policies=("baseline",),
                              static_layout=True, compress_artifacts=True)
    assert result["status"] == "SUCCESS" and result["static_layout"]
    run = json.loads(read_saved(tmp_path / "suite" / result["runs"][0]["result"]))
    assert run["static_layout_audit"]["ok"]


def test_static_detects_unannounced_geometry_mutation(monkeypatch) -> None:
    original = runner.SimulationEngine

    def engine_with_mutation(*args, **kwargs):
        engine = original(*args, **kwargs)

        def mutate():
            yield engine.env.timeout(0.5)
            engine.factory.edges[0].width = 1.3

        engine.env.process(mutate())
        return engine

    monkeypatch.setattr(runner, "SimulationEngine", engine_with_mutation)
    case = examples()[0]
    case.duration = 6
    result = runner.run_case(case, "baseline", 31, static_layout=True)
    assert result["status"] == "FAILED"
    assert "static-layout" in result["failure"]
    assert not result["static_layout_audit"]["ok"]
    assert result["observed_seconds"] < case.duration


def test_static_rejects_non_boolean_and_interventions_before_output(tmp_path) -> None:
    case = examples()[0]
    with pytest.raises(ValueError, match="boolean"):
        runner.run_case(case, "baseline", 31, static_layout=1)
    from simulation.evaluation.scenario import Intervention

    case.interventions = (Intervention(1, "edge_width", "1", 1.3),)
    with pytest.raises(ValueError, match="empty intervention"):
        runner.run_suite((case,), (31,), tmp_path / "suite", static_layout=True)
    assert not (tmp_path / "suite").exists()
