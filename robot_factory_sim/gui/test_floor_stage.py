"""3D 건물 ↔ 층별 도면 전환, 축척(1 : N) 단계 조절, 층 히트 테스트 검증."""

from datetime import datetime

from PySide6.QtCore import QEventLoop, QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from database.seed_data import build_default_config
from gui import constants as c
from gui.factory_panel import FactoryPanel
from simulation.engine import SimulationEngine
from simulation.state import SimState


def _state(duration: float = 120.0) -> SimState:
    """엔진을 잠깐 돌려 SimWorker와 같은 모양의 상태를 만든다."""
    engine = SimulationEngine(build_default_config(), "baseline", epoch=datetime.now())
    engine.env.run(until=duration)
    return SimState(
        sim_time=engine.now, sim_datetime=engine.sim_datetime(), wall_elapsed=engine.now, speed=1, running=True,
        mode=engine.mode, layout_version=engine.layout_version, layout=engine.factory, robots=engine.robot_snapshots(),
        stations=engine.station_snapshots(), chargers=engine.charger_snapshots(), orders=engine.order_snapshots(),
        pending_tasks=len(engine.pending_tasks), avg_task_cycle=engine.metrics.avg_cycle_time(), kpis=engine.kpis(),
        engine_kpis={engine.mode: engine.kpis()}, params=dict(engine.params), events=list(engine.timeline),
        elevators=engine.elevator_snapshots(),
    )


def _wait(ms: int) -> None:
    """이벤트 루프를 ms만큼 돌려 애니메이션이 끝나게 한다."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _panel() -> FactoryPanel:
    """상태를 넣은 패널."""
    QApplication.instance() or QApplication([])
    panel = FactoryPanel()
    panel.resize(640, 820)
    panel.show()
    panel.update_state(_state())
    _wait(50)
    return panel


def test_overview_first_then_floor_click_shrinks_building_to_top_left() -> None:
    """처음엔 건물이 무대 전체를 쓰고, 층을 고르면 왼쪽 위로 줄어들며 도면이 열린다."""
    panel = _panel()
    stage = panel.stage
    assert not stage.detail_mode
    assert stage.building.geometry() == stage.rect()
    stage.select_floor(2)
    _wait(c.BUILDING_ANIMATION_MS + 250)
    mini = stage.building.geometry()
    assert stage.detail_mode and stage.current_floor == 2
    assert (mini.x(), mini.y()) == (0, 0)
    assert mini.width() < stage.width() / 2 and mini.height() < stage.height() / 2
    assert stage.plan.isVisible() and stage.plan.floor == 2
    assert stage.header.title.text().startswith("2F")
    stage.show_overview()
    _wait(c.BUILDING_ANIMATION_MS + 250)
    assert not stage.detail_mode and stage.building.geometry() == stage.rect() and not stage.plan.isVisible()
    panel.close()


def test_plan_shows_only_selected_floor_items() -> None:
    """도면에는 선택한 층의 스테이션/충전소만 보이고 엘리베이터는 모든 층에 보인다."""
    panel = _panel()
    plan = panel.stage.plan
    for floor, expected in ((1, {"WS-01", "WS-02"}), (2, {"WS-03", "WS-04"}), (3, {"WS-05"})):
        plan.set_floor(floor)
        assert {sid for sid, item in plan._stations.items() if item.isVisible()} == expected  # noqa: SLF001
        assert all(item.isVisible() for item in plan._elevators.values())  # noqa: SLF001
    panel.close()


def test_zoom_steps_snap_to_grid_friendly_ratios() -> None:
    """Ctrl+휠 단계는 SCALE_RATIOS 값만 지나가고, 맞춤으로 돌아올 수 있다."""
    panel = _panel()
    stage = panel.stage
    stage.select_floor(1)
    _wait(c.BUILDING_ANIMATION_MS + 250)
    plan = stage.plan
    plan.zoom_step(1)
    zoomed_in = round(plan.current_ratio())
    assert zoomed_in in c.SCALE_RATIOS
    plan.zoom_step(1)
    assert round(plan.current_ratio()) < zoomed_in
    for _ in range(12):
        plan.zoom_step(-1)
    assert round(plan.current_ratio()) == max(c.SCALE_RATIOS)
    assert "격자 1칸 = 5 m" in plan.cell_size_text()
    plan.fit_to_view()
    assert stage.header.ratio_label.text().endswith("(맞춤)")
    panel.close()


def test_clicking_a_slab_selects_that_floor() -> None:
    """각 층 바닥판 중심을 실제로 누르면 그 층의 floor_clicked가 나가고 드래그는 클릭으로 세지 않는다."""
    panel = _panel()
    building = panel.stage.building
    building.repaint()
    emitted: list[int] = []
    building.floor_clicked.connect(emitted.append)
    for floor in (3, 2, 1):
        center = building._hit[floor].boundingRect().center()  # noqa: SLF001
        assert building._floor_at(center) == floor  # noqa: SLF001
        QTest.mouseClick(building, Qt.MouseButton.LeftButton, pos=center.toPoint())
    assert emitted == [3, 2, 1]
    emitted.clear()
    start = building._hit[3].boundingRect().center().toPoint()  # noqa: SLF001
    QTest.mousePress(building, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(building, start + QPoint(40, 5))
    QTest.mouseRelease(building, Qt.MouseButton.LeftButton, pos=start + QPoint(40, 5))
    assert emitted == []
    panel.close()
