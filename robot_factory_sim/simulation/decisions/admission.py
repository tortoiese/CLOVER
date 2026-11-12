"""Prepare without side effects, then validate and publish on the owner thread."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import config
from simulation.decisions.contracts import Observation, Proposal, digest
from simulation.decisions.observation import TASK_FIELDS
from simulation.planning.snapshot import topology_hash
from simulation.travel import estimate_trip
from utils.event_types import RobotState

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def check(engine: SimulationEngine, proposal: Proposal) -> tuple[str, ...]:
    """Validate affected actors and jobs without invalidating unrelated arrivals."""
    obs, errors = proposal.observation, []
    data = json.loads(obs.payload)
    if (engine.execution_epoch, engine.layout_version, topology_hash(engine)) != (
        obs.epoch,
        obs.layout,
        data["topology"],
    ):
        return ("STALE_LAYOUT_OR_EPOCH",)
    if engine.params != data["params"]:
        return ("STALE_PARAMETERS",)
    before = {r["robot_id"]: r for r in data["robots"]}
    old_jobs = {t["external_uid"]: t for t in data["tasks"]}
    pending = {t.external_uid: t for t in engine.pending_tasks}
    if any(t.priority == 0 and t.external_uid not in old_jobs for t in engine.pending_tasks):
        return ("STALE_CRITICAL_ARRIVAL",)
    assignments, preemptions = set(), set()
    for intent in proposal.intents:
        robot = engine.robots.get(intent.robot)
        previous = before.get(intent.robot)
        if robot is None or previous is None or not robot.alive or robot.color_index != previous["instance"]:
            errors.append("STALE_ROBOT_INSTANCE")
            continue
        if asdict(robot.cfg) != previous["cfg"]:
            errors.append("STALE_ROBOT_CONFIG")
        if intent.kind in {"assign", "charge", "charge_choice"}:
            if not robot.is_available() and not (
                robot.forced_charge and robot.current_task is None and robot.state == RobotState.IDLE
            ):
                errors.append("STALE_ROBOT_AVAILABILITY")
            if (robot.x, robot.y, robot.floor) != (
                previous["x"],
                previous["y"],
                previous["floor"],
            ):
                errors.append("STALE_ROBOT_POSITION")
            if abs(robot.live_battery() - previous["live"]) > 1e-7:
                errors.append("STALE_ROBOT_ENERGY")
        if intent.kind in {"assign", "preempt"}:
            if intent.robot in assignments or intent.job in preemptions:
                errors.append("DUPLICATE_OWNER")
            assignments.add(intent.robot)
            preemptions.add(intent.job)
            task = pending.get(intent.job)
            if task is None or digest(old_jobs.get(intent.job)) != digest({k: getattr(task, k) for k in TASK_FIELDS}):
                errors.append("STALE_JOB")
            elif intent.destination not in engine._usable_stations(task):
                errors.append("STALE_JOB_CAPABILITY")
            if intent.kind == "preempt":
                old_task = previous["current_task"]
                if (
                    robot.state != RobotState.MOVING
                    or robot.current_task is None
                    or old_task is None
                    or robot.current_task.external_uid != old_task["external_uid"]
                    or robot.battery_pct <= config.RECALL_MIN_BATTERY
                ):
                    errors.append("STALE_PREEMPTION")
        if intent.kind in {"assign", "preempt", "charge_choice"}:
            target = (
                engine.chargers.get(intent.destination)
                if intent.kind == "charge_choice"
                else (engine.stations.get(intent.destination))
            )
            if (
                target is None
                or not target.active
                or not math.isfinite(estimate_trip(engine, robot, intent.destination).seconds)
            ):
                errors.append("STALE_DESTINATION")
        if intent.kind in {"charge_choice", "charge_target"}:
            if intent.target is None or not math.isfinite(intent.target) or not 0 <= intent.target <= 100:
                errors.append("INVALID_CHARGE_TARGET")
        if intent.kind == "charge_target" and robot.state != RobotState.CHARGING:
            errors.append("STALE_CHARGE_SESSION")
        if intent.kind not in {
            "assign",
            "preempt",
            "charge",
            "charge_choice",
            "charge_target",
        }:
            errors.append("UNKNOWN_INTENT")
    return tuple(dict.fromkeys(errors))


def publish(
    engine: SimulationEngine,
    proposal: Proposal,
    approved_charge: dict[str, tuple[str, float]],
) -> dict[str, Any]:
    """Check current physical facts, then expose safe actions in one SimPy event.

    Partial unexpected failure is explicit. Existing successful assignments are
    woken and retained; unconsumed tasks are never removed speculatively.
    """
    errors = check(engine, proposal)
    if errors:
        return {"accepted": False, "reason": ";".join(errors), "applied": []}
    pending = {t.external_uid: t for t in engine.pending_tasks}
    applied, wake = [], []
    choices = {i.robot: i for i in proposal.intents if i.kind == "charge_choice"}
    reason = "COMMITTED"
    try:
        for intent in proposal.intents:
            robot = engine.robots[intent.robot]
            if intent.kind == "assign":
                task = pending[intent.job]
                original = task.station_id
                task.station_id = intent.destination
                try:
                    if not engine.assign(task, robot, wake=False):
                        task.station_id = original
                        raise ValueError("assignment no longer eligible")
                except Exception:
                    if robot.current_task is not task:
                        task.station_id = original
                    raise
                wake.append(robot)
            elif intent.kind == "preempt":
                if not engine.preempt(robot, pending[intent.job]):
                    raise ValueError("preemption rejected")
            elif intent.kind == "charge":
                if intent.robot not in choices:
                    continue
            elif intent.kind == "charge_choice":
                approved_charge[intent.robot] = (intent.destination, intent.target)
                robot.forced_charge = True
                wake.append(robot)
            elif intent.kind == "charge_target":
                previous = approved_charge.get(intent.robot)
                if previous is not None:
                    approved_charge[intent.robot] = (previous[0], intent.target)
            applied.append(asdict(intent))
    except Exception as exc:
        reason = "PARTIAL_APPLICATION: " + str(exc)
    finally:
        for robot in wake:
            robot.wake()
    return {"accepted": reason == "COMMITTED", "reason": reason, "applied": applied}


class CohortView:
    """Read-only engine facade retaining the request's original pending cohort."""

    def __init__(self, engine: SimulationEngine, observation: Observation) -> None:
        self.engine = engine
        identities = {t["external_uid"] for t in json.loads(observation.payload)["tasks"]}
        self.pending_tasks = [t for t in engine.pending_tasks if t.external_uid in identities]

    def __getattr__(self, key: str) -> Any:
        return getattr(self.engine, key)


