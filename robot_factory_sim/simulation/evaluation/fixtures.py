"""Six small foundation fixtures, independent of GUI demos and optimizer outcomes."""

from __future__ import annotations

from simulation.evaluation.scenario import Scenario
from simulation.plant import PlantSettings, RobotInitial
from utils.config_models import (
    ChargingStationConfig,
    ElevatorConfig,
    FactoryConfig,
    FactoryProfile,
    PathEdgeConfig,
    PathNodeConfig,
    RobotConfig,
    WorkStationConfig,
)


def _station(sid: str, x: float, y: float, floor: int = 1) -> WorkStationConfig:
    return WorkStationConfig(
        sid, sid, x, y, task_type="inspection", avg_process_time=8, process_time_std=0.8, floor=floor
    )


def _case(
    name: str,
    factory: FactoryConfig,
    positions: dict[str, RobotInitial],
    rate: float = 0.08,
    cabins: dict[str, int] | None = None,
) -> Scenario:
    return Scenario(
        name,
        factory,
        600,
        0,
        positions,
        cabins or {},
        PlantSettings(),
        {"kind": "poisson", "rate": rate, "priority": 2, "deadline_slack": 90, "fixed_station": True},
    )


def examples() -> tuple[Scenario, ...]:
    """Build open, assignment, corridor, intersection, charge and multi-floor cases."""
    profile = FactoryProfile(floor_width=400, floor_height=400)
    stations = [_station("S0", 100, 200), _station("S1", 300, 200)]
    nodes = [
        PathNodeConfig(1, 200, 200, "charger", "C"),
        PathNodeConfig(2, 100, 200, "station", "S0"),
        PathNodeConfig(3, 300, 200, "station", "S1"),
    ]
    open_factory = FactoryConfig(
        profile,
        stations,
        [ChargingStationConfig("C", 200, 200)],
        [RobotConfig("R0", move_speed=1), RobotConfig("R1", move_speed=1)],
        [],
        nodes,
        [PathEdgeConfig(1, 1, 2, width=3), PathEdgeConfig(2, 1, 3, width=3)],
    )
    open_case = _case("open", open_factory, {rid: RobotInitial(200, 200) for rid in ("R0", "R1")}, 0.04)

    xs = (100, 114, 120, 130, 150, 158)
    links = {100: "S0", 114: "C1", 120: "S1", 130: "S2", 150: "S3", 158: "C0"}
    pair_nodes = [
        PathNodeConfig(i + 1, x, 100, "charger" if links[x].startswith("C") else "station", links[x])
        for i, x in enumerate(xs)
    ]
    paired = FactoryConfig(
        profile=FactoryProfile(floor_width=260, floor_height=220),
        stations=[_station(f"S{i}", x, 100) for i, x in enumerate((100, 120, 130, 150))],
        chargers=[ChargingStationConfig("C0", 158, 100), ChargingStationConfig("C1", 114, 100)],
        robots=[RobotConfig("R0", move_speed=1), RobotConfig("R1", move_speed=1)],
        nodes=pair_nodes,
        edges=[PathEdgeConfig(i, i, i + 1, width=3) for i in range(1, len(xs))],
    )
    joint_case = _case("joint_dispatch", paired, {"R0": RobotInitial(130, 100), "R1": RobotInitial(100, 100)}, 0.12)

    narrow = FactoryConfig(
        profile=FactoryProfile(floor_width=300, floor_height=250),
        stations=[_station("S0", 50, 100), _station("S1", 250, 100)],
        chargers=[ChargingStationConfig("C", 50, 150)],
        robots=[RobotConfig("R0", move_speed=1), RobotConfig("R1", move_speed=1)],
        nodes=[
            PathNodeConfig(1, 50, 100, "station", "S0"),
            PathNodeConfig(2, 250, 100, "station", "S1"),
            PathNodeConfig(3, 50, 150, "charger", "C"),
        ],
        edges=[PathEdgeConfig(1, 1, 2, width=1.3), PathEdgeConfig(2, 1, 3, width=3)],
    )
    narrow_case = _case("narrow", narrow, {"R0": RobotInitial(50, 100), "R1": RobotInitial(250, 100)}, 0.1)

    junction = FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400),
        stations=[_station("S0", 100, 200), _station("S1", 300, 200), _station("S2", 200, 100)],
        chargers=[ChargingStationConfig("C", 200, 300)],
        robots=[RobotConfig("R0", move_speed=1), RobotConfig("R1", move_speed=1)],
        nodes=[
            PathNodeConfig(1, 200, 200, "intersection"),
            PathNodeConfig(2, 100, 200, "station", "S0"),
            PathNodeConfig(3, 300, 200, "station", "S1"),
            PathNodeConfig(4, 200, 100, "station", "S2"),
            PathNodeConfig(5, 200, 300, "charger", "C"),
        ],
        edges=[PathEdgeConfig(i, 1, i + 1, width=2.2) for i in range(1, 5)],
    )
    intersection_case = _case(
        "intersection", junction, {"R0": RobotInitial(100, 200), "R1": RobotInitial(200, 300)}, 0.1
    )

    charge = FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400),
        stations=[_station("S0", 100, 200), _station("S1", 300, 200)],
        chargers=[ChargingStationConfig("C", 200, 200, capacity=1, charge_rate=0.25)],
        robots=[RobotConfig(f"R{i}", battery_capacity=20, move_speed=1, battery_drain_rate=0.2) for i in range(4)],
        nodes=[
            PathNodeConfig(1, 200, 200, "charger", "C"),
            PathNodeConfig(2, 100, 200, "station", "S0"),
            PathNodeConfig(3, 300, 200, "station", "S1"),
        ],
        edges=[PathEdgeConfig(1, 1, 2, width=3), PathEdgeConfig(2, 1, 3, width=3)],
    )
    charge_case = _case(
        "charging", charge, {f"R{i}": RobotInitial(200, 200, soc_pct=soc) for i, soc in enumerate((35, 60, 25, 80))}
    )

    multi = FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400, floor_count=2),
        stations=[_station("S0", 100, 200), _station("S1", 100, 200, 2)],
        chargers=[ChargingStationConfig("C0", 100, 300), ChargingStationConfig("C1", 100, 300, floor=2)],
        robots=[RobotConfig("R0", move_speed=1), RobotConfig("R1", move_speed=1)],
        elevators=[ElevatorConfig("E", "E", 200, 200, travel_time=4)],
        nodes=[
            PathNodeConfig(1, 100, 200, "station", "S0"),
            PathNodeConfig(2, 100, 300, "charger", "C0"),
            PathNodeConfig(3, 200, 200, "elevator", "E"),
            PathNodeConfig(4, 100, 200, "station", "S1", floor=2),
            PathNodeConfig(5, 100, 300, "charger", "C1", floor=2),
            PathNodeConfig(6, 200, 200, "elevator", "E", floor=2),
        ],
        edges=[
            PathEdgeConfig(1, 1, 2, width=3),
            PathEdgeConfig(2, 1, 3, width=3),
            PathEdgeConfig(3, 4, 5, width=3),
            PathEdgeConfig(4, 4, 6, width=3),
        ],
    )
    multi_case = _case(
        "multifloor", multi, {"R0": RobotInitial(100, 300), "R1": RobotInitial(100, 300, floor=2)}, cabins={"E": 1}
    )
    cases = (open_case, joint_case, narrow_case, intersection_case, charge_case, multi_case)
    for case in cases:
        case.validate()
    return cases


def joint_batch() -> Scenario:
    """A deterministic two-job assignment counterexample, not a general benchmark."""
    scenario = examples()[1]
    scenario.id = "joint_batch"
    scenario.duration = 30
    for station in scenario.factory.stations:
        station.process_time_std = 0
    scenario.workload = {
        "kind": "trace",
        "jobs": [
            {
                "uid": "batch:first",
                "at": 0,
                "station": "S1",
                "allowed": ["S1"],
                "operation": "inspection",
                "priority": 2,
                "noise": 0,
                "deadline": 13,
            },
            {
                "uid": "batch:second",
                "at": 0,
                "station": "S3",
                "allowed": ["S3"],
                "operation": "inspection",
                "priority": 2,
                "noise": 0,
                "deadline": 13,
            },
        ],
    }
    scenario.validate()
    return scenario
