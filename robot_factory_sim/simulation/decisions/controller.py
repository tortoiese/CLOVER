"""One owner-thread staged D1 path for policy, charge and fallback decisions."""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable, Generator
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from simulation.decisions import admission
from simulation.decisions.adapters import propose
from simulation.decisions.contracts import (
    DecisionSettings,
    Proposal,
    decode,
    digest,
    encode,
)
from simulation.decisions.observation import capture
from simulation.decisions.timing import DelayLedger
from simulation.planning.contracts import ALL_RH_BACKENDS, Settings
from simulation.planning.process import SolverProcess
from simulation.planning.replay import decode as decode_rh
from simulation.planning.replay import encode as encode_rh
from simulation.scheduler import BaseScheduler

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine
    from simulation.robot import Robot
    from simulation.station import ChargingStation


class DecisionScheduler(BaseScheduler):
    """Preserve selectors while delaying every proposed state change.

    Args:
        engine: Sole owner-thread plant for observations and atomic publication.
        settings: Explicit staged-D1 timing and lifecycle settings.
        math_settings: Existing RH mathematical settings, unmodified in D0.
        replay_records: Original values and phase durations, never native reruns.
    """

    def __init__(
        self,
        engine: SimulationEngine,
        settings: dict[str, Any],
        math_settings: dict[str, Any] | None = None,
        replay_records: list[dict[str, Any]] | None = None,
    ) -> None:
        self.engine, self.name = engine, engine.mode
        self.settings = DecisionSettings(**settings)
        self.settings.validate()
        if math_settings and math_settings.get("timing_mode", "D0") != "D0":
            raise ValueError("RH prototype D1 conflicts with staged decision timing")
        self.math = Settings(
            **{
                **(math_settings or {}),
                "formulation": "native" if self.name == "rh_cpsat_native" else "grid",
            }
        )
        self.math.validate()
        self.worker = SolverProcess()
        self.records: list[dict[str, Any]] = []
        self.lifecycle: list[dict[str, Any]] = []
        self.replay_records = replay_records
        self.policy_state = "{}"
        self.ready = self.started = self.closed = self.busy = False
        self.restarts = 0
        self.coverage_ok = True
        self.pending_dispatch = False
        self.queued_at: float | None = None
        self.pending_charge: dict[str, Any] = {}
        self.pending_target: dict[str, Any] = {}
        self.approved_charge: dict[str, tuple[str, float]] = {}
        self.processes: list[Any] = []
        self.statuses: Counter[str] = Counter()
        self.fallbacks: Counter[str] = Counter()
        self.next_plan = 0.0
        self.signature: Any = None
        self.clock = time.monotonic
        if self.settings.startup_mode == "eager":
            self.pending_dispatch = True
            self.queued_at = engine.now
            self._kick()

    def close(self) -> None:
        """Invalidate pending publications and reap every owned child and coroutine."""
        self.closed = True
        self.worker.close()
        for process in self.processes:
            if process.is_alive:
                process._generator.close()

    def dispatch(self, engine: SimulationEngine) -> None:
        """Coalesce job/availability triggers without reserving physical resources."""
        idle = tuple(r.robot_id for r in engine.robot_list() if r.is_available())
        if idle and engine.pending_tasks:
            signature = (
                tuple(t.external_uid for t in engine.pending_tasks),
                idle,
                engine.layout_version,
                tuple(sorted(engine.params.items())),
            )
            if self.name not in ALL_RH_BACKENDS or signature != self.signature or engine.now >= self.next_plan:
                self.pending_dispatch = True
                self._mark_trigger()
                self.signature = signature
                self.next_plan = engine.now + self.math.interval
        self._kick()

    def _dispatch(self, engine: SimulationEngine, idle: list[Robot]) -> None:
        self.dispatch(engine)

    def _kick(self) -> None:
        if self.closed or self.busy or not (self.pending_dispatch or self.pending_charge or self.pending_target):
            return
        self.busy = True
        self.processes = [process for process in self.processes if process.is_alive]
        self.processes.append(self.engine.env.process(self._decision_process()))

    def _mark_trigger(self) -> None:
        if self.queued_at is None:
            self.queued_at = self.engine.now

    def idle_charge_process(self, robot: Robot) -> Generator[Any, Any, bool]:
        """Wait for an approved charging choice instead of calling live policy hooks."""
        event = self.engine.env.event()
        self.pending_charge[robot.robot_id] = event
        self._mark_trigger()
        self._kick()
        return bool((yield event))

    def target_process(self, robot: Robot) -> Generator[Any, Any, float]:
        """Charge no further than the last approved target while a new decision waits."""
        event = self.engine.env.event()
        self.pending_target[robot.robot_id] = event
        self._mark_trigger()
        self._kick()
        yield event
        return self.charge_target(robot, self.engine)

    def charge_finished(self, robot: Robot) -> None:
        """Expire a completed session's destination/goal before the next idle query."""
        self.approved_charge.pop(robot.robot_id, None)

    def should_charge(self, robot: Robot, engine: SimulationEngine) -> bool:
        """Only execute already approved intents; do not make a free new choice."""
        return robot.robot_id in self.approved_charge and robot.current_task is None

    def choose_charger(self, robot: Robot, engine: SimulationEngine) -> ChargingStation | None:
        """Read the approved destination without running a selector."""
        value = self.approved_charge.get(robot.robot_id)
        return engine.chargers.get(value[0]) if value else None

    def charge_target(self, robot: Robot, engine: SimulationEngine) -> float:
        """Read the last approved charge goal; no approval means no extra charge."""
        return self.approved_charge.get(robot.robot_id, ("", robot.battery_pct))[1]

    def _notify(self, charges: dict[str, Any], targets: dict[str, Any], allow_charge: bool) -> None:
        for rid, event in charges.items():
            robot = self.engine.robots.get(rid)
            if not event.triggered:
                event.succeed(
                    bool(
                        allow_charge
                        and robot is not None
                        and robot.current_task is None
                        and rid in self.approved_charge
                    )
                )
        for event in targets.values():
            if not event.triggered:
                event.succeed(None)

    def _startup_process(
        self, ledger: DelayLedger, row: dict[str, Any], original: dict[str, Any] | None
    ) -> Generator[Any, Any, bool]:
        if self.ready and (
            (original is not None and "startup" not in original)
            or (original is None and self.worker.process is not None and self.worker.process.is_alive())
        ):
            return True
        self.ready = False
        if self.started:
            if self.restarts >= self.settings.max_worker_restarts:
                row["startup"] = {"status": "DISABLED_FOR_RUN"}
                return False
            backoff = self.settings.restart_backoff_seconds[self.restarts]
            self.restarts += 1
            row["restart_backoff_sim_seconds"] = backoff
            yield self.engine.env.timeout(backoff)
        self.started = True
        at = self.engine.now

        def start() -> dict[str, Any]:
            if original is not None:
                if "startup" not in original:
                    raise ValueError("missing startup replay receipt")
                return original["startup"]
            reply = self.worker.request(
                "ready",
                self.name,
                {},
                request_id=row["token"] + "-ready",
                epoch=self.engine.execution_epoch,
                timeout=self.settings.startup_timeout_seconds,
                memory_limit_mib=self.settings.memory_limit_mib,
            )
            row["startup"] = reply
            return reply

        reply = yield from ledger.call_process("startup", start)
        row["startup"] = reply
        self.ready = reply["status"] == "READY"
        self.lifecycle.append(
            {
                "token": row["token"],
                "requested_at": at,
                "returned_at": self.engine.now,
                "reply": reply,
                "restart": self.restarts,
                "schema": "clover-worker-lifecycle-v1",
                "state": "READY" if self.ready else "UNAVAILABLE",
            }
        )
        return self.ready

    def _admit_process(
        self,
        ledger: DelayLedger,
        row: dict[str, Any],
        prepare: Callable[[], tuple[bool, str]],
        publish: Callable[[], dict[str, Any]],
    ) -> Generator[Any, Any, dict[str, Any]]:
        """Bound fresh admission attempts; partial application is never repeated."""
        for attempt in range(self.settings.max_admission_attempts):
            row["admission_attempts"] = attempt + 1
            valid, reason = yield from ledger.call_process("admission_prepare", prepare)
            if not valid:
                return {"accepted": False, "reason": reason, "applied": []}
            receipt = yield from ledger.publish_process(publish)
            if receipt["accepted"] or not ledger.coverage_ok or not receipt["reason"].startswith("STALE"):
                return receipt
        return receipt

    def _decision_process(self) -> Generator[Any, Any, None]:
        engine = self.engine
        index = len(self.records)
        original = (
            self.replay_records[index] if self.replay_records is not None and index < len(self.replay_records) else None
        )
        if self.replay_records is not None and original is None:
            raise ValueError("decision replay records exhausted")
        row: dict[str, Any] = {
            "schema": "clover-decision-record-v1",
            "token": f"decision-{index}",
            "triggered_at": engine.now if self.queued_at is None else self.queued_at,
            "started_at": engine.now,
            "queue_sim_wait_seconds": engine.now - (engine.now if self.queued_at is None else self.queued_at),
            "observed_at": None,
            "time": None,
            "reason": "PENDING_AT_END",
            "accepted": False,
            "proposal": None,
            "rh_result": None,
            "receipt": None,
            "phases": [],
            "coverage_ok": True,
            "fallback": None,
        }
        if original is not None and (row["token"], row["triggered_at"]) != (
            original["token"],
            original["triggered_at"],
        ):
            raise ValueError("decision replay trigger identity differs")
        self.records.append(row)
        self.queued_at = None
        ledger = DelayLedger(
            engine.env,
            self.settings.publication_allowance_seconds,
            self.clock,
            original["phases"] if original is not None else None,
        )
        row["phases"] = ledger.records
        row["pending_phase"] = ledger.pending
        charges, targets = self.pending_charge, self.pending_target
        dispatch = self.pending_dispatch
        self.pending_charge, self.pending_target, self.pending_dispatch = {}, {}, False
        finished = False
        try:
            ready = yield from self._startup_process(ledger, row, original)
            spent = engine.now - row["triggered_at"]
            if not ready or spent >= self.settings.end_to_end_soft_deadline_seconds:
                row["reason"] = "BACKEND_NOT_READY" if not ready else "DEADLINE_BEFORE_OBSERVATION"
            else:

                def read_observations() -> tuple[Any, Any]:
                    observation = capture(
                        engine,
                        self.name,
                        row["token"],
                        self.policy_state,
                        deadline=time.monotonic() + self.settings.end_to_end_soft_deadline_seconds - spent,
                    )
                    snapshot = None
                    if self.name in ALL_RH_BACKENDS and dispatch and engine.pending_tasks:
                        from simulation.planning.snapshot import capture as capture_rh

                        snapshot = capture_rh(engine, self.math, row["token"])
                    return observation, snapshot

                observation, snapshot = yield from ledger.call_process("capture", read_observations)
                row["observed_at"] = observation.now
                if original is not None and original["observation_hash"] != observation.hash:
                    raise ValueError("decision replay observation differs from current plant")
                row["observation_hash"] = observation.hash
                remaining = self.settings.end_to_end_soft_deadline_seconds - (engine.now - row["triggered_at"])
                if remaining <= 0:
                    row["reason"] = "DEADLINE_AFTER_CAPTURE"
                else:
                    is_rh = snapshot is not None
                    options = {
                        "dispatch": dispatch and self.name not in ALL_RH_BACKENDS,
                        "charge_robots": tuple(charges),
                        "target_robots": tuple(targets),
                    }
                    payload = {"observation": observation, "options": options}
                    if is_rh:
                        data = json.loads(observation.payload)
                        forced = tuple(r["robot_id"] for r in data["robots"] if r["forced_charge"])
                        payload = {
                            "snapshot": snapshot,
                            "observation": replace(observation, policy="safe_fallback"),
                            "options": {
                                "dispatch": False,
                                "charge_robots": forced,
                                "target_robots": tuple(targets),
                            },
                        }
                    elif self.name in ALL_RH_BACKENDS:
                        payload["observation"] = replace(observation, policy="safe_fallback")

                    def compute() -> dict[str, Any]:
                        if original is not None:
                            saved = dict(original["compute"])
                            if saved["status"] == "OK":
                                saved["value"] = (
                                    {
                                        "plan": decode_rh(saved["value"]["plan"]),
                                        "auxiliary": decode(saved["value"]["auxiliary"]),
                                    }
                                    if is_rh
                                    else decode(saved["value"])
                                )
                                if is_rh:
                                    plan = saved["value"]["plan"]
                                    expected = replace(
                                        payload["snapshot"],
                                        settings=replace(
                                            payload["snapshot"].settings, timeout=plan.input.snapshot.settings.timeout
                                        ),
                                    )
                                    if expected != plan.input.snapshot:
                                        raise ValueError("recorded RH computation input differs")
                                elif saved["value"].observation != payload["observation"]:
                                    raise ValueError("recorded selector computation input differs")
                            return saved
                        remaining_now = self.settings.end_to_end_soft_deadline_seconds - (
                            engine.now - row["triggered_at"]
                        )
                        if is_rh:
                            snap = payload["snapshot"]
                            payload["snapshot"] = replace(
                                snap,
                                settings=replace(
                                    snap.settings, timeout=min(snap.settings.timeout, max(0.000001, remaining_now))
                                ),
                            )
                        saved = self.worker.request(
                            "rh" if is_rh else "propose",
                            self.name,
                            payload,
                            request_id=row["token"],
                            epoch=engine.execution_epoch,
                            timeout=min(
                                self.settings.request_watchdog_seconds,
                                max(0.001, remaining_now),
                            ),
                            memory_limit_mib=self.settings.memory_limit_mib,
                        )
                        row["compute"] = {key: value for key, value in saved.items() if key != "value"}
                        returned = saved.get("value")
                        row["compute"]["value"] = (
                            (
                                {"plan": encode_rh(returned["plan"]), "auxiliary": encode(returned["auxiliary"])}
                                if is_rh
                                else encode(returned)
                            )
                            if returned is not None
                            else None
                        )
                        return saved

                    reply = yield from ledger.call_process("compute", compute)
                    self.lifecycle.append(
                        {
                            "schema": "clover-worker-lifecycle-v1",
                            "token": row["token"],
                            "requested_at": row["phases"][-1]["at"],
                            "returned_at": engine.now,
                            "state": "READY" if reply["status"] == "OK" else "REAPED",
                            "reply": {k: v for k, v in reply.items() if k != "value"},
                        }
                    )
                    row["compute"] = {key: value for key, value in reply.items() if key != "value"}
                    value = reply.get("value")
                    row["compute"]["value"] = (
                        (
                            {
                                "plan": encode_rh(value["plan"]),
                                "auxiliary": encode(value["auxiliary"]),
                            }
                            if is_rh
                            else encode(value)
                        )
                        if value is not None
                        else None
                    )
                    if reply["status"] != "OK":
                        row["reason"] = reply["status"]
                        self.ready = False
                    elif engine.now - row["triggered_at"] >= self.settings.end_to_end_soft_deadline_seconds:
                        row["reason"] = "END_TO_END_SOFT_DEADLINE"
                    elif is_rh:
                        auxiliary = value["auxiliary"]
                        value = value["plan"]
                        row["rh_result"] = encode_rh(value)
                        if value.report.status not in {"OPTIMAL", "FEASIBLE"}:
                            row["reason"] = value.report.status
                        elif len(value.deferred) == len(value.input.snapshot.jobs) and not any(
                            a.arc.kind == "charge" for a in value.actions
                        ):
                            row["reason"] = "ALL_DEFERRED_RECOVERY"
                        else:

                            def prepare_rh() -> tuple[bool, str]:
                                staged, reason = admission.revalidate_rh(engine, value, observation)
                                return staged is not None, reason

                            def publish_rh() -> dict[str, Any]:
                                receipt = admission.publish_rh(engine, value, observation)
                                if receipt["accepted"]:
                                    auxiliary_receipt = admission.publish(engine, auxiliary, self.approved_charge)
                                    receipt["auxiliary"] = auxiliary_receipt
                                    receipt["accepted"] &= auxiliary_receipt["accepted"]
                                    if not receipt["accepted"]:
                                        receipt["reason"] = "PARTIAL_AUXILIARY: " + auxiliary_receipt["reason"]
                                    self._notify(charges, targets, auxiliary_receipt["accepted"])
                                return receipt

                            receipt = yield from self._admit_process(ledger, row, prepare_rh, publish_rh)
                            row.update(accepted=receipt["accepted"], reason=receipt["reason"], receipt=receipt)
                            finished = receipt["accepted"]
                    else:
                        row["proposal"] = encode(value)

                        def prepare() -> tuple[bool, str]:
                            errors = admission.check(engine, value)
                            return not errors, ";".join(errors)

                        def publish() -> dict[str, Any]:
                            receipt = admission.publish(engine, value, self.approved_charge)
                            if receipt["accepted"]:
                                self.policy_state = value.state
                                self._notify(charges, targets, True)
                            return receipt

                        receipt = yield from self._admit_process(ledger, row, prepare, publish)
                        row.update(accepted=receipt["accepted"], reason=receipt["reason"], receipt=receipt)
                        finished = receipt["accepted"]
            if not finished:
                yield from self._fallback_process(ledger, row, charges, targets, original)
            row["time"] = engine.now
            row["coverage_ok"] = ledger.coverage_ok
            self.coverage_ok &= ledger.coverage_ok
            if not ledger.coverage_ok:
                row["reason"] = "TIMING_COVERAGE_VIOLATION"
            row["soft_deadline_exceeded"] = (
                engine.now - row["triggered_at"] > self.settings.end_to_end_soft_deadline_seconds
            )
            ledger.verify_consumed()
            if original is not None and any(
                digest(row[k]) != digest(original[k]) for k in ("time", "reason", "accepted", "receipt", "coverage_ok")
            ):
                raise ValueError("decision replay admission differs")
            self.statuses[row["reason"]] += 1
        except Exception as exc:
            if original is not None:
                raise
            row.update(time=engine.now, reason="DECISION_ERROR: " + str(exc), accepted=False)

            def hold() -> None:
                self._notify(charges, targets, False)

            yield from ledger.publish_process(hold)
            self.coverage_ok &= ledger.coverage_ok
            row["coverage_ok"] = ledger.coverage_ok
            row["time"] = engine.now
        finally:
            self.busy = False
            self._kick()

    def _fallback_process(
        self,
        ledger: DelayLedger,
        row: dict[str, Any],
        charges: dict[str, Any],
        targets: dict[str, Any],
        original: dict[str, Any] | None,
    ) -> Generator[Any, Any, None]:
        self.fallbacks[row["reason"]] += 1
        started = self.clock()
        fallback: dict[str, Any] = {
            "cause": row["reason"],
            "proposal": None,
            "receipt": None,
        }
        row["fallback"] = fallback
        try:

            def choose() -> Proposal:
                if original is not None:
                    value = original["fallback"]["proposal"]
                    if value is None:
                        raise TimeoutError("recorded fallback exhausted budget")
                    restored = decode(value)
                    current = capture(self.engine, "safe_fallback", row["token"] + "-fallback")
                    if restored.observation != current:
                        raise ValueError("fallback replay observation differs")
                    return restored
                observation = capture(
                    self.engine,
                    "safe_fallback",
                    row["token"] + "-fallback",
                    deadline=started + self.settings.fallback_budget_seconds,
                )
                value = propose(
                    observation,
                    charge_robots=tuple(charges),
                    target_robots=tuple(targets),
                )
                if self.clock() - started > self.settings.fallback_budget_seconds:
                    raise TimeoutError("fallback calculation budget exhausted")
                return value

            value = yield from ledger.call_process("fallback_compute", choose)
            fallback["proposal"] = encode(value)
            yield from ledger.call_process("fallback_prepare", lambda: admission.check(self.engine, value))

            def publish() -> dict[str, Any]:
                receipt = admission.publish(self.engine, value, self.approved_charge)
                self._notify(charges, targets, receipt["accepted"])
                return receipt

            fallback["receipt"] = yield from ledger.publish_process(publish)
        except TimeoutError:
            fallback["receipt"] = {
                "accepted": False,
                "reason": "FALLBACK_BUDGET_EXCEEDED_HOLD",
                "applied": [],
            }
            yield from ledger.publish_process(lambda: self._notify(charges, targets, False))
