"""통행 규칙 편집·저장, 실제 대기 시각화, 반복 갱신 비용을 검증한다."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from gui import factory_view
from gui.config_editor import ConfigEditorDialog, NodeItem
from gui.factory_view import FactoryView
from gui.widgets.robot_item import RobotItem
from simulation.engine import SimulationEngine
from simulation.factory_service import FactoryService
from simulation.state import RobotSnapshot, SimState, state_to_context
from simulation.test_traffic_rules import traffic_factory
from utils.config_models import PathNodeConfig


def _state(engine: SimulationEngine) -> SimState:
    return SimState(
        sim_time=engine.now, sim_datetime=datetime.now(), wall_elapsed=engine.now, speed=1, running=True,
        mode=engine.mode, layout_version=engine.layout_version, layout=engine.factory,
        robots=engine.robot_snapshots(), stations=engine.station_snapshots(), chargers=engine.charger_snapshots(),
        orders=[], pending_tasks=len(engine.pending_tasks), avg_task_cycle=0, kpis=engine.kpis(),
        engine_kpis={engine.mode: engine.kpis()}, params=dict(engine.params), traffic=engine.corridors.snapshots(),
    )


def test_editor_can_change_width_direction_and_intersection(tmp_path: Path) -> None:
    """GUI 입력이 기존 DB 컬럼으로 보존되며 로봇 크기를 기본값으로 잃지 않는다."""
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    factory = traffic_factory()
    factory.nodes.append(PathNodeConfig(4, 50, 50))
    factory.robots[0].width, factory.robots[0].length = 1.1, 1.4
    service.save_factory_config(factory, "test")
    dialog = ConfigEditorDialog(service)
    try:
        item = dialog._edge_items[0]
        dialog._selected = item
        dialog._populate_form(item)
        assert dialog._props.currentIndex() == 6
        dialog._edge_width.setValue(2.7)
        dialog._edge_two_way.setChecked(False)
        old_start, old_end = item.edge.from_node, item.edge.to_node
        dialog._reverse_edge()
        assert (item.edge.from_node, item.edge.to_node) == (old_end, old_start)
        assert not item.edge.is_bidirectional and item.edge.width == 2.7
        node = next(item for item in dialog._node_items.values() if not item.node.linked_station_id)
        assert isinstance(node, NodeItem)
        # 설비에 연결되지 않은 일반 노드만 교차로로 변경한다.
        dialog._selected = node
        dialog._populate_form(node)
        dialog._n_kind.setCurrentText("intersection")
        assert node.node.node_type == "intersection"
        collected = dialog._collect()
        assert collected.robots[0].width == 1.1 and collected.robots[0].length == 1.4
        service.save_factory_config(collected, "test")
        saved = service.load_factory_config()
        assert saved.edges[0].width == 2.7 and not saved.edges[0].is_bidirectional
        assert saved.robots[0].width == 1.1
        dialog._dirty = False
    finally:
        dialog._dirty = False
        dialog.close()
        app.processEvents()


def test_editor_rejects_invalid_robot_dimensions(tmp_path: Path) -> None:
    """음수 크기나 NaN을 저장하지 않는다."""
    QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    dialog = ConfigEditorDialog(service)
    try:
        dialog._robot_table.item(0, 6).setText("nan")
        with pytest.raises(ValueError):
            dialog._read_robot_table()
        dialog._robot_table.item(0, 6).setText("-1")
        with pytest.raises(ValueError):
            dialog._read_robot_table()
    finally:
        dialog._dirty = False
        dialog.close()


def test_live_queue_and_wait_reason_are_visible_and_serializable() -> None:
    """실제 SimPy 요청을 GUI에 표시하고 사라진 대기열도 반영한다."""
    app = QApplication.instance() or QApplication([])
    engine = SimulationEngine(traffic_factory(), "baseline", comparison_seed=1)
    for robot in engine.robots.values():
        robot.x, robot.y = 0, 100
    engine.add_task("A", "B", 2)
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    view = FactoryView()
    view.resize(640, 480)
    view.show()
    app.processEvents()
    try:
        state = _state(engine)
        view.update_state(state)
        view._update_traffic(force=True)
        item = view._traffic_items["edge:1"]
        assert item._label.text() == "대기 1"
        waiting = next(robot for robot in state.robots if robot.wait_reason)
        assert waiting.wait_reason in view._robots[waiting.id].toolTip()
        assert state_to_context(state)["traffic"][0]["waiting"] == 1
        view.set_traffic_visible(False)
        assert not item.isVisible()
        view.set_traffic_visible(True)
        assert item.isVisible()
        engine.env.run(until=60)
        view.update_state(_state(engine))
        view._update_traffic(force=True)
        assert item._label.text() == ""
    finally:
        view.close()


def test_same_routes_are_not_rebuilt_on_identical_snapshots(monkeypatch: Any) -> None:
    """같은 스냅샷 100회에서 경로 재생성이 증가하지 않고 오버레이는 제한 주기로 갱신된다."""
    app = QApplication.instance() or QApplication([])
    engine = SimulationEngine(traffic_factory(), "baseline", comparison_seed=1)
    engine.add_task("A", "B", 2)
    engine.env.run(until=1)
    state = _state(engine)
    state.robots[0] = replace(state.robots[0], route=[(0, 100), (100, 100)], route_floors=[1, 1])
    clock = [0.0]
    monkeypatch.setattr(factory_view, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    view = FactoryView()
    view.show()
    app.processEvents()
    try:
        view.update_state(state)
        before_routes, before_traffic = view.route_rebuilds, view.traffic_refreshes
        for _ in range(100):
            view.update_state(state)
        assert view.route_rebuilds == before_routes
        assert view.traffic_refreshes == before_traffic
        clock[0] = 0.21
        view.update_state(state)
        assert view.traffic_refreshes == before_traffic + 1
        view.hide()
        before_traffic = view.traffic_refreshes
        clock[0] += 1
        view._animate()
        assert view.traffic_refreshes == before_traffic
    finally:
        view.close()


def test_floor_switch_restores_robot_position_and_route() -> None:
    """숨긴 층의 스냅샷도 돌아왔을 때 최신 위치·경로로 보인다."""
    app = QApplication.instance() or QApplication([])
    factory = traffic_factory()
    factory.profile.floor_count = 2
    engine = SimulationEngine(factory, comparison_seed=1)
    state = _state(engine)
    state.robots = [replace(state.robots[0], x=20, y=100, route=[(20, 100), (100, 100)], route_floors=[1, 1])]
    view = FactoryView()
    view.show()
    app.processEvents()
    try:
        view.update_state(state)
        assert view._routes[state.robots[0].id].isVisible()
        view.set_floor(2)
        state.robots[0].x = 30
        state.robots[0].route[0] = (30, 100)
        view.update_state(state)
        view.set_floor(1)
        assert view._robots[state.robots[0].id].pos().x() == 30
        assert view._routes[state.robots[0].id].isVisible()
    finally:
        view.close()


def test_idle_robot_does_not_repaint_for_emergency_blink_phase() -> None:
    """일반 로봇은 다른 로봇의 긴급 깜빡임 위상 변화로 다시 그리지 않는다."""
    QApplication.instance() or QApplication([])
    item = RobotItem("R", 0)
    snap = RobotSnapshot("R", 0, 0, "idle", 100, 0)
    item.apply_snapshot(snap, False)
    item.apply_snapshot(snap, True)
    assert not item.blink_on


def test_editor_fits_scene_after_show(tmp_path: Path) -> None:
    """표시 전의 작은 viewport 크기로 고정된 축척을 사용하지 않는다."""
    app = QApplication.instance() or QApplication([])
    service = FactoryService(tmp_path / "factory.db")
    service.ensure_database()
    dialog = ConfigEditorDialog(service)
    try:
        dialog.show()
        app.processEvents()
        app.processEvents()
        assert dialog._view.transform().m11() > 0.1
    finally:
        dialog._dirty = False
        dialog.close()
