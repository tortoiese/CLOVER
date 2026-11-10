"""기존 공장 DB를 건드리지 않는 통행·병목 GUI 시연.

실행: python traffic_demo.py --width 1.3 --robots 6
통로 폭의 단위는 m이며, 창을 닫으면 임시 DB가 정리된다.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import config
from agent.mock_agent import MockAgent
from database.db_manager import DBManager
from database.seed_data import build_scenario_config
from gui.main_window import MainWindow
from simulation.factory_service import FactoryService
from utils.config_models import RobotConfig


def main(argv: list[str] | None = None) -> int:
    """임시 중앙복도형 공장에서 통행 규칙 시연을 시작한다.

    Args:
        argv: 테스트에서 주입할 CLI 인수. None이면 실제 CLI 사용.

    Returns:
        Qt 애플리케이션 종료 코드.
    """
    parser = argparse.ArgumentParser(description="통행·병목 GUI 시연 (기존 DB 변경 없음)")
    parser.add_argument("--width", type=float, default=1.3, help="중앙 통로 폭(m)")
    parser.add_argument("--robots", type=int, default=6, help="로봇 대수(1~20)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.width) or not 0 < args.width <= 50 or not 1 <= args.robots <= 20:
        parser.error("통로 폭은 0 초과 50m 이하, 로봇은 1~20대여야 합니다.")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(config.QSS_PATH.read_text(encoding="utf-8"))
    factory, _ = build_scenario_config("corridor", "S2", corridor_width=args.width)
    factory.robots = [RobotConfig(f"R-{number:02d}") for number in range(1, args.robots + 1)]
    factory.edges[0].is_bidirectional = False
    with tempfile.TemporaryDirectory(prefix="factory-traffic-") as directory:
        db_path = Path(directory) / "factory.db"
        database = DBManager(db_path)
        database.initialize()
        database.close()
        service = FactoryService(db_path)
        service.save_factory_config(factory, "traffic_demo")
        window = MainWindow(service, MockAgent())
        window.setWindowTitle("통행 규칙 시연 — 임시 공장")
        window._console.add_system_message(
            "통행 시연: 중앙 통로는 폭 제한·FIFO 진입, 좌측 조립 진입로는 일방통행입니다. "
            "노란 구간·로봇은 통행 대기이며 마우스를 올리면 원인·순서를 볼 수 있습니다.")
        window.show()

        def start_when_ready() -> None:
            if window._closing:
                return
            if window._state is None:
                QTimer.singleShot(50, start_when_ready)
                return
            window._factory.stage.select_floor(1)
            window._act_start.trigger()

        QTimer.singleShot(200, start_when_ready)
        try:
            return app.exec()
        finally:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
