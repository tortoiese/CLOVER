"""Owner-thread GUI adapter for the current headless planner and physical executor.

Live polling follows wall time while the Qt controller advances its plant. It is
not the staged-D1 research protocol. Headless D0/D1 use their existing controllers.
"""
from __future__ import annotations

import multiprocessing
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from queue import Empty, Queue
from threading import Thread
from typing import TYPE_CHECKING, Any

from simulation.decisions.admission import publish_rh
from simulation.decisions.observation import capture as observe
from simulation.planning.contracts import (
    PreparedInput,
    Report,
    Result,
    Settings,
    Snapshot,
)
from simulation.planning.controller import RollingHorizonScheduler
from simulation.planning.process import SolverProcess, _worker
from simulation.planning.replay import encode
from simulation.planning.snapshot import capture
from simulation.planning.validation import validate

LIVE_POLICIES = ("baseline", "optimized", "assignment_milp_legacy",
                 "rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_interval", "rh_cpsat_native")

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


class LiveSolverProcess(SolverProcess):
    """Nonblocking submit/poll around the same isolated native worker."""

    def __init__(self) -> None:
        super().__init__()
        self.pending: Snapshot | None = None
        self.responses: Queue[tuple[bool, object]] = Queue()
        self.started = 0.0
        self.backend = "milp"
        self.reused = False
        self.peak_rss = 0
        self.clock: Callable[[], float] = time.monotonic

    def close(self) -> None:
        """Reap child and IPC thread before invalidating this generation."""
        super().close()
        self.pending = None

    def submit(self, snapshot: Snapshot, backend: str) -> None:
        """Send only frozen values; never run a solver in the Qt owner thread."""
        if self.pending is not None:
            raise ValueError("one live request at a time")
        submitted_at = self.clock()
        self.reused = self.process is not None and self.process.is_alive()
        if not self.reused:
            self.close()
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            self.connection = parent
            self.process = context.Process(target=_worker, args=(child,), daemon=True)
            self.process.start()
            child.close()
        self.started, self.backend, self.pending = submitted_at, backend, snapshot
        self.peak_rss = 0
        self.responses = responses = Queue()
        connection = self.connection
        started = self.started

        def exchange() -> None:
            try:
                connection.send((snapshot, started, backend))
                responses.put((True, connection.recv()))
            except (EOFError, BrokenPipeError, OSError) as exc:
                responses.put((False, str(exc)))

        self.rpc_thread = Thread(target=exchange, daemon=True)
        self.rpc_thread.start()

    def _failure(self, status: str, message: str) -> Result:
        snapshot = self.pending
        elapsed = self.clock() - self.started
        self.close()
        return Result(PreparedInput(snapshot, (), ()), Report(
            status, backend=self.backend, total_seconds=elapsed,
            parent_elapsed_seconds=elapsed, worker_peak_rss_mib=self.peak_rss / 1024**2,
            message=message, timing_stage="live_parent", timing_complete=False))

    def poll(self) -> Result | None:
        """Observe availability, watchdog and RSS without waiting for a response."""
        if self.pending is None:
            return None
        try:
            import psutil
        except ImportError:
            return self._failure("UNAVAILABLE", "psutil is required for worker supervision")
        try:
            if self.clock() - self.started > self.pending.settings.watchdog:
                return self._failure("HARD_TIMEOUT", "live parent watchdog")
            if self.process is None or not self.process.is_alive():
                return self._failure("ERROR", "live worker exited")
            self.peak_rss = max(self.peak_rss, psutil.Process(self.process.pid).memory_info().rss)
            if self.peak_rss > self.pending.settings.memory_limit_mib * 1024**2:
                return self._failure("MEMORY_LIMIT", "live worker RSS limit")
            try:
                ok, result = self.responses.get_nowait()
            except Empty:
                return None
            if not ok or not isinstance(result, Result):
                return self._failure("ERROR", f"worker transport: {result}")
            if result.input.snapshot != self.pending:
                return self._failure("ERROR", "response observation differs from owned request")
            self.pending = None
            self.rpc_thread.join(timeout=.1)
            elapsed = self.clock() - self.started
            return replace(result, report=replace(result.report,
                parent_elapsed_seconds=elapsed, worker_reused=self.reused,
                worker_peak_rss_mib=self.peak_rss / 1024**2))
        except (EOFError, BrokenPipeError, OSError, psutil.Error) as exc:
            return self._failure("ERROR", str(exc))


