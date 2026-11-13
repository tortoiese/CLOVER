"""Explicit synthetic reference maps; never substitute them for team factory data."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

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

REFERENCE_SCHEMA = "clover-design-references-v1"


def _station(sid: str, x: float, y: float, floor: int = 1) -> WorkStationConfig:
    return WorkStationConfig(sid, sid, x, y, task_type="inspection", avg_process_time=8,
                             process_time_std=0, floor=floor)


def _robots(count: int) -> list[RobotConfig]:
    return [RobotConfig(f"R{i}", move_speed=1, battery_drain_rate=0.1) for i in range(count)]


def _open(count: int = 4) -> FactoryConfig:
    stations = [_station(f"S{i}", x, y) for i, (x, y) in enumerate(
        ((100, 200), (300, 200), (200, 100), (200, 300)))]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
        chargers=[ChargingStationConfig("C", 200, 200, capacity=4, charge_rate=5)], robots=_robots(count),
        nodes=[PathNodeConfig(1, 200, 200, "charger", "C")]
        + [PathNodeConfig(i + 2, s.x, s.y, "station", s.id) for i, s in enumerate(stations)],
        edges=[PathEdgeConfig(i + 1, 1, i + 2, width=5) for i in range(4)],
    )


def _narrow() -> FactoryConfig:
    stations = [_station(f"S{i}", 350, y) for i, y in enumerate((100, 175, 250, 325))]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
        chargers=[ChargingStationConfig("C", 50, 200, capacity=4, charge_rate=5)], robots=_robots(4),
        nodes=[PathNodeConfig(1, 50, 200, "charger", "C"), PathNodeConfig(2, 100, 200),
               PathNodeConfig(3, 300, 200)]
        + [PathNodeConfig(i + 4, s.x, s.y, "station", s.id) for i, s in enumerate(stations)],
        edges=[PathEdgeConfig(1, 2, 3, width=1.3), PathEdgeConfig(2, 1, 2, width=5)]
        + [PathEdgeConfig(i + 3, 3, i + 4, width=5) for i in range(4)],
    )


def _joint() -> FactoryConfig:
    """Two copies of a local greedy assignment counterexample, fixed before runs."""
    xs = (100, 114, 120, 130, 150, 158)
    nodes, stations, chargers, edges = [], [], [], []
    for row, y in enumerate((100, 300)):
        base = row * 6
        for col, x in enumerate(xs):
            node = PathNodeConfig(base + col + 1, x, y)
            if col in (2, 4):
                sid = f"S{row * 2 + (col == 4)}"
                node.node_type, node.linked_station_id = "station", sid
                stations.append(_station(sid, x, y))
            elif col in (1, 5):
                cid = f"C{row * 2 + (col == 5)}"
                node.node_type, node.linked_station_id = "charger", cid
                chargers.append(ChargingStationConfig(cid, x, y, capacity=2, charge_rate=5))
            nodes.append(node)
        edge_start = len(edges)
        edges.extend(PathEdgeConfig(edge_start + i + 1, base + i + 1, base + i + 2, width=5) for i in range(5))
    edges.append(PathEdgeConfig(11, 1, 7, width=5))
    return FactoryConfig(profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
                         chargers=chargers, robots=_robots(4), nodes=nodes, edges=edges)


def _junction() -> FactoryConfig:
    stations = [_station(f"S{i}", x, y) for i, (x, y) in enumerate(
        ((100, 200), (300, 200), (200, 100), (350, 200)))]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
        chargers=[ChargingStationConfig("C", 200, 300, capacity=4, charge_rate=5)], robots=_robots(4),
        nodes=[PathNodeConfig(1, 200, 200, "intersection"), PathNodeConfig(2, 200, 300, "charger", "C")]
        + [PathNodeConfig(i + 3, s.x, s.y, "station", s.id) for i, s in enumerate(stations)],
        edges=[PathEdgeConfig(i + 1, a, b, width=5)
               for i, (a, b) in enumerate(((1, 2), (1, 3), (1, 4), (1, 5), (4, 6)))],
    )


def _heterogeneous() -> FactoryConfig:
    stations = [_station(f"S{i}", x, y) for i, (x, y) in enumerate(
        ((100, 200), (300, 200), (100, 150), (300, 150)))]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
        chargers=[ChargingStationConfig("C", 100, 300, capacity=4, charge_rate=5)], robots=_robots(4),
        nodes=[PathNodeConfig(1, 100, 200, "station", "S0"), PathNodeConfig(2, 300, 200, "station", "S1"),
               PathNodeConfig(3, 100, 300, "charger", "C"), PathNodeConfig(4, 300, 300),
               PathNodeConfig(5, 100, 150, "station", "S2"), PathNodeConfig(6, 300, 150, "station", "S3")],
        edges=[PathEdgeConfig(i + 1, a, b, width=1.3 if i == 0 else 2.2)
               for i, (a, b) in enumerate(((1, 2), (1, 3), (3, 4), (4, 2), (1, 5), (2, 6)))],
    )


def _multifloor() -> FactoryConfig:
    stations = [_station(f"S{i}", x, 100, 2 if i < 4 else 1)
                for i, x in enumerate((50, 150, 250, 350) * 2)]
    nodes = [PathNodeConfig(1, 100, 200, "elevator", "E0"),
             PathNodeConfig(2, 100, 200, "elevator", "E0", floor=2),
             PathNodeConfig(3, 300, 200, "elevator", "E1"),
             PathNodeConfig(4, 300, 200, "elevator", "E1", floor=2),
             PathNodeConfig(5, 100, 300, "charger", "C0"),
             PathNodeConfig(6, 300, 300, "charger", "C1")]
    nodes += [PathNodeConfig(10 + i, s.x, s.y, "station", s.id, floor=s.floor)
              for i, s in enumerate(stations)]
    links = [(5, 1), (6, 3), (5, 6), (2, 4)]
    links += [(2 if i < 2 else 4, 10 + i) for i in range(4)]
    links += [(1 if i < 2 else 3, 14 + i) for i in range(4)]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400, floor_count=2), stations=stations,
        chargers=[ChargingStationConfig("C0", 100, 300, capacity=2, charge_rate=5),
                  ChargingStationConfig("C1", 300, 300, capacity=2, charge_rate=5)], robots=_robots(4),
        elevators=[ElevatorConfig("E0", "E0", 100, 200),
                   ElevatorConfig("E1", "E1", 300, 200, is_active=False)],
        nodes=nodes, edges=[PathEdgeConfig(i + 1, a, b, width=5) for i, (a, b) in enumerate(links)],
    )


def _grid(side: int, count: int = 4) -> FactoryConfig:
    points = [(20 + c * 280 / (side - 1), 20 + r * 280 / (side - 1))
              for r in range(side) for c in range(side)]
    corners = [0, side - 1, side * (side - 1), side * side - 1]
    center = side // 2 * side + side // 2
    stations = [_station(f"S{i}", *points[p]) for i, p in enumerate(corners)]
    nodes = [PathNodeConfig(i + 1, x, y) for i, (x, y) in enumerate(points)]
    nodes[center].node_type, nodes[center].linked_station_id = "charger", "C"
    for i, p in enumerate(corners):
        nodes[p].node_type, nodes[p].linked_station_id = "station", f"S{i}"
    links = [(r * side + c + 1, r * side + c + 2) for r in range(side) for c in range(side - 1)]
    links += [(r * side + c + 1, (r + 1) * side + c + 1) for r in range(side - 1) for c in range(side)]
    return FactoryConfig(
        profile=FactoryProfile(floor_width=400, floor_height=400), stations=stations,
        chargers=[ChargingStationConfig("C", *points[center], capacity=4, charge_rate=5)],
        robots=_robots(count), nodes=nodes,
        edges=[PathEdgeConfig(i + 1, a, b, width=5) for i, (a, b) in enumerate(links)],
    )


def synthetic_references() -> dict[str, Any]:
    """Return editable, uncalibrated prototypes with complete initial conditions.

    The 64/256-node grids are separate synthetic cohorts. The other graphs are
    mechanism fixtures, not the team's final 64/256-node factory layouts.
    """
    factories = {f"S{i:02d}": _open(8 if i in {5, 6} else 4) for i in range(12)}
    factories.update(S01=_joint(), S02=_narrow(), S03=_junction(), S07=_heterogeneous(),
                     S08=_multifloor(), S11=_grid(8))
    factories.update(X01=_narrow(), X02=_open(), X03=_multifloor(), X04=_open())
    rates = dict(zip((f"S{i:02d}" for i in range(12)), (.03, 0, 0, 0, .04, .18, .17, .08, 0, .08, .14, .08)))
    rates.update(X01=.04, X02=.04, X03=.08, X04=.084)
    cases, targets = {}, {}
    for family, factory in factories.items():
        battery_case = family in {"S04", "S09", "X01", "X02"}
        if battery_case:
            factory.robots = [replace(r, battery_capacity=20, battery_drain_rate=.2) for r in factory.robots]
        if family in {"S04", "X02"}:
            factory.chargers[0].capacity, factory.chargers[0].charge_rate = 1, .25
        if family == "S09":
            factory.chargers[0].charge_rate = 1
        if family == "S10":
            for station in factory.stations:
                station.avg_process_time = 20
        charger = factory.chargers[0]
        initial = {r.id: RobotInitial(charger.x, charger.y, soc_pct=50 if battery_case else 80)
                   for r in factory.robots}
        if family == "S01":
            initial = {f"R{i}": RobotInitial(x, y, soc_pct=80)
                       for i, (x, y) in enumerate(((130, 100), (100, 100), (130, 300), (100, 300)))}
        if family in {"S08", "X03"}:
            initial = {f"R{i}": RobotInitial(100 if i < 2 else 300, 300, soc_pct=80) for i in range(4)}
        batch = family in {"S01", "S02", "S03", "S08"}
        duration, warmup = (300, 0) if batch else (7800, 600)
        if family == "S09":
            duration, warmup = 1200, 0
        if family == "S10":
            warmup = 0
        case = Scenario(
            f"REF_{family}", factory, duration, warmup, initial,
            {e.id: 1 for e in factory.elevators},
            PlantSettings(work_drain_per_sec=.5 if battery_case else .1),
            {"kind": "poisson", "rate": rates[family], "fixed_station": True, "deadline_slack": 120},
        )
        case.validate()
        cases[family] = case.to_dict()
        targets[family] = {"corridor_edges": [1], "station": factory.stations[0].id,
                           "charger": charger.id, "robot": factory.robots[0].id, "edge": 1}
        if family == "S00":
            targets[family]["corridor_edges"] = [e.id for e in factory.edges]
        if family == "S03":
            targets[family]["intersection_node"] = 1
        if family in {"S08", "X03"}:
            targets[family].update(primary_cabin="E0", secondary_cabin="E1")
    variants = {}
    for cell_id, factory in (("S11_graph_256", _grid(16)), ("S11_robots_8", _grid(8, 8))):
        raw = copy.deepcopy(cases["S11"])
        raw["factory"] = factory.to_dict()
        c = factory.chargers[0]
        raw["initial_robots"] = {r.id: {"x": c.x, "y": c.y, "floor": 1, "soc_pct": 80}
                                 for r in factory.robots}
        Scenario.from_dict(raw)
        variants[cell_id] = raw
    return {"schema": REFERENCE_SCHEMA, "layout_id": "synthetic-reference-v1",
            "provenance": "synthetic_reference_not_team_factory", "calibration_status": "UNCALIBRATED",
            "cases": cases, "variants": variants, "targets": targets,
            "reference_mu": {"S00": .12, "S10": .20, "X04": .12},
            "assumptions": ["0.1 metre/pixel; battery-unit energy",
                            "rates are E1 calibration candidates, not demonstrated capacities",
                            "S09 transient window has no warmup; irreversible events have no recovery",
                            "S09 adds one common pre-intervention probe job to every arm",
                            "grids of different node counts are distinct environment cohorts"]}
