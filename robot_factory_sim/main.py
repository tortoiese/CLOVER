"""Robot Factory Simulator 진입점.

실행: ``python main.py``
1. factory_config.db 확인 → 없으면 schema.sql + seed_data로 기본 구성 생성
2. DB 구성을 읽어 GUI 렌더링
3. [▶ 시작] → SimWorker + ConfigWatcher + ProductionEventWatcher 동작
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pyqtgraph as pg  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import config  # noqa: E402
from agent.mock_agent import MockAgent  # noqa: E402
from gui import constants as c  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from simulation.factory_service import FactoryService  # noqa: E402


def load_stylesheet() -> str:
    """theme.qss를 읽는다 (없으면 빈 문자열).

    Returns:
        QSS 문자열
    """
    try:
        return config.QSS_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> int:
    """애플리케이션 실행.

    Returns:
        종료 코드
    """
    app = QApplication(sys.argv)
    app.setApplicationName(c.APP_TITLE)
    app.setStyle("Fusion")
    app.setStyleSheet(load_stylesheet())
    pg.setConfigOptions(antialias=True, background=c.PANEL, foreground=c.TEXT)

    service = FactoryService(config.DB_PATH)
    seeded = service.ensure_database()
    window = MainWindow(service, MockAgent())
    if seeded:
        window.statusBar().showMessage("factory_config.db 생성 — 기본 공장 구성(스테이션 5, 충전소 2, 로봇 5)을 넣었습니다.", 8000)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
