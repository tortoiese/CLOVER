"""Persistent spawn worker with parent-enforced wall and resident-memory limits."""

from __future__ import annotations

import multiprocessing
import time
import uuid
from dataclasses import replace
from multiprocessing.connection import Connection
from queue import Empty, Queue
from threading import Thread

from simulation.planning.contracts import PreparedInput, Report, Result, Snapshot


def _exchange(connection: Connection, payload: object, responses: Queue[tuple[bool, object]]) -> None:
    """Return transport failures when watchdog disposal interrupts an exchange."""
    try:
        connection.send(payload)
        responses.put((True, connection.recv()))
    except (EOFError, OSError) as exc:
        responses.put((False, str(exc)))
    except TypeError as exc:
        # On POSIX, a blocked send can resume after close() clears its handle.
        # Keep genuine serialization/programming errors visible on open pipes.
        if not connection.closed:
            raise
        responses.put((False, f"connection closed during exchange: {exc}"))


def _worker(connection: Connection) -> None:
    ready_policy = None
    try:
        while True:
            payload = connection.recv()
            if payload is None:
                return
            if isinstance(payload, dict) and payload.get("rpc_version") == 1:
                from simulation.decisions.rpc import handle

                reply, ready_policy = handle(payload, ready_policy)
                connection.send(reply)
                continue
            from simulation.planning.backend import Backend
            from simulation.planning.snapshot import prepare

            snapshot, started, backend = payload
            received, cpu = time.perf_counter(), time.process_time()
            try:
                data = prepare(snapshot, deadline=started + snapshot.settings.timeout)
                prepared = time.perf_counter()
                result = Backend(backend).solve(data, started=started)
                result = replace(
                    result,
                    report=replace(
                        result.report,
                        dispatch_to_worker_seconds=received - started,
                        prepare_seconds=prepared - received,
                        worker_cpu_seconds=time.process_time() - cpu,
                        stage_seconds={
                            "worker_dispatch": received - started,
                            "route_prepare": prepared - received,
                            **result.report.stage_seconds,
                        },
                    ),
                )
            except Exception as exc:
                if isinstance(exc, TimeoutError):
                    status = "NO_INCUMBENT_TIMEOUT"
                elif isinstance(exc, ValueError):
                    status = (
                        "BUILD_LIMIT"
                        if any(code in str(exc) for code in ("BUILD_LIMIT", "ROUTE_LIMIT"))
                        else "MODEL_INVALID"
                    )
                else:
                    status = "ERROR"
                result = Result(
                    PreparedInput(snapshot, (), ()),
                    Report(
                        status,
                        backend=backend,
                        message=f"{type(exc).__name__}: {exc}",
                        total_seconds=time.perf_counter() - started,
                        dispatch_to_worker_seconds=received - started,
                        prepare_seconds=time.perf_counter() - received,
                        worker_cpu_seconds=time.process_time() - cpu,
                        timing_stage="route_prepare",
                        timing_complete=False,
                        stage_seconds={
                            "worker_dispatch": received - started,
                            "route_prepare": time.perf_counter() - received,
                        },
                    ),
                )
            connection.send(result)
    except (EOFError, BrokenPipeError):
        return
    finally:
        connection.close()


