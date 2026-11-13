"""Readiness, generation, startup watchdog and reaping checks on real children."""

from __future__ import annotations

import copy
from multiprocessing.connection import Connection

import pytest

from simulation.decisions.observation import capture
from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import examples
from simulation.planning.process import SolverProcess
from simulation.planning.test_snapshot import initialize


def forged_worker(connection: Connection) -> None:
    """Simulate a late reply from a different owned generation."""
    payload = connection.recv()
    value = {key: payload[key] for key in ("session", "generation", "request_id", "epoch", "policy")}
    value["generation"] += 1
    connection.send({**value, "status": "READY", "value": {}})
    try:
        connection.recv()
    except EOFError:
        pass


def silent_worker(connection: Connection) -> None:
    """Read a command, then block forever waiting for a command that never comes."""
    connection.recv()
    try:
        connection.recv()
    except EOFError:
        pass


@pytest.mark.parametrize(
    "policy",
    (
        "baseline",
        "optimized",
        "assignment_milp_legacy",
        "rh_milp_grid",
        "rh_cpsat_linear",
        "rh_cpsat_interval",
        "rh_cpsat_native",
    ),
)
def test_selected_backend_ready_ack_and_generation_reuse(policy: str) -> None:
    worker = SolverProcess()
    try:
        reply = worker.request(
            "ready",
            policy,
            {},
            request_id="ready",
            epoch="test",
            timeout=10,
            memory_limit_mib=1024,
        )
        assert reply["status"] == "READY" and worker.ready_for == policy
        first_session, first_generation = worker.session, worker.generation
        reply = worker.request(
            "ready",
            policy,
            {},
            request_id="again",
            epoch="test",
            timeout=10,
            memory_limit_mib=1024,
        )
        assert reply["worker_reused"] and worker.generation == first_generation and worker.session == first_session
        worker.close()
        assert worker.process is None and worker.connection is None and worker.rpc_thread is None
        reply = worker.request(
            "ready",
            policy,
            {},
            request_id="restart",
            epoch="test",
            timeout=10,
            memory_limit_mib=1024,
        )
        assert (
            reply["status"] == "READY" and worker.generation == first_generation + 1 and worker.session != first_session
        )
    finally:
        worker.close()


def test_startup_timeout_and_memory_limit_reap_worker() -> None:
    worker = SolverProcess()
    try:
        reply = worker.request(
            "ready",
            "rh_cpsat_native",
            {},
            request_id="timeout",
            epoch="t",
            timeout=0.001,
            memory_limit_mib=1024,
        )
        assert reply["status"] == "STARTUP_TIMEOUT" and worker.process is None
        reply = worker.request(
            "ready",
            "rh_cpsat_native",
            {},
            request_id="rss",
            epoch="t",
            timeout=10,
            memory_limit_mib=1,
        )
        assert reply["status"] == "STARTUP_MEMORY_LIMIT" and worker.process is None
    finally:
        worker.close()


def test_immutable_selector_roundtrip_requires_ready_worker() -> None:
    scenario = examples()[0]
    engine = SimulationEngine(
        copy.deepcopy(scenario.factory),
        demand_source="disabled",
        initial_robots=scenario.initial_robots,
    )
    initialize(engine)
    engine.add_task("inspection", "S0", 2, external_uid="one")
    observation = capture(engine, "baseline", "one")
    worker = SolverProcess()
    try:
        args = {"observation": observation, "options": {}}
        reply = worker.request(
            "propose",
            "baseline",
            args,
            request_id="bad",
            epoch=engine.execution_epoch,
            timeout=5,
            memory_limit_mib=1024,
        )
        assert reply["status"] == "WORKER_CRASH" and not any(r.current_task for r in engine.robot_list())
        assert (
            worker.request(
                "ready",
                "baseline",
                {},
                request_id="ready",
                epoch=engine.execution_epoch,
                timeout=5,
                memory_limit_mib=1024,
            )["status"]
            == "READY"
        )
        reply = worker.request(
            "propose",
            "baseline",
            args,
            request_id="good",
            epoch=engine.execution_epoch,
            timeout=5,
            memory_limit_mib=1024,
        )
        assert reply["status"] == "OK" and reply["value"].observation == observation
        assert reply["value"].intents[0].job == "one"
    finally:
        worker.close()
        engine.close()


@pytest.mark.parametrize(
    "target,expected", ((forged_worker, "STARTUP_STALE_GENERATION"), (silent_worker, "STARTUP_TIMEOUT"))
)
def test_bad_or_hung_child_is_rejected_and_reaped(
    monkeypatch: pytest.MonkeyPatch, target: object, expected: str
) -> None:
    import simulation.planning.process as module

    monkeypatch.setattr(module, "_worker", target)
    worker = SolverProcess()
    try:
        reply = worker.request(
            "ready", "baseline", {}, request_id="identity", epoch="e", timeout=2, memory_limit_mib=1024
        )
        assert reply["status"] == expected
        assert worker.process is None and worker.connection is None and worker.rpc_thread is None
    finally:
        worker.close()
