"""좁은 대시보드 배치와 전체 실행 구간의 그래프 보존을 검증한다."""

from datetime import datetime

from PySide6.QtWidgets import QApplication, QScrollArea, QToolButton

import config
from database.seed_data import build_default_config
from gui import constants as c
from gui.dashboard_panel import DashboardPanel
from simulation.state import RobotSnapshot, SimState, TimelineEvent
from utils.event_types import RobotState, TimelineKind


def test_dashboard_stays_narrow_when_values_grow() -> None:
    """대시보드가 화면을 밀지 않으며 ROI 입력과 차트에 접근할 수 있다."""
    app = QApplication.instance() or QApplication([])
    panel = DashboardPanel()
    panel.setStyleSheet(config.QSS_PATH.read_text(encoding="utf-8"))
    panel.resize(390, 640)
    panel.show()
    try:
        roi = panel._roi_panel
        panel._tabs.setCurrentWidget(roi)
        app.processEvents()
        minimum_width = panel.minimumSizeHint().width()
        card = panel._cards["throughput"]
        card.set_values(7.7, 4.3, "라운드로빈")
        assert card._value.text() == "7.7"
        assert card._reference.text() == "라운드로빈 4.3 건/분"
        details = panel.findChild(QScrollArea, "KpiDetails")
        toggle = panel.findChild(QToolButton, "KpiDetailsToggle")
        assert not details.isVisible()
        toggle.click()
        app.processEvents()
        assert details.isVisible() and panel.width() == 390
        assert details.height() == c.KPI_DETAILS_HEIGHT
        assert details.verticalScrollBar().maximum() > 0
        toggle.click()
        app.processEvents()

        roi.update_kpis(1, 100, 4_000_000, 7_000_000)
        card.set_values(123_456_789.0, 0.00001, "긴 이름을 가진 비교 운영 정책")
        app.processEvents()

        assert panel.width() == 390
        assert panel.minimumSizeHint().width() == minimum_width
        assert "123456789.0" in card._value.toolTip()
        assert panel._tabs.count() == 9
        assert panel._tabs.tabText(0) == "지표"
        scroll = roi.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        assert scroll.verticalScrollBar().maximum() > 0
    finally:
        panel.close()


def test_live_plots_keep_initial_data_and_axis_origin_after_history_compaction() -> None:
    """오래 실행해도 0분이 화면에 남고 휠/드래그로 축이 이동하지 않는다."""
    app = QApplication.instance() or QApplication([])
    panel = DashboardPanel()
    panel.resize(550, 640)
    panel.show()
    factory = build_default_config()
    initial_event = TimelineEvent(0, TimelineKind.INFO, "시작")
    events = [initial_event]
    try:
        for step in range(c.HISTORY_MAX_POINTS + 40):
            current = step * c.HISTORY_SAMPLE_INTERVAL
            if step and step % 8 == 0:
                events.append(TimelineEvent(current, TimelineKind.INFO, f"event {step}"))
            state = SimState(current, datetime(2026, 1, 1), 0, 1, True, "baseline", 0, factory,
                             [RobotSnapshot("R-01", 0, 0, RobotState.IDLE, 100, 0)],
                             [], [], [], 0, 0, {"throughput": 2}, {"baseline": {"throughput": 2}},
                             {}, events=events[-40:])
            panel.update_state(state)
        app.processEvents()
        assert panel._history[0].minute == 0
        assert len(panel._history) <= c.HISTORY_MAX_POINTS
        assert panel._events["baseline"][0] is initial_event
        assert panel._gantt["R-01"][0].start == 0
        for index, plot in enumerate(panel._plots):
            panel._tabs.setCurrentWidget(plot)
            panel._refresh_charts()
            if index in {0, 1, 2, 3, 6}:
                assert plot.getViewBox().viewRange()[0][0] == 0
            assert plot.getViewBox().state["mouseEnabled"] == [False, False]
        panel._tabs.setCurrentWidget(panel._metrics)
        panel._refresh_charts()
        assert panel._metrics.item(0, 1).text() == "2.0"
        x, _ = panel._throughput_curves["baseline"].getData()
        assert x[0] == 0 and x[-1] > 30
    finally:
        panel.close()