class SolverProcess:
    """Isolate native failures and dispose the entire worker after a hard limit."""

    def __init__(self) -> None:
        self.process: multiprocessing.Process | None = None
        self.connection: Connection | None = None
        self.rpc_thread: Thread | None = None
        self.generation = 0
        self.session = ""
        self.ready_for: str | None = None

    def close(self) -> None:
        """Stop and reap the owned child; no solver process survives engine disposal."""
        self.ready_for = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=0.5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=0.5)
            self.process.close()
            self.process = None
        if self.rpc_thread is not None:
            self.rpc_thread.join(timeout=0.5)
            self.rpc_thread = None

    def request(self, command: str, policy: str, arguments: dict, *, request_id: str, epoch: str,
                timeout: float, memory_limit_mib: float) -> dict:
        """Run an identity-checked D1 RPC with a parent-enforced total bound.

        Readiness and operational calls use the same owned worker and watchdog.
        A failed generation is fully reaped before another may be created.
        """
        started = time.perf_counter()
        startup = command == "ready"
        peak = 0
        reused = self.process is not None and self.process.is_alive()

        def failure(code: str, message: str) -> dict:
            identity = {"session": self.session, "generation": self.generation,
                        "request_id": request_id, "epoch": epoch, "policy": policy}
            self.close()
            return {**identity, "status": ("STARTUP_" if startup else "") + code, "message": message,
                    "elapsed_seconds": time.perf_counter() - started, "worker_reused": reused,
                    "peak_rss_mib": peak / 1024**2, "value": None}

        try:
            import psutil
        except ImportError:
            return failure("UNAVAILABLE", "psutil is required for memory supervision")
        try:
            if command != "ready" and (not reused or self.ready_for != policy):
                return failure("WORKER_CRASH" if not reused else "NOT_READY", "no matching prepared worker")
            if not reused:
                self.close()
                self.generation += 1
                self.session = uuid.uuid4().hex
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                self.connection = parent
                self.process = context.Process(target=_worker, args=(child,), daemon=True)
                self.process.start()
                child.close()
            identity = {"session": self.session, "generation": self.generation,
                        "request_id": request_id, "epoch": epoch, "policy": policy}
            payload = {"rpc_version": 1, **identity, "command": command, "arguments": arguments,
                       "started": started}
            responses: Queue[tuple[bool, object]] = Queue()
            connection = self.connection
            self.rpc_thread = Thread(target=_exchange, args=(connection, payload, responses), daemon=True)
            self.rpc_thread.start()
            monitor = psutil.Process(self.process.pid)
            while True:
                if time.perf_counter() - started > timeout:
                    return failure("TIMEOUT" if startup else "HARD_TIMEOUT", "parent RPC watchdog")
                if not self.process.is_alive():
                    return failure("WORKER_CRASH", "worker exited without matching response")
                peak = max(peak, monitor.memory_info().rss)
                if peak > memory_limit_mib * 1024**2:
                    return failure("MEMORY_LIMIT", "resident memory limit")
                try:
                    ok, value = responses.get(timeout=.005)
                except Empty:
                    continue
                if not ok or not isinstance(value, dict):
                    return failure("TRANSPORT_ERROR", str(value))
                if any(value.get(key) != expected for key, expected in identity.items()):
                    return failure("STALE_GENERATION", "response identity differs from owned request")
                self.rpc_thread.join(timeout=.1)
                if startup and value["status"] == "READY":
                    self.ready_for = policy
                elif value["status"] not in {"READY", "OK"}:
                    self.close()
                elapsed=time.perf_counter()-started
                returned=value.get("value")
                if isinstance(returned,dict) and isinstance(returned.get("plan"),Result):
                    plan=returned["plan"]
                    response=max(0,elapsed-plan.report.total_seconds)
                    returned["plan"]=replace(plan,report=replace(plan.report,
                        worker_reused=reused,worker_peak_rss_mib=peak/1024**2,
                        parent_elapsed_seconds=elapsed,response_seconds=response,
                        stage_seconds={**plan.report.stage_seconds,"worker_response":response}))
                return {**value, "elapsed_seconds": time.perf_counter() - started,
                        "peak_rss_mib": peak / 1024**2, "worker_reused": reused}
        except (EOFError, BrokenPipeError, OSError, psutil.Error) as exc:
            return failure("TRANSPORT_ERROR", str(exc))

    def solve(self, snapshot: Snapshot, started: float, backend: str = "milp") -> Result:
        """Return a complete result or a retained explicit hard failure."""
        reused, peak_rss = False, 0

        def failure(status: str, message: str) -> Result:
            self.close()
            return Result(
                PreparedInput(snapshot, (), ()),
                Report(
                    status,
                    backend=backend,
                    total_seconds=time.perf_counter() - started,
                    parent_elapsed_seconds=time.perf_counter() - started,
                    worker_reused=reused,
                    worker_peak_rss_mib=peak_rss / 1024**2,
                    timing_stage="parent_" + status.lower(),
                    timing_complete=False,
                    message=message,
                ),
            )

        try:
            import psutil
        except ImportError:
            return failure("UNAVAILABLE", "psutil is required for the parent memory watchdog")
        reused = self.process is not None and self.process.is_alive()
        if not reused:
            self.close()
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            self.connection = parent
            self.process = context.Process(target=_worker, args=(child,), daemon=True)
            self.process.start()
            child.close()
        try:
            responses: Queue[tuple[bool, object]] = Queue()
            connection = self.connection
            # Large IPC can block too. The owner must remain able to reap the
            # worker even if the child never reads or finishes a response.
            self.rpc_thread = Thread(target=_exchange, args=(connection, (snapshot, started, backend), responses),
                                     daemon=True)
            self.rpc_thread.start()
            monitor = psutil.Process(self.process.pid)
            while True:
                if time.perf_counter() - started > snapshot.settings.watchdog:
                    return failure("HARD_TIMEOUT", "total decision watchdog, including cold start")
                if not self.process.is_alive():
                    return failure("ERROR", "native worker exited without a result")
                rss = monitor.memory_info().rss
                peak_rss = max(peak_rss, rss)
                if rss > snapshot.settings.memory_limit_mib * 1024**2:
                    return failure("MEMORY_LIMIT", "resident memory limit")
                try:
                    ok, value = responses.get(timeout=0.005)
                    if not ok or not isinstance(value, Result):
                        return failure("ERROR", f"worker transport: {value}")
                    self.rpc_thread.join(timeout=0.1)
                    elapsed = time.perf_counter() - started
                    return replace(
                        value,
                        report=replace(
                            value.report,
                            worker_peak_rss_mib=peak_rss / 1024**2,
                            worker_reused=reused,
                            parent_elapsed_seconds=elapsed,
                            response_seconds=max(0, elapsed - value.report.total_seconds),
                            stage_seconds={
                                **value.report.stage_seconds,
                                "worker_response": max(0, elapsed - value.report.total_seconds),
                            },
                        ),
                    )
                except Empty:
                    pass
        except (EOFError, BrokenPipeError, OSError, psutil.Error) as exc:
            return failure("ERROR", f"worker transport: {exc}")
