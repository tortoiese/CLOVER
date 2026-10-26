"""Spawned solver isolation with a hard watchdog and no Qt objects in children."""

from __future__ import annotations

import multiprocessing as mp
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from multiprocessing.connection import Connection

from simulation.planning.backend import Backend
from simulation.planning.contracts import Report, Result
from simulation.planning.snapshot import Request, prepare


def solve_request(request: Request, backend: str) -> Result:
    """Include detached route preparation in the total soft budget."""
    started = time.monotonic()
    try:
        snapshot = prepare(request)
        return Backend(backend).solve(snapshot, started)
    except Exception as exc:  # noqa: BLE001 — process boundary must return an explicit failure
        status = "UNSUPPORTED" if str(exc).startswith("UNSUPPORTED_") else "ERROR"
        return Result(request.snapshot, Report(backend, status, time.monotonic() - started,
                                               scope=request.snapshot.scope, message=str(exc)))


def _serve(connection: Connection, backend: str) -> None:
    try:
        unavailable = ""
        # Native library initialization belongs to the separate cold-start watchdog.
        try:
            if backend == "milp":
                from ortools.linear_solver import pywraplp
                pywraplp.Solver.CreateSolver("SCIP")
            else:
                from ortools.sat.python import cp_model
                cp_model.CpModel()
        except ImportError as exc:
            unavailable = f"Install requirements (ortools/SCIP): {exc}"
        connection.send("READY")
        while True:
            request = connection.recv()
            if request is None:
                break
            if unavailable:
                connection.send(Result(request.snapshot, Report(backend, "UNAVAILABLE", 0,
                                                                 scope=request.snapshot.scope, message=unavailable)))
            else:
                connection.send(solve_request(request, backend))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class SolverProcess:
    """One persistent subprocess; reset and shutdown terminate only owned work."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.process: mp.Process | None = None
        self.connection: Connection | None = None
        self.pending: Request | None = None
        self.started = 0.0
        self.submitted = 0.0
        self._cold = True
        self.peak_rss_mib = 0.0
        self._send_thread: threading.Thread | None = None
        self._cancel_send = threading.Event()
        self._send_error = ""
        self._messages: queue.Queue = queue.Queue()
        self._psutil = None

    def submit(self, request: Request) -> None:
        """Start one request, without building paths or importing a native solver here."""
        if self.pending is not None:
            raise RuntimeError("a solve is already in progress")
        child = None
        if self.process is None:
            context = mp.get_context("spawn")
            self.connection, child = context.Pipe()
            self.process = context.Process(target=_serve, args=(child, self.backend), daemon=True)
            self._cold = True
        self.pending = request
        self.started = time.monotonic()
        self.submitted = self.started
        self.peak_rss_mib = 0
        self._cancel_send = threading.Event()
        self._send_error = ""
        self._messages = queue.Queue()
        self._send_thread = threading.Thread(target=self._send_request,
                                            args=(request, self.process, self.connection, child, self._cancel_send,
                                                  self._messages),
                                            name="clover-planner-transport", daemon=True)
        self._send_thread.start()

    def _send_request(self, request: Request, process: mp.Process, connection: Connection,
                      child: Connection | None, cancelled: threading.Event, messages: queue.Queue) -> None:
        """Keep spawn and pipe backpressure off both Qt and simulation event loops."""
        try:
            import psutil
            self._psutil = psutil
            if child is not None and not cancelled.is_set():
                process.start()
            if not cancelled.is_set():
                connection.send(request)
                while not cancelled.is_set():
                    message = connection.recv()
                    messages.put(message)
                    if message != "READY":
                        break
        except ImportError:
            self._send_error = "UNAVAILABLE: Install requirements: psutil is required for RSS monitoring"
        except (OSError, EOFError, RuntimeError, ValueError) as exc:
            if not cancelled.is_set():
                self._send_error = str(exc)
        finally:
            if child is not None:
                child.close()
            if cancelled.is_set() and process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(.2)
                if process.is_alive():
                    process.kill()
                    process.join(.2)

    def poll(self) -> Result | None:
        """Return once, or kill a crashed/overdue solver and return an explicit failure."""
        if self.pending is None:
            return None
        request = self.pending
        if self._send_error:
            message = self._send_error
            self.close()
            status = "UNAVAILABLE" if message.startswith("UNAVAILABLE:") else "PROCESS_ERROR"
            return Result(request.snapshot, Report(self.backend, status, 0,
                                                   scope=request.snapshot.scope, message=message))
        if self._psutil is None and self._send_thread is not None:
            if time.monotonic() - self.submitted <= 10:
                return None
            self.close()
            return Result(request.snapshot, Report(self.backend, "HARD_TIMEOUT", 10,
                                                   scope=request.snapshot.scope, message="monitor initialization timeout"))
        try:
            psutil = self._psutil
            if psutil is None:
                import psutil
        except ImportError:
            self.close()
            return Result(request.snapshot, Report(self.backend, "UNAVAILABLE", 0, scope=request.snapshot.scope,
                                                   message="Install requirements: psutil is required for RSS monitoring"))
        try:
            if self.process.pid is None:
                if time.monotonic() - self.submitted <= max(10, request.snapshot.settings.watchdog):
                    return None
                self.close()
                return Result(request.snapshot, Report(self.backend, "HARD_TIMEOUT", 10,
                                                       scope=request.snapshot.scope, message="startup timeout"))
            rss = psutil.Process(self.process.pid).memory_info().rss / (1024 * 1024)
            self.peak_rss_mib = max(self.peak_rss_mib, rss)
            if rss > request.snapshot.settings.memory_limit_mib:
                elapsed = time.monotonic() - self.started
                self.close()
                return Result(request.snapshot, Report(self.backend, "MEMORY_LIMIT", elapsed,
                                                       scope=request.snapshot.scope,
                                                       message=f"RSS {rss:.1f} MiB exceeds configured limit"))
            try:
                result = self._messages.get_nowait()
            except queue.Empty:
                result = None
            if self._send_thread is None and self.connection.poll():
                result = self.connection.recv()
            if result is not None:
                if result == "READY":
                    self._cold = False
                    self.started = time.monotonic()
                    return None
                self.pending = None
                self._cold = False
                return replace(result, report=replace(result.report, peak_rss_mib=self.peak_rss_mib,
                                                       total_wall_seconds=time.monotonic() - self.submitted))
            limit = max(10.0, request.snapshot.settings.watchdog) if self._cold else request.snapshot.settings.watchdog
            if self.process.is_alive() and time.monotonic() - self.started <= limit:
                return None
            status = "HARD_TIMEOUT" if self.process.is_alive() else "PROCESS_ERROR"
        except (EOFError, OSError, psutil.Error):
            status = "PROCESS_ERROR"
        elapsed = time.monotonic() - self.started
        self.close()
        return Result(request.snapshot, Report(self.backend, status, elapsed, scope=request.snapshot.scope,
                                               message="solver process restarted"))

    def solve(self, request: Request, cancelled: Callable[[], bool] | None = None) -> Result:
        """Headless paired experiments wait for isolated work at a fixed sim-time."""
        self.submit(request)
        while True:
            if cancelled and cancelled():
                self.close()
                raise InterruptedError("solver request cancelled")
            result = self.poll()
            if result is not None:
                return result
            time.sleep(.005)

    def close(self) -> None:
        """Bound cancellation time and release pipes on reset, stop or application exit."""
        self._cancel_send.set()
        if self.process is not None and self.process.pid is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(.2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(.2)
        if self.connection is not None:
            self.connection.close()
        self.process = self.connection = self.pending = None
