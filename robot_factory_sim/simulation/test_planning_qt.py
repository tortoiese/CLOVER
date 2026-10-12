"""Offscreen checks for dedicated solver-thread responsiveness and mode display."""

from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QApplication

from database.db_manager import DBManager
from simulation.planning.models import Snapshot, SolveReport, SolveResult
from simulation.sim_worker import _SimController
from simulation.test_planning import factory
from utils.event_types import ActionKind


@pytest.fixture(scope="module")
def app():
    """Keep one QApplication alive for all offscreen widget tests."""
    return QApplication.instance() or QApplication([])


@pytest.fixture
def controller(tmp_path):
    """Create a disposable DB through the project's DBManager API."""
    path = str(tmp_path / "factory.db")
    db = DBManager(path)
    db.initialize()
    db.save_factory_config(factory(), changed_by="test")
    db.close()
    value = _SimController(path)
    yield value
    value.shutdown()


def pump(app, seconds: float) -> None:
    """Deliver Qt events while timers and a background solver run."""
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        app.processEvents()
        time.sleep(0.002)


def test_slow_solve_does_not_block_state_emission(app, controller, monkeypatch) -> None:
    from simulation.planning import qt_worker

    class Slow:
        def solve(self, snapshot: Snapshot) -> SolveResult:
            time.sleep(0.4)
            return SolveResult(snapshot.token, SolveReport("NOT_SOLVED", "slow", 0.4))

    monkeypatch.setattr(qt_worker, "MilpBackend", Slow)
    states = []
    controller.state_ready.connect(states.append)
    controller.handle_command(ActionKind.SET_MODE, {"mode": "milp_rh"})
    controller.primary.update_param("background_enabled", 0.0)
    controller.primary.add_task("test", "S", 2)
    controller.start_loop()
    controller.handle_command(ActionKind.START, {})
    pump(app, 0.8)
    assert len(states) >= 4
    assert any(s.planning["milp_rh"]["pending"] for s in states)
    assert controller.primary.now > 0.3


def test_reset_drops_old_worker_results(app, controller, monkeypatch) -> None:
    from simulation.planning import qt_worker

    class Slow:
        def solve(self, snapshot: Snapshot) -> SolveResult:
            time.sleep(0.2)
            return SolveResult(snapshot.token, SolveReport("NOT_SOLVED", "old", 0.2))

    monkeypatch.setattr(qt_worker, "MilpBackend", Slow)
    old = controller._engines["milp_rh"]
    old.env.run(until=0.01)
    old.add_task("test", "S", 0)
    old.env.run(until=0.6)
    controller.handle_command(ActionKind.RESET, {})
    pump(app, 0.4)
    current = controller._engines["milp_rh"]
    assert current is not old
    assert current.scheduler.stats.last_report is None


def test_dashboard_supports_three_modes_and_planned_wait(app, controller) -> None:
    from gui.dashboard_panel import DashboardPanel

    states = []
    controller.state_ready.connect(states.append)
    controller.handle_command(ActionKind.SET_MODE, {"mode": "milp_rh"})
    panel = DashboardPanel(None)
    panel.update_state(states[-1])
    panel.show()
    for i in range(7):
        panel._tabs.setCurrentIndex(i)
        panel._refresh_charts()
    assert "MILP RH" in panel._kpi_title.text()
    panel.close()
    panel._timer.stop()


def test_console_accepts_milp_mode(app) -> None:
    import asyncio

    from agent.mock_agent import MockAgent

    response = asyncio.run(MockAgent().process_message("MILP 모드로 바꿔줘", {}))
    assert response.actions[0].params["mode"] == "milp_rh"