class LiveRollingHorizonScheduler(RollingHorizonScheduler):
    """Use P4 math/admission unchanged, publishing only in the Qt owner thread."""

    def __init__(self, backend: str, settings: Settings) -> None:
        super().__init__(settings=settings, backend=backend)
        self.worker = LiveSolverProcess()
        self.pending_observation: Any = None
        self.last_result: Result | None = None
        self.last_execution = "WAITING"
        self.serial = 0
        self.closed = False

    def close(self) -> None:
        """Invalidate pending values and terminate owned native/IPC work."""
        self.closed = True
        self.pending_observation = None
        super().close()

    def notify_event(self, reason: str) -> None:
        """Discard outstanding values after an operator changes settings."""
        self.worker.close()
        self.pending_observation = None
        self.signature = None
        self.next_plan = 0
        self.last_execution = "INVALIDATED_" + reason.upper()

    def planning_state(self) -> dict[str, Any]:
        """Expose accepted/rejected prefix evidence without old reservation APIs."""
        return {"scope": "traffic", "timing_protocol": "live-owner-poll-v1",
                "pending": self.worker.pending is not None, "execution_status": self.last_execution,
                "report": asdict(self.last_result.report) if self.last_result else None,
                "settings": asdict(self.settings), "accepted": sum(r["accepted"] for r in self.records),
                "solves": self.serial, "fallbacks": sum(self.fallbacks.values()),
                "overruns": self.statuses["HARD_TIMEOUT"], "reservations": []}

    def dispatch(self, engine: SimulationEngine) -> None:
        """Poll then capture the next immutable request; never block native solve."""
        if self.closed:
            return
        result = self.worker.poll()
        if result is not None:
            self.last_result = result
            self.statuses[result.report.status] += 1
            reason, accepted = result.report.status, False
            if result.report.status in {"OPTIMAL", "FEASIBLE"}:
                errors = validate(result)
                if errors:
                    reason = "MODEL_INVALID: " + "; ".join(errors)
                elif len(result.deferred) == len(result.input.snapshot.jobs) and not any(
                        action.arc.kind == "charge" for action in result.actions):
                    reason = "ALL_DEFERRED_RECOVERY"
                else:
                    receipt = publish_rh(engine, result, self.pending_observation)
                    accepted, reason = receipt["accepted"], receipt["reason"]
            self.records.append({"time": engine.now, "result": encode(result),
                                 "accepted": accepted, "reason": reason})
            self.pending_observation = None
            self.last_execution = reason
            self.signature = None
            if not accepted:
                self._safe_dispatch(engine, reason)
        if self.worker.pending is not None:
            return
        idle = [r for r in engine.robot_list() if r.is_available()]
        if not idle:
            return
        if not engine.pending_tasks:
            self.fallback._dispatch(engine, idle)
            return
        signature = (tuple(t.external_uid for t in engine.pending_tasks),
                     tuple(r.robot_id for r in idle), engine.layout_version, tuple(sorted(engine.params.items())))
        if signature == self.signature and engine.now < self.next_plan:
            return
        self.signature = signature
        self.next_plan = engine.now + self.settings.interval
        token = f"live-{self.serial}"
        self.serial += 1
        self.pending_observation = observe(engine, engine.mode, token)
        snapshot = capture(engine, self.settings, token)
        try:
            self.worker.submit(snapshot, self.backend)
            self.last_execution = "COMPUTING"
        except (OSError, ValueError) as exc:
            self.worker.close()
            self.pending_observation = None
            self.last_execution = "PROCESS_ERROR"
            self._safe_dispatch(engine, str(exc))