def revalidate_rh(engine: SimulationEngine, result: Any, observation: Observation) -> tuple[Any, str]:
    """Rebase the original cohort, never provide new jobs to an old solve."""
    from simulation.planning.delay import rebase

    return rebase(CohortView(engine, observation), result)


def publish_rh(engine: SimulationEngine, result: Any, observation: Observation) -> dict[str, Any]:
    """Fresh physical revalidation and atomic prefix application in the prepaid event."""
    from simulation.planning.execution import commit
    from simulation.planning.replay import encode
    from simulation.planning.snapshot import capture

    old = json.loads(observation.payload)
    ids = {t["external_uid"] for t in old["tasks"]}
    if any(t.priority == 0 and t.external_uid not in ids for t in engine.pending_tasks):
        return {
            "accepted": False,
            "reason": "STALE_CRITICAL_ARRIVAL",
            "applied_result": None,
        }
    shifted, reason = revalidate_rh(engine, result, observation)
    if shifted is None:
        return {"accepted": False, "reason": reason, "applied_result": None}
    current = capture(engine, shifted.input.snapshot.settings, shifted.input.snapshot.token)
    accepted, reason = commit(engine, shifted, observed_snapshot=current)
    return {
        "accepted": accepted,
        "reason": reason,
        "applied_result": encode(shifted) if accepted else None,
    }
