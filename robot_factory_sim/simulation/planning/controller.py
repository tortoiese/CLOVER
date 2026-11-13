"""Opt-in multi-backend RH controller with prefix execution and recorded replay."""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Generator
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from simulation.planning.contracts import Settings
from simulation.planning.delay import rebase
from simulation.planning.execution import commit
from simulation.planning.process import SolverProcess
from simulation.planning.replay import decode, encode
from simulation.planning.snapshot import capture
from simulation.planning.validation import validate
from simulation.scheduler import BaseScheduler, OptimizedScheduler
from simulation.travel import estimate_trip

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot
    from simulation.station import ChargingStation


class SafeFallback(OptimizedScheduler):
    """Use energy-screened idle dispatch; never preempt an executing RH prefix."""

    def _preempt_for_critical(self, engine: SimulationEngine) -> None:
        pass

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """Hold when no charger is reachable with the currently available energy."""
        choices = [
            c
            for c in engine.chargers.values()
            if c.active
            and estimate_trip(engine, robot, c.id).horizontal_m * robot.cfg.battery_drain_rate <= robot.live_battery()
        ]
        return min(choices, key=lambda c: estimate_trip(engine, robot, c.id).seconds, default=None)


class RollingHorizonScheduler(BaseScheduler):
    """Plan on arrivals/availability/layout changes and a bounded periodic trigger."""

    name = "rh_milp_grid"

    def __init__(
        self,
        settings: Settings | None = None,
        replay_records: list[dict[str, Any]] | None = None,
        backend: str = "milp",
    ) -> None:
        """Select a solver while retaining one controller and physical executor."""
        self.settings = settings or Settings(horizon=30, step=2, max_tasks=8)
        self.settings = replace(self.settings, formulation="native" if backend == "cpsat_native" else "grid")
        self.settings.validate()
        self.worker = SolverProcess()
        self.backend = backend
        self.fallback = SafeFallback()
        self.records: list[dict[str, Any]] = []
        self.replay_records = replay_records
        self.statuses: Counter[str] = Counter()
        self.fallbacks: Counter[str] = Counter()
        self.next_plan = 0.0
        self.signature: Any = None
        self.pending_return = False
        self.apply_processes: list[Any] = []
        self._clock = time.perf_counter

    def close(self) -> None:
        """Release the isolated native worker on engine disposal."""
        self.worker.close()
        for process in self.apply_processes:
            if process.is_alive:
                process._generator.close()

    def _safe_dispatch(self, engine: SimulationEngine, reason: str) -> None:
        self.fallbacks[reason] += 1
        engine.ledger.emit("planning_fallback", reason=reason)
        self.fallback._dispatch(engine, [r for r in engine.robot_list() if r.is_available()])

    def dispatch(self, engine: SimulationEngine) -> None:
        """Compute from one observation, applying D1 proposals at observation+L."""
        if self.pending_return:
            return
        idle = [r for r in engine.robot_list() if r.is_available()]
        if not idle:
            return
        if not engine.pending_tasks:
            self.fallback._dispatch(engine, idle)
            return
        signature = (
            tuple(t.external_uid for t in engine.pending_tasks),
            tuple(r.robot_id for r in idle),
            engine.layout_version,
            tuple(sorted(engine.params.items())),
        )
        if signature == self.signature and engine.now < self.next_plan:
            return
        self.signature = signature
        self.next_plan = engine.now + self.settings.interval
        token = f"rh-{len(self.records)}"
        started = self._clock()
        snapshot = capture(engine, self.settings, token)
        if self.replay_records is not None:
            if len(self.records) >= len(self.replay_records):
                raise ValueError("recorded planning decisions exhausted")
            recorded = self.replay_records[len(self.records)]
            result = decode(recorded["result"])
            if result.input.snapshot != snapshot:
                raise ValueError("recorded planning input differs from current observed plant")
        else:
            result = self.worker.solve(snapshot, started, self.backend)
        prevalidation = ()
        if result.report.status in {"OPTIMAL", "FEASIBLE"}:
            try:
                prevalidation = validate(result)
            except Exception as exc:
                prevalidation = (str(exc),)
        delay = (
            (recorded["latency_seconds"] if self.replay_records is not None else self._clock() - started)
            if self.settings.timing_mode == "D1"
            else 0.0
        )
        record = {
            "requested_at": engine.now,
            "time": None,
            "result": encode(result),
            "latency_seconds": delay,
            "accepted": False,
            "reason": "PENDING_AT_END",
            "prevalidation_errors": prevalidation,
            "applied_result": None,
        }
        self.records.append(record)
        self.statuses[result.report.status] += 1
        engine.ledger.emit(
            "planning_decision",
            token=token,
            report=asdict(result.report),
            candidates=len(snapshot.jobs),
            pending=snapshot.pending_count,
            excluded=len(snapshot.excluded),
            timing_mode=self.settings.timing_mode,
            latency_seconds=delay,
        )
        if self.settings.timing_mode == "D1":
            self.pending_return = True
            process = engine.env.process(
                self._apply_after(engine, result, record, recorded if self.replay_records is not None else None)
            )
            self.apply_processes.append(process)
        else:
            self._admit(engine, result, record, recorded if self.replay_records is not None else None)

    def _apply_after(
        self, engine: SimulationEngine, result: Any, record: dict[str, Any], recorded: dict[str, Any] | None
    ) -> Generator[Any, Any, None]:
        yield engine.env.timeout(record["latency_seconds"])
        self.pending_return = False
        self.signature = None
        self._admit(engine, result, record, recorded)

    def _admit(
        self, engine: SimulationEngine, result: Any, record: dict[str, Any], recorded: dict[str, Any] | None
    ) -> None:
        started = self._clock()
        applied = result
        if result.report.status not in ("OPTIMAL", "FEASIBLE"):
            accepted, reason = False, result.report.status
        elif record["prevalidation_errors"]:
            accepted, reason = False, "MODEL_INVALID: " + "; ".join(record["prevalidation_errors"])
        elif len(result.deferred) == len(result.input.snapshot.jobs) and not any(
            a.arc.kind == "charge" for a in result.actions
        ):
            accepted, reason = False, "ALL_DEFERRED_RECOVERY"
        else:
            try:
                if self.settings.timing_mode == "D1":
                    applied, reason = rebase(engine, result)
                    if applied is None:
                        accepted = False
                    else:
                        accepted, reason = commit(engine, applied)
                else:
                    accepted, reason = commit(engine, result)
            except Exception as exc:
                accepted, reason = False, "MODEL_INVALID: " + str(exc)
        record.update(
            time=engine.now,
            accepted=accepted,
            reason=reason,
            applied_result=encode(applied) if accepted else None,
            apply_wall_seconds=(recorded["apply_wall_seconds"] if recorded is not None else self._clock() - started),
        )
        if recorded is not None and (record["accepted"], record["reason"], record["time"]) != (
            recorded["accepted"],
            recorded["reason"],
            recorded["time"],
        ):
            raise ValueError("recorded planning admission differs")
        engine.ledger.emit("planning_admission", token=result.input.snapshot.token, accepted=accepted, reason=reason)
        if not accepted:
            self._safe_dispatch(engine, reason)

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        self.dispatch(engine)

    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """Let the model own pending-work charging; preserve idle maintenance."""
        return not engine.pending_tasks and self.fallback.should_charge(robot, engine)

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """Use the common recovery/idle charging target."""
        return self.fallback.charge_target(robot, engine)

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """Choose a charger reachable with current energy for recovery actions."""
        return self.fallback.choose_charger(robot, engine)
