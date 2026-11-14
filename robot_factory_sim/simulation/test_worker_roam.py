"""GUI 워커의 층 순회(roam)는 RH 정책과 공존하지 않는다."""
from __future__ import annotations

from pathlib import Path

import config
from simulation.factory_service import FactoryService
from simulation.sim_worker import _SimController


def _engines(worker: _SimController) -> list:
    return list(worker._engines.values())  # noqa: SLF001 — 워커 내부 엔진 구성 검사


def test_roam_is_on_for_heuristics_and_off_when_an_rh_policy_is_active(tmp_path: Path) -> None:
    service = FactoryService(tmp_path / "roam.db")
    service.ensure_database()
    worker = _SimController(str(tmp_path / "roam.db"))
    try:
        assert all(e.roam for e in _engines(worker))
        worker._active_policies = [*config.SCHEDULER_MODES, "rh_cpsat_linear"]  # noqa: SLF001
        worker._build_engines()  # noqa: SLF001
        assert len(_engines(worker)) == len(config.SCHEDULER_MODES) + 1
        # 순회가 켜지면 RH 계획이 무효가 되고 에너지 비교도 불공정해지므로 모든 엔진에서 꺼야 한다.
        assert not any(e.roam for e in _engines(worker))
    finally:
        for engine in _engines(worker):
            engine.close()
