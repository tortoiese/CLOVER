"""Native process supervision and latest-state publication in the live Qt adapter."""
from __future__ import annotations

import time
from dataclasses import replace

from simulation.engine import SimulationEngine
from simulation.evaluation.fixtures import joint_batch
from simulation.planning.contracts import Result, Settings
from simulation.planning.live import LiveRollingHorizonScheduler, LiveSolverProcess
from simulation.planning.snapshot import capture


def engine() -> SimulationEngine:
    """Use a small existing joint-allocation plant with external-only arrivals."""
    case = joint_batch()
    value = SimulationEngine(case.factory, 'rh_cpsat_linear', params={'task_rate': 0},
                             demand_source='external', initial_robots=case.initial_robots)
    value.scheduler.close()
    value.scheduler = LiveRollingHorizonScheduler('cpsat_linear', Settings(
        horizon=20, step=2, max_tasks=4, timeout=5, watchdog=7, native_actions=4))
    value.scheduler.name = value.mode
    value.env.run(until=.01)
    return value


def result(worker: LiveSolverProcess) -> Result:
    """Poll a bounded real child without changing the captured plant."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = worker.poll()
        if value is not None:
            return value
        time.sleep(.01)
    raise AssertionError('owned native response did not return')


def test_live_supervisor_reuses_child_and_reaps_connection() -> None:
    plant = engine()
    worker = LiveSolverProcess()
    try:
        snapshot = capture(plant, plant.scheduler.settings, 'first')
        worker.submit(snapshot, 'cpsat_linear')
        pid = worker.process.pid
        first = result(worker)
        assert first.report.status in {'OPTIMAL', 'FEASIBLE'}
        worker.submit(replace(snapshot, token='second'), 'cpsat_linear')
        second = result(worker)
        assert second.report.worker_reused and worker.process.pid == pid
    finally:
        worker.close()
        plant.close()
    assert worker.process is worker.connection is worker.rpc_thread is worker.pending is None


def test_live_hard_deadline_reaps_only_owned_child() -> None:
    plant = engine()
    worker = LiveSolverProcess()
    try:
        worker.submit(capture(plant, plant.scheduler.settings, 'timeout'), 'cpsat_linear')
        worker.clock = lambda: worker.started + 100
        started = time.monotonic()
        failed = worker.poll()
        assert failed.report.status == 'HARD_TIMEOUT'
        assert time.monotonic() - started < 3
        assert worker.process is worker.connection is worker.rpc_thread is worker.pending is None
    finally:
        worker.close()
        plant.close()


def test_live_memory_limit_is_not_a_successful_plan() -> None:
    plant = engine()
    worker = LiveSolverProcess()
    try:
        settings = replace(plant.scheduler.settings, memory_limit_mib=1)
        worker.submit(capture(plant, settings, 'rss'), 'cpsat_linear')
        failed = result(worker)
        assert failed.report.status == 'MEMORY_LIMIT'
        assert worker.process is None and worker.pending is None
    finally:
        worker.close()
        plant.close()


def test_settings_change_discards_pending_values_before_new_request() -> None:
    plant = engine()
    try:
        plant.scheduler.dispatch(plant)
        # No work means no unnecessary native request.
        assert plant.scheduler.worker.pending is None
        sid = plant.factory.stations[0].id
        task = plant.add_task('P', sid, 2, external_uid='old')
        plant.scheduler.dispatch(plant)
        assert plant.scheduler.worker.pending is not None
        plant.scheduler.notify_event('settings')
        assert plant.scheduler.worker.process is None
        assert plant.scheduler.pending_observation is None
        assert not plant.scheduler.records
        assert task in plant.pending_tasks
    finally:
        plant.close()


def test_live_publication_keeps_unrelated_arrival_in_real_queue() -> None:
    plant = engine()
    try:
        sid = plant.factory.stations[0].id
        old = plant.add_task('P', sid, 2, external_uid='old')
        plant.scheduler.dispatch(plant)
        new = plant.add_task('P', sid, 2, external_uid='new')
        # Owner dispatch is not allowed to consume the new job as part of the old solve.
        deadline = time.monotonic() + 10
        while not plant.scheduler.records and time.monotonic() < deadline:
            plant.env.run(until=plant.now + .05)
            time.sleep(.01)
        assert plant.scheduler.records and plant.scheduler.records[0]['accepted'], plant.scheduler.records
        assert new in plant.pending_tasks and old not in plant.pending_tasks
        assert old.status != 'queued' and new.status == 'queued'
    finally:
        plant.close()
