"""Qt-only solver worker; the headless planner never imports this module."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal, Slot

from simulation.planning.milp_backend import MilpBackend
from simulation.planning.models import Snapshot


class PlannerWorker(QObject):
    """Receive frozen snapshots and return plans without touching live engines."""

    result_ready = Signal(object)

    @Slot(object)
    def solve(self, snapshot: Snapshot) -> None:
        """Compute on the dedicated planner QThread and emit a frozen result."""
        self.result_ready.emit(MilpBackend().solve(snapshot))
