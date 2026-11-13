"""Real Qt integration for local comparison, policy activation and solver isolation."""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Generator
from dataclasses import asdict
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import config
from agent.rag.factory import create_agent
from gui.main_window import MainWindow
from simulation.comparison import run_comparison
from simulation.factory_service import FactoryService
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)
from utils.event_types import ActionKind


def factory() -> FactoryConfig:
    """One passable mm-contract corridor and a two-second station."""
    return FactoryConfig(chargers=[ChargingStationConfig("C", 0, 0, charge_rate=5)],
        stations=[WorkStationConfig("S", "S", 10, 0, avg_process_time=2, process_time_std=0)],
        robots=[RobotConfig("R", move_speed=1)],
        nodes=[PathNodeConfig(1, 0, 0, linked_station_id="C"),
               PathNodeConfig(2, 10, 0, linked_station_id="S")],
        edges=[PathEdgeConfig(1, 1, 2, width=1.3)])


@pytest.fixture(autouse=True)
def no_qt_callback_errors(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """A Qt slot exception must fail the acceptance test instead of only printing."""
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, traceback: errors.append(value))
    yield
    assert not errors, errors


def wait_until(predicate: Callable[[], bool], timeout: float = 8) -> None:
    """Pump real Qt events while waiting; the GUI must remain responsive."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(20)
    assert predicate()


@pytest.mark.parametrize("mode", ["rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_interval", "rh_cpsat_native"])
def test_gui_policy_activation_async_solver_and_clean_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             mode: str) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    service = FactoryService(tmp_path / "isolated.db")
    service.ensure_database()
    for order in service.list_orders(active_only=True):
        service.cancel_order(order.order_id)
    service.save_factory_config(factory())
    service.create_order("P", 1, required_stations=["S"])
    window = MainWindow(service, create_agent(run_comparison))
    window.show()
    beats = []
    timer = QTimer(window)
    timer.setInterval(10)
    timer.timeout.connect(lambda: beats.append(1))
    timer.start()
    try:
        wait_until(lambda: window._state is not None)
        assert config.DEFAULT_SCHEDULER_MODE == "optimized"
        assert set(window._state.active_policies) == {"baseline", "optimized"}
        from simulation.planning.contracts import Settings
        window._worker.send_command(ActionKind.SET_PLANNING, asdict(Settings(
            horizon=12, step=2, max_tasks=4, timeout=5, watchdog=7, native_actions=4)))
        window._mode.setCurrentIndex(window._mode.findData(mode))
        wait_until(lambda: window._state.mode == mode)
        assert window._state.sim_time == 0
        assert mode in window._state.active_policies
        window._worker.send_command(ActionKind.SET_SPEED, {"speed": 5})
        window._worker.send_command(ActionKind.START)
        wait_until(lambda: window._state.engine_kpis.get(mode, {}).get("completed_units", 0) >= 1, timeout=12)
        assert window._state.planning[mode]["accepted"] >= 1, window._state.planning[mode]
        assert "교통" in window._factory._mode.text()
        assert len(beats) > 20
        window._worker.send_command(ActionKind.RESET)
        wait_until(lambda: window._state.sim_time < 2)
    finally:
        timer.stop()
        window.close()
        wait_until(lambda: not window._worker.isRunning() and not window._agent_bridge.isRunning())
        app.processEvents()


def test_settings_and_default_factory_gui_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import numpy as np

    from gui.planning_settings import PlanningSettingsDialog
    from simulation.planning.contracts import Settings

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    service = FactoryService(tmp_path / "default.db")
    service.ensure_database()
    before = service.load_factory_config().to_dict()
    window = MainWindow(service, create_agent(run_comparison))
    window.show()
    try:
        wait_until(lambda: window._state is not None)
        dialog = PlanningSettingsDialog(asdict(Settings()), window)
        dialog.step.setCurrentIndex(dialog.step.findData(.5))
        dialog.fields["horizon"].setValue(20)
        assert dialog.values()["step"] == .5
        window._worker.send_command(ActionKind.SET_PLANNING, dialog.values())
        dialog.close()
        assert service.load_factory_config().to_dict() == before
        window._mode.setCurrentIndex(window._mode.findData("rh_milp_grid"))
        wait_until(lambda: window._state.mode == "rh_milp_grid")
        window._worker.send_command(ActionKind.START)
        worker_response = []
        response = []
        for index in range(30):
            count = len(window.gui_response_ms)
            window._worker.send_command(ActionKind.SET_SPEED, {"speed": 1 if index % 2 else 2})
            wait_until(lambda observed=count: len(window.gui_response_ms) > observed, timeout=2)
            worker_response.append(window._state.diagnostics["worker_command_ms"])
            response.append(window.gui_response_ms[-1])
        reset_started = time.monotonic()
        window._mode.setCurrentIndex(window._mode.findData("rh_cpsat_linear"))
        wait_until(lambda: window._state.mode == "rh_cpsat_linear")
        assert set(window._state.active_policies) == {"baseline", "optimized", "rh_cpsat_linear"}
        evidence = {"platform": app.platformName(), "factory_robots": len(before["robots"]),
                    "factory_stations": len(before["stations"]), "samples": response,
                    "command_to_state_p95_ms": float(np.quantile(response, .95)),
                    "worker_command_ms": worker_response,
                    "policy_activation_reset_ms": (time.monotonic() - reset_started) * 1000,
                    "target_150ms_met": float(np.quantile(response, .95)) < 150,
                    "scope": "30 live speed commands sent to GUI state application; activation reset reported separately"}
        if os.environ.get("CLOVER_QA_METRICS"):
            Path(os.environ["CLOVER_QA_METRICS"]).write_text(json.dumps(evidence, indent=2))
        assert len(response) == 30 and all(value < 2000 for value in response), evidence
    finally:
        window.close()
        wait_until(lambda: not window._worker.isRunning() and not window._agent_bridge.isRunning())
        app.processEvents()


def test_live_engines_pair_process_noise_by_job_identity(tmp_path: Path) -> None:
    from simulation.sim_worker import _SimController

    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "paired.db")
    service.ensure_database()
    controller = _SimController(str(service.db_path))
    try:
        observations = []
        for engine in controller._engines.values():
            assert engine.comparison_seed == config.RANDOM_SEED
            sid = next(iter(engine.stations))
            observations.append([engine.add_task("P", sid, 1, order_id="same-order").process_noise for _ in range(3)])
        assert observations[0] == observations[1]
        assert len(set(observations[0])) == 3
    finally:
        controller.shutdown()
        app.processEvents()


def test_policy_activation_reuses_observed_inputs_and_keeps_default(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from simulation.sim_worker import _SimController

    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "activation.db")
    service.ensure_database()
    controller = _SimController(str(service.db_path))
    factory_before = controller.primary.factory.to_dict()

    def unexpected_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("policy activation must not synchronously reload the DB")

    try:
        monkeypatch.setattr(controller._db, "load_factory_config", unexpected_read)
        monkeypatch.setattr(controller._db, "list_orders", unexpected_read)
        controller._dispatch_command(ActionKind.SET_MODE, {"mode": "rh_milp_grid"})
        assert controller.primary.factory.to_dict() == factory_before
        assert controller.primary.now == 0
        assert config.DEFAULT_SCHEDULER_MODE == "optimized"
        assert set(controller._engines) == {"baseline", "optimized", "rh_milp_grid"}
    finally:
        controller.shutdown()
        app.processEvents()


@pytest.mark.parametrize("quantity", [1, 3])
def test_policy_activation_preserves_flushed_order_progress(tmp_path: Path, quantity: int) -> None:
    """A new comparison must not restart work already persisted as completed."""
    from simulation.sim_worker import _SimController

    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "progress.db")
    service.ensure_database()
    for existing in service.list_orders(active_only=True):
        service.cancel_order(existing.order_id)
    service.save_factory_config(factory())
    order = service.create_order("Resume", quantity, required_stations=["S"])
    controller = _SimController(str(service.db_path))
    controller._timer.stop()
    try:
        for engine in controller._engines.values():
            engine.update_param("task_rate", 0)
            while engine.orders[order.order_id].completed == 0 and engine.now < 30:
                engine.env.run(until=engine.now + .5)
            assert engine.orders[order.order_id].completed == 1
        controller._flush_progress()
        persisted = next(row for row in service.list_orders() if row.order_id == order.order_id)
        assert persisted.completed == 1
        controller._dispatch_command(ActionKind.SET_MODE, {"mode": "rh_milp_grid"})
        for engine in controller._engines.values():
            if quantity == 1:
                assert order.order_id not in engine.orders
            else:
                assert engine.orders[order.order_id].completed == persisted.completed
        assert next(row for row in service.list_orders() if row.order_id == order.order_id).completed == 1
    finally:
        controller.shutdown()
        service.close()
        app.processEvents()


def test_live_demo_demand_is_enabled_and_paired_after_activation(tmp_path: Path) -> None:
    """Passing the noise seed must not silently disable GUI background demand."""
    from simulation.sim_worker import _SimController
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "demand.db")
    service.ensure_database()
    for order in service.list_orders(active_only=True):
        service.cancel_order(order.order_id)
    service.save_factory_config(factory())
    controller = _SimController(str(service.db_path))
    try:
        observed = []
        for model in controller._engines.values():
            assert model.demand_source == "stochastic"
            model.env.run(until=30)
            observed.append(tuple((t.external_uid, t.created_at, t.process_noise)
                                  for t in model.tasks.values()))
        assert observed[0] == observed[1] and observed[0]
    finally:
        controller.shutdown()
        service.close()
        app.processEvents()


def test_switch_flushes_progress_before_restarting_comparison(tmp_path: Path) -> None:
    """Work completed since the last timer flush must not reappear on activation."""
    from simulation.sim_worker import _SimController
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "unflushed.db")
    service.ensure_database()
    for order in service.list_orders(active_only=True):
        service.cancel_order(order.order_id)
    service.save_factory_config(factory())
    order = service.create_order("P", 3, required_stations=["S"])
    controller = _SimController(str(service.db_path))
    try:
        model = controller.primary
        model.update_param("task_rate", 0)
        while model.orders[order.order_id].completed == 0 and model.now < 30:
            model.env.run(until=model.now + .5)
        assert model.orders[order.order_id].completed == 1
        controller._dispatch_command(ActionKind.SET_MODE, {"mode": "rh_milp_grid"})
        assert next(r for r in service.list_orders() if r.order_id == order.order_id).completed == 1
        assert all(e.orders[order.order_id].completed == 1 for e in controller._engines.values())
        # Returning to the slower baseline must not undo persisted completed work.
        controller._dispatch_command(ActionKind.SET_MODE, {"mode": "baseline"})
        controller._flush_progress()
        assert next(r for r in service.list_orders() if r.order_id == order.order_id).completed == 1
    finally:
        controller.shutdown()
        service.close()
        app.processEvents()
