"""좁은 대시보드에서 수치가 갱신돼도 패널 폭이 유지되는지 검증한다."""

from PySide6.QtWidgets import QApplication, QScrollArea, QToolButton

import config
from gui.dashboard_panel import DashboardPanel


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
        assert details.verticalScrollBar().maximum() > 0
        toggle.click()
        app.processEvents()

        roi.update_kpis(1, 100, 4_000_000, 7_000_000)
        card.set_values(123_456_789.0, 0.00001, "긴 이름을 가진 비교 운영 정책")
        app.processEvents()

        assert panel.width() == 390
        assert panel.minimumSizeHint().width() == minimum_width
        assert "123456789.0" in card._value.toolTip()
        assert panel._tabs.count() == 8
        scroll = roi.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0
        assert scroll.verticalScrollBar().maximum() > 0
    finally:
        panel.close()
