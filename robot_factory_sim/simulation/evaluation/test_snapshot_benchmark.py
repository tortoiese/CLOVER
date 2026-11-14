"""Real saved observations, compiler parity, corruption and failure retention."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from simulation.decisions.contracts import DecisionSettings
from simulation.decisions.rpc import handle
from simulation.evaluation import snapshot_benchmark as benchmark
from simulation.evaluation.fixtures import joint_batch
from simulation.evaluation.frozen_snapshot import freeze_snapshot, load_snapshot
from simulation.evaluation.runner import run_case, source_manifest
from simulation.evaluation.scenario import fingerprint
from simulation.planning.backend import Backend
from simulation.planning.contracts import Settings
from simulation.planning.incumbent import Hint
from simulation.planning.validation import validate

pytest.importorskip("ortools")


@pytest.fixture(scope="module")
def saved(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, int]:
    """Generate a genuine common-D1 run and freeze its first nonempty input."""
    root = tmp_path_factory.mktemp("frozen-grid")
    case = joint_batch()
    case.duration = 40
    run = run_case(
        case, "rh_milp_grid", 1001, output=root,
        planning_settings=asdict(Settings(horizon=10, interval=2, step=2, sigma=0, timeout=2, watchdog=3)),
        decision_settings=DecisionSettings(end_to_end_soft_deadline_seconds=10,
                                           publication_allowance_seconds=.2).to_dict(),
    )
    assert run["status"] == "SUCCESS", run["failure"]
    index = next(i for i, d in enumerate(run["control"]["decisions"])
                 if (((d.get("compute") or {}).get("value") or {}).get("plan") or {})
                 .get("input", {}).get("snapshot", {}).get("jobs"))
    path = root / f"{run['case_id']}.json"
    frozen = root / "snapshot.json"
    freeze_snapshot(path, index, frozen)
    return path, frozen, index


def test_frozen_model_and_hint_prove_same_optimum(saved: tuple[Path, Path, int]) -> None:
    _, path, _ = saved
    body, model, hint = load_snapshot(path)
    before = fingerprint({"variables": [asdict(v) for v in model.variables],
                          "rows": [asdict(r) for r in model.rows], "hint": asdict(hint)})
    results = [Backend(kind).solve(model.input, model=model, hint=hint, budget_seconds=5)
               for kind in ("milp", "cpsat_linear")]
    assert all(r.report.status == "OPTIMAL" and not validate(r) for r in results)
    assert len({(r.report.phase1_cost, r.report.phase2_cost) for r in results}) == 1
    assert {r.report.model_hash for r in results} == {body["model_hash"]}
    assert before == fingerprint({"variables": [asdict(v) for v in model.variables],
                                  "rows": [asdict(r) for r in model.rows], "hint": asdict(hint)})
    with pytest.raises(ValueError, match="prebuilt"):
        Backend("cpsat_native").solve(model.input, model=model, hint=hint, budget_seconds=2)
    with pytest.raises(ValueError, match="prebuilt"):
        Backend().solve(model.input, model=model)
    invalid = Hint(tuple(999999 for _ in hint.values), "BROKEN")
    assert Backend().solve(model.input, model=model, hint=invalid, budget_seconds=2).report.status == "MODEL_INVALID"


@pytest.mark.parametrize("fault", ("artifact", "model", "hint", "source", "native"))
def test_corrupt_or_incompatible_frozen_inputs_rejected(
    saved: tuple[Path, Path, int], tmp_path: Path, fault: str,
) -> None:
    body = json.loads(saved[1].read_text())
    if fault == "artifact":
        body["variables"] += 1
    else:
        if fault == "model":
            body["model_hash"] = "wrong"
        elif fault == "hint":
            body["hint"]["values"][0] = 999999
            body["hint_hash"] = fingerprint(body["hint"])
        elif fault == "source":
            body["source"]["source_hash"] = "other source"
        else:
            body["prepared"]["input"]["snapshot"]["settings"]["formulation"] = "native"
            body["input_hash"] = fingerprint(body["prepared"]["input"])
        body["artifact_hash"] = fingerprint({k: v for k, v in body.items() if k != "artifact_hash"})
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(body))
    with pytest.raises(ValueError):
        load_snapshot(path)


def test_rejected_worker_plan_is_retained_and_record_disagreement_rejected(
    saved: tuple[Path, Path, int], tmp_path: Path,
) -> None:
    original, _, index = saved
    run = json.loads(original.read_text())
    record = run["control"]["decisions"][index]
    record.update(rh_result=None, accepted=False, reason="END_TO_END_SOFT_DEADLINE")
    run["control"]["decisions_hash"] = fingerprint(run["control"]["decisions"])
    path = tmp_path / "rejected.json"
    path.write_text(json.dumps(run))
    frozen = tmp_path / "rejected-snapshot.json"
    body = freeze_snapshot(path, index, frozen)
    assert not body["origin"]["accepted"] and body["origin"]["worker_return_used"]
    record["rh_result"] = copy.deepcopy(record["compute"]["value"]["plan"])
    record["rh_result"]["report"]["status"] = "ERROR"
    run["control"]["decisions_hash"] = fingerprint(run["control"]["decisions"])
    path.write_text(json.dumps(run))
    with pytest.raises(ValueError, match="differ"):
        freeze_snapshot(path, index, tmp_path / "disagreement.json")
    with pytest.raises(ValueError, match="exists"):
        freeze_snapshot(original, index, frozen)


def test_cli_real_workers_validated_and_disposed(saved: tuple[Path, Path, int], tmp_path: Path) -> None:
    output = tmp_path / "benchmark"
    process = subprocess.run(
        [sys.executable, "-B", "-m", "simulation.evaluation", "benchmark-snapshot",
         "--snapshot", str(saved[1]), "--output", str(output), "--budgets", "5", "--repeats", "1"],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["planned_runs"] == 2 and manifest["status"] == "SUCCESS"
    assert not manifest["parity_mismatches"]
    for row in manifest["runs"]:
        artifact = json.loads((output / row["result"]).read_text())
        assert all(artifact["disposed"].values()) and row["valid_plan"] and row["timed_eligible"]
        assert artifact["result"]["report"]["model_hash"] == manifest["model_hash"]
        assert artifact["hint_hash"] == manifest["hint_hash"]
    with pytest.raises(ValueError, match="exists"):
        benchmark.benchmark_snapshot(saved[1], output)


def test_budget_and_source_drift_keep_all_planned_arms(
    saved: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = benchmark.benchmark_snapshot(saved[1], tmp_path / "budget", suite_wall_limit=.001)
    assert len(result["runs"]) == 12 and result["statuses"] == {"NOT_RUN_BUDGET": 12}
    source = source_manifest()
    calls = 0

    def changed() -> dict:
        nonlocal calls
        calls += 1
        return source if calls == 1 else {**source, "source_hash": "changed"}

    monkeypatch.setattr(benchmark, "source_manifest", changed)
    result = benchmark.benchmark_snapshot(saved[1], tmp_path / "drift")
    assert result["statuses"] == {"SOURCE_CHANGED": 12}
    assert all(row["result"] is None for row in result["runs"])


def test_prebuilt_options_require_same_input(saved: tuple[Path, Path, int]) -> None:
    _, model, hint = load_snapshot(saved[1])
    other = replace(model.input, snapshot=replace(model.input.snapshot, token="different"))
    with pytest.raises(ValueError, match="matching"):
        Backend().solve(other, model=model, hint=hint, budget_seconds=2)


@pytest.mark.parametrize("budgets", ((), (.25, .25), (0,), (float("inf"),), (True,)))
def test_invalid_budgets_fail_before_execution(saved: tuple[Path, Path, int], tmp_path: Path, budgets: tuple) -> None:
    with pytest.raises(ValueError):
        benchmark.benchmark_snapshot(saved[1], tmp_path / "invalid", budgets=budgets)
    assert not (tmp_path / "invalid").exists()


@pytest.mark.parametrize("fault", ("late", "parity", "cross_budget", "startup", "mutated",
                                   "unexpected", "bad_reply", "close_failure"))
def test_failure_guards_keep_denominators_and_close_workers(
    saved: tuple[Path, Path, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    path = tmp_path / "snapshot.json"
    path.write_bytes(saved[1].read_bytes())
    _, model, hint = load_snapshot(path)
    valid = Backend().solve(model.input, model=model, hint=hint, budget_seconds=5)
    assert valid.report.status == "OPTIMAL" and not validate(valid)
    closed: list[bool] = []
    computes: list[str] = []

    class Worker:
        """Inject transport/proof faults after producing a real valid optimum."""

        process = connection = rpc_thread = None

        def request(self, command: str, policy: str, args: dict, **kwargs: object) -> dict:
            """Return deterministic test faults without running a second solver."""
            checkpoint = json.loads((output / "manifest.json").read_text())
            assert len(checkpoint["runs"]) == checkpoint["planned_runs"]
            assert any(r["status"] == "RUNNING" for r in checkpoint["runs"])
            if fault == "unexpected":
                raise RuntimeError("unexpected worker error")
            base = {"elapsed_seconds": .01, "peak_rss_mib": 50}
            if command == "ready":
                if fault == "mutated":
                    path.write_bytes(path.read_bytes() + b"\n")
                return {**base, "status": "STARTUP_UNAVAILABLE" if fault == "startup" else "READY"}
            computes.append(policy)
            if fault == "bad_reply":
                return {**base, "status": "OK", "value": {}}
            report = replace(valid.report, backend="milp" if policy == "rh_milp_grid" else "cpsat_linear",
                             total_seconds=args["budget_seconds"] + .1 if fault == "late" else 0)
            if fault == "parity" and policy == "rh_cpsat_linear":
                report = replace(report, phase2_cost=report.phase2_cost + 1)
            if fault == "cross_budget" and args["budget_seconds"] > 1:
                report = replace(report, phase2_cost=report.phase2_cost + 1)
            return {**base, "status": "OK", "value": {"plan": replace(valid, report=report),
                                                       "validation_errors": (), "validation_seconds": .01}}

        def close(self) -> None:
            """Record disposal even for failed startup or changing inputs."""
            closed.append(True)
            if fault == "close_failure":
                raise RuntimeError("worker close failed")

    monkeypatch.setattr(benchmark, "SolverProcess", Worker)
    output = tmp_path / "runs"
    result = benchmark.benchmark_snapshot(path, output, budgets=(1, 2) if fault == "cross_budget" else (1,), repeats=1)
    assert len(result["runs"]) == (4 if fault == "cross_budget" else 2)
    if fault in {"parity", "cross_budget"}:
        assert result["status"] == "PARITY_MISMATCH" and result["parity_mismatches"]
    elif fault == "late":
        assert result["statuses"] == {"SOFT_BUDGET_EXCEEDED": 2}
        assert all(r["valid_plan"] and not r["timed_eligible"] for r in result["runs"])
    elif fault == "startup":
        assert result["statuses"] == {"STARTUP_UNAVAILABLE": 2} and not computes
    elif fault in {"unexpected", "bad_reply", "close_failure"}:
        code = "DISPOSAL_FAILED" if fault == "close_failure" else "EVALUATION_ERROR"
        assert result["status"] == "PARTIAL"
        assert result["statuses"] == {code: 1, "NOT_RUN_" + code: 1}
        first = result["runs"][0]
        artifact = json.loads((output / first["result"]).read_text())
        assert artifact["error"] if fault != "close_failure" else artifact["disposal_error"]
        assert first["result_sha256"] and result["runs"][1]["result"] is None
        assert len(closed) == 1
    else:
        assert result["statuses"] == {"SNAPSHOT_CHANGED": 2} and len(closed) == 1 and not computes
    assert len(closed) == sum(row["result"] is not None for row in result["runs"])


def test_worker_times_complete_backend_return_not_only_internal_report(
    saved: tuple[Path, Path, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, model, hint = load_snapshot(saved[1])
    result = Backend().solve(model.input, model=model, hint=hint, budget_seconds=5)
    assert result.report.status == "OPTIMAL"

    def delayed(self: Backend, data: object, **kwargs: object) -> object:
        assert kwargs["started"] <= time.perf_counter()
        time.sleep(.01)
        return replace(result, report=replace(result.report, total_seconds=0))

    monkeypatch.setattr(Backend, "solve", delayed)
    payload = {"session": "test", "generation": 1, "request_id": "complete", "epoch": "test",
               "policy": "rh_milp_grid", "command": "frozen_grid",
               "arguments": {"model": model, "hint": hint, "budget_seconds": 1},
               "started": time.perf_counter()}
    reply, _ = handle(payload, "rh_milp_grid")
    assert reply["status"] == "OK"
    assert reply["value"]["plan"].report.total_seconds >= .01
