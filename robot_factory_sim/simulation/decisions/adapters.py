"""Run existing selectors on a private value workspace, returning intents only."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace
from typing import Any

from simulation.decisions.contracts import Intent, Observation, Proposal
from simulation.decisions.observation import trip_key
from simulation.scheduler import create_scheduler
from simulation.travel import TripEstimate


class ObservedRobot(SimpleNamespace):
    """Private working copy, never a live robot or process handle."""

    def position(self) -> tuple[float, float]:
        """Return interpolated coordinates captured by the owner thread."""
        return tuple(self._position)

    def is_available(self) -> bool:
        """Apply staged assignment/charge changes to subsequent local choices."""
        return self.available and self.current_task is None and not self.forced_charge

    def live_battery(self) -> float:
        """Return the frozen, already interpolated battery reading."""
        return self.live


class ObservedGraph:
    """Frozen distance/point lookups for the old selector interface."""

    def __init__(self, view: PolicyWorkspace) -> None:
        self.view = view
        self.facility_nodes = set(view.data["connected"])

    def facility_point(self, key: str) -> tuple[float, float] | None:
        """Read the observed approach point without recomputing a path."""
        value = self.view.data["points"].get(key)
        return tuple(value) if value else None

    def facility_floor(self, key: str) -> int | None:
        """Read the observed approach floor."""
        return self.view.data["floors"].get(key)

    def distance_to_facility(self, x: float, y: float, key: str, floor: int, width: float) -> float:
        """Return the legacy surrogate distance, not physical metres."""
        robot = next(r for r in self.view.robot_list() if r.cfg.width == width)
        return self.view.observed_trip(robot, key, (x, y, floor)).routing_cost_px


class PolicyWorkspace:
    """Minimal mutable calculation scratch space backed only by immutable input."""

    def __init__(self, observation: Observation) -> None:
        self.data = json.loads(observation.payload)
        self.now, self.env = observation.now, SimpleNamespace(now=observation.now)
        self.params = self.data["params"]
        self.factory = SimpleNamespace(profile=SimpleNamespace(**self.data["profile"]))
        self.plant = SimpleNamespace(**self.data["plant"])
        self.intents: list[Intent] = []
        self.pending_tasks = [SimpleNamespace(**t) for t in self.data["tasks"]]
        self.robots = {}
        for values in self.data["robots"]:
            values = dict(values)
            values["cfg"] = SimpleNamespace(**values["cfg"])
            values["current_task"] = SimpleNamespace(**values["current_task"]) if values["current_task"] else None
            values["_position"] = values.pop("position")
            self.robots[values["robot_id"]] = ObservedRobot(**values)
        for collection in ("stations", "chargers"):
            result = {}
            for key in self.data["station_order" if collection == "stations" else "charger_order"]:
                value = self.data[collection][key]
                attrs = dict(value)
                attrs["cfg"] = SimpleNamespace(**attrs["cfg"])
                attrs["resource"] = SimpleNamespace(count=attrs.pop("count"))
                result[key] = SimpleNamespace(**attrs)
            setattr(self, collection, result)
        self.path_graph = ObservedGraph(self)

    def observed_trip(
        self,
        robot: ObservedRobot,
        destination: str,
        origin: tuple[float, float, int] | None = None,
    ) -> TripEstimate:
        """Read an estimate for an origin included in the complete capture."""
        key = trip_key(robot.robot_id, origin or (*robot.position(), robot.floor), destination)
        values = self.data["trips"].get(key)
        if values is None:
            # Equal-width robots can share a graph query but not travel speeds.
            origin = origin or (*robot.position(), robot.floor)
            alternative = next(
                (
                    r
                    for r in self.robot_list()
                    if r.cfg.width == robot.cfg.width
                    and trip_key(r.robot_id, origin, destination) in self.data["trips"]
                ),
                None,
            )
            if alternative is None:
                raise ValueError("unobserved route lookup")
            values = self.data["trips"][trip_key(alternative.robot_id, origin, destination)]
        return TripEstimate(*(math.inf if value is None else value for value in values))

    def robot_list(self) -> list[ObservedRobot]:
        """Preserve engine robot order for stable ties."""
        return list(self.robots.values())

    def robot_ids(self) -> list[str]:
        """Preserve the baseline round-robin order."""
        return list(self.robots)

    def station_available(self, key: str) -> bool:
        """Use current observed availability and graph attachment."""
        return key in self.stations and self.stations[key].active and key in self.path_graph.facility_nodes

    def _usable_stations(self, task: Any) -> list[str]:
        return [
            key
            for key in (task.allowed_stations or (task.station_id,))
            if self.station_available(key)
            and self.stations[key].cfg.task_type == task.operation
            and self.stations[key].dedicated_order in (None, task.order_id)
        ]

    def pending_count_by_station(self) -> dict[str, int]:
        """Count remaining local jobs exactly as the existing selector does."""
        result: dict[str, int] = {}
        for task in self.pending_tasks:
            result[task.station_id] = result.get(task.station_id, 0) + 1
        return result

    def take_task(self, predicate: Any) -> Any:
        """Stage emergency selection without removing any live job."""
        choices = [t for t in self.pending_tasks if predicate(t) and self._usable_stations(t)]
        task = min(choices, key=lambda t: (t.priority, t.created_at), default=None)
        if task is not None:
            self.pending_tasks.remove(task)
        return task

    def requeue_task(self, task: Any) -> None:
        """Restore a local emergency candidate after local admission failure."""
        self.pending_tasks.append(task)

    def assign(self, task: Any, robot: ObservedRobot) -> bool:
        """Stage a job assignment and local ownership changes."""
        usable = self._usable_stations(task)
        if not usable:
            return False
        if task.station_id not in usable:
            task.station_id = min(
                usable,
                key=lambda s: self.path_graph.distance_to_facility(robot.x, robot.y, s, robot.floor, robot.cfg.width),
            )
        if task in self.pending_tasks:
            self.pending_tasks.remove(task)
        robot.current_task = task
        self.intents.append(Intent("assign", robot.robot_id, task.external_uid, task.station_id))
        return True

    def send_to_charge(self, robot: ObservedRobot) -> bool:
        """Stage predictive/opportunity charging without waking a robot."""
        if robot.target_id in self.chargers or robot.state in {
            "charging",
            "charge_wait",
        }:
            return False
        robot.forced_charge = True
        self.intents.append(Intent("charge", robot.robot_id))
        return True

    def preempt(self, robot: ObservedRobot, task: Any) -> bool:
        """Stage the same critical victim without interrupting a live process."""
        if task not in self.pending_tasks:
            return False
        self.pending_tasks.remove(task)
        self.intents.append(Intent("preempt", robot.robot_id, task.external_uid, task.station_id))
        return True


def propose(
    observation: Observation,
    *,
    dispatch: bool = True,
    charge_robots: tuple[str, ...] = (),
    target_robots: tuple[str, ...] = (),
) -> Proposal:
    """Select on a private workspace using the production policy implementation."""
    view = PolicyWorkspace(observation)
    state = view.data["policy_state"]
    if observation.policy == "safe_fallback":
        from simulation.planning.controller import SafeFallback

        scheduler = SafeFallback()
    else:
        scheduler = create_scheduler(observation.policy)
    for key, attribute in (
        ("pointer", "_pointer"),
        ("next_plan", "_next_plan"),
        ("weights", "weights"),
    ):
        if key in state:
            setattr(scheduler, attribute, state[key])
    # The robot's idle loop checks reactive charging before accepting new work.
    # Preserve that order when charge and dispatch triggers share one observation.
    for rid in charge_robots:
        robot = view.robots.get(rid)
        if robot is not None and robot.current_task is None and (
            robot.forced_charge or scheduler.should_charge(robot, view)
        ):
            view.send_to_charge(robot)
    if dispatch:
        scheduler.dispatch(view)
    charging = {i.robot for i in view.intents if i.kind == "charge"}
    for rid in dict.fromkeys((*charge_robots, *charging)):
        if rid not in view.robots:
            continue
        robot = view.robots[rid]
        if robot.current_task is not None:
            continue
        if rid not in charging and not robot.forced_charge and not scheduler.should_charge(robot, view):
            continue
        charger = scheduler.choose_charger(robot, view)
        if charger is not None:
            view.intents.append(
                Intent(
                    "charge_choice",
                    rid,
                    destination=charger.id,
                    target=scheduler.charge_target(robot, view),
                )
            )
    for rid in target_robots:
        if rid not in view.robots or view.robots[rid].state != "charging":
            continue
        view.intents.append(
            Intent(
                "charge_target",
                rid,
                target=scheduler.charge_target(view.robots[rid], view),
            )
        )
    output = {
        key: getattr(scheduler, attribute)
        for key, attribute in (
            ("pointer", "_pointer"),
            ("next_plan", "_next_plan"),
            ("weights", "weights"),
        )
        if hasattr(scheduler, attribute)
    }
    records = getattr(scheduler, "solve_records", [])
    return Proposal(
        observation,
        tuple(view.intents),
        json.dumps(output, sort_keys=True),
        json.dumps({"legacy_solves": records}, sort_keys=True, allow_nan=False),
    )
