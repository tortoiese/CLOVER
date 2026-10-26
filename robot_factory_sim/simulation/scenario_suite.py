"""Predeclared mechanism-based scenarios and disjoint screening/validation seeds.

The heuristic policy and physical model are identical across all experiment arms.
Scenario parameters are fixed before evaluating the held-out cohort.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from database.seed_data import build_default_config
from simulation.experiments import Demand, generate_trace
from simulation.planning.contracts import Settings
from utils.config_models import (
    ChargingStationConfig,
    FactoryConfig,
    FactoryProfile,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)

SCREENING_SEEDS = (42, 43, 44)
VALIDATION_SEEDS = tuple(range(100, 110))


@dataclass(frozen=True)
class Scenario:
    """A fixed configuration, public operating assumption and benchmark settings."""

    id: str
    hypothesis: str
    factory: FactoryConfig
    settings: Settings
    duration: float = 120
    wave_period: float = 8

    def trace(self, seed: int) -> tuple[Demand, ...]:
        """Create the complete exogenous trace before any policy starts."""
        if self.id.startswith("alternating_dispatch"):
            rng = np.random.default_rng(seed)
            jobs = []
            for wave, start in enumerate(np.arange(0, self.duration - 1, self.wave_period)):
                at = float(start + rng.uniform(0, .15))
                destinations = ("S1", "S3") if wave % 2 == 0 else ("S2", "S0")
                for sid in destinations:
                    jobs.append(Demand(len(jobs), at, sid, (sid,), "inspection", 2,
                                       float(rng.normal()), at + 7))
            return tuple(jobs)
        return generate_trace(self.factory, self.duration, .2 if self.id == "reference_factory" else .6, seed)


def scenarios() -> tuple[Scenario, ...]:
    """Return an assignment-coupling case, a charger case and a neutral reference."""
    profile = FactoryProfile(factory_name="교대 물류 배차", floor_width=260, floor_height=220)
    xs = (100, 114, 120, 130, 150, 158)
    links = {100: "S0", 114: "C1", 120: "S1", 130: "S2", 150: "S3", 158: "C0"}
    nodes = [PathNodeConfig(i + 1, x, 100, linked_station_id=links[x]) for i, x in enumerate(xs)]
    stations = [WorkStationConfig(f"S{i}", f"검사 셀 {i}", x, 100, task_type="inspection",
                                 avg_process_time=3, process_time_std=.15) for i, x in enumerate((100, 120, 130, 150))]
    paired = FactoryConfig(profile=profile, stations=stations,
                           chargers=[ChargingStationConfig("C0", 158, 100), ChargingStationConfig("C1", 114, 100)],
                           robots=[RobotConfig("R0", move_speed=1, battery_drain_rate=.1),
                                   RobotConfig("R1", move_speed=1, battery_drain_rate=.1)],
                           nodes=nodes, edges=[PathEdgeConfig(i, i, i + 1, width=5) for i in range(1, len(xs))])
    # The existing dock spacing places R0 at x=130 and R1 at x=100, for every policy.
    charge_nodes = [PathNodeConfig(1, 100, 100, linked_station_id="C")]
    charge_stations = []
    charge_edges = []
    for i, (x, y) in enumerate(((60, 100), (140, 100), (100, 60), (100, 140)), 2):
        sid = f"W{i}"
        charge_stations.append(WorkStationConfig(sid, sid, x, y, task_type="inspection",
                                                 avg_process_time=12, process_time_std=.4))
        charge_nodes.append(PathNodeConfig(i, x, y, linked_station_id=sid))
        charge_edges.append(PathEdgeConfig(i, 1, i, width=5))
    charge = FactoryConfig(profile=FactoryProfile(factory_name="단일 충전 슬롯", floor_width=260, floor_height=220),
                           stations=charge_stations, chargers=[ChargingStationConfig("C", 100, 100, charge_rate=.25)],
                           robots=[RobotConfig(f"R{i}", battery_capacity=6, move_speed=1, battery_drain_rate=.15)
                                   for i in range(4)], nodes=charge_nodes, edges=charge_edges)
    return (
        Scenario("alternating_dispatch", "Joint assignment preserves the nearby robot for the other destination.",
                 paired, Settings(horizon=20, interval=1, timeout=2, scope="traffic")),
        Scenario("alternating_dispatch_longer", "Sensitivity: 240 seconds and batches every 10 seconds.",
                 paired, Settings(horizon=20, interval=1, timeout=2, scope="traffic"), duration=240, wave_period=10),
        Scenario("single_charge_slot", "Partial charging may reduce contention for one shared charging slot.",
                 charge, Settings(horizon=40, interval=2, timeout=2, scope="traffic")),
        Scenario("reference_factory", "Reference workload; no performance improvement is assumed.",
                 build_default_config(), Settings(horizon=60, interval=5, timeout=2, scope="traffic")),
    )
