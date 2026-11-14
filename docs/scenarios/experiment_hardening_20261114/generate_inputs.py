"""Generate explicit synthetic development/pilot inputs without running a simulation."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from simulation.decisions.contracts import DEADLINE_PROTOCOL, DecisionSettings
from simulation.evaluation.design import materialize
from simulation.evaluation.design_references import synthetic_references
from simulation.evaluation.scenario import Scenario, fingerprint
from simulation.evaluation.study import validate_spec
from simulation.planning.contracts import Settings

ROOT = Path(__file__).resolve().parents[3]
DOC: Path


def write(path: Path, value: Any) -> None:
    """Write a new artifact, preserving earlier files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")


def window(case: Scenario, metadata: dict[str, Any], phase: str) -> dict[str, Any]:
    """Declare the common finite observation and charging exposure requirement."""
    batch = metadata["input_variation"] == "deterministic"
    return {
        "mode": "transient_stress" if phase == "stress" else ("finite_batch" if batch else "finite_stream"),
        "arrival_cutoff": case.duration,
        "drain_seconds": 0,
        "drain_in_measurement": True,
        "minimum_observation_seconds": case.duration - case.warmup,
        "minimum_completed_charge_sessions_per_robot": 2 if metadata["family"] == "S04" else 0,
        "minimum_charge_fraction": 0.1,
        "nominal": phase != "stress" and metadata["family"] not in ("S04", "S09", "S10"),
    }


def modify_geometry(case: Scenario, scale: float) -> None:
    """Scale actual paths and facility/initial coordinates, keeping widths fixed."""
    case.factory.profile.floor_width *= scale
    case.factory.profile.floor_height *= scale
    for group in (case.factory.stations, case.factory.chargers, case.factory.nodes, case.factory.elevators):
        for obj in group:
            obj.x *= scale
            obj.y *= scale
    case.initial_robots = {
        rid: replace(value, x=value.x * scale, y=value.y * scale) for rid, value in case.initial_robots.items()
    }
    for edge in case.factory.edges:
        if edge.distance is not None:
            edge.distance *= scale


def base_spec() -> dict[str, Any]:
    """Choose a research reference profile without claiming tuning optimality."""
    return {
        "schema": "clover-study-spec-v1",
        "stage": "pilot",
        "provenance": {
            "kind": "synthetic_research",
            "reviewed_by": "user approved synthetic validation on 2026-11-14",
            "description": (
                'Reviewed design-v3 synthetic reference geometry; explicit scale variants are designed '
                'examples, not independent factory samples.'
            ),
        },
        "decision_settings": DecisionSettings(
            timing_protocol=DEADLINE_PROTOCOL,
            end_to_end_soft_deadline_seconds=2,
            publication_allowance_seconds=0.2,
            admission_reserve_seconds=0.1,
            primary_deadline_policy="reject_late_primary",
            record_planning_inputs=True,
        ).to_dict(),
        "planning_settings": asdict(
            Settings(horizon=40, step=2, max_tasks=8, sigma=0, timeout=2, watchdog=3, native_actions=4)
        ),
        "selection": {
            "method": "a_priori",
            "rationale": (
                'H40/t2 is a prespecified research reference informed by the historical pilot, not a '
                'demonstrated optimum; weights unchanged. No heldout outcome used.'
            ),
            "heldout_used": False,
            "calibration_reports": [],
        },
        "analysis": {
            "control": "optimized",
            "candidates": ["baseline", "rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_native"],
            "metrics": [
                "throughput_per_min",
                "late_due_rate",
                "energy_per_completed",
                "measurement_energy_consumed",
                "mean_wip",
                "unfinished",
                "charge_queue_robot_seconds",
                "arrival_to_start_p95",
                "batch_makespan",
                "recorded_run_wall_seconds",
                "dispatch_timely_acceptance_rate",
                "dispatch_deadline_exceeded_rate",
                "dispatch_fallback_rate",
                "dispatch_latency_p95",
                "dispatch_latency_p99",
            ],
            "primary_metrics": ["throughput_per_min"],
            "practical_difference": {"throughput_per_min": 0.1},
            "guardrails": {
                "nominal_faults": 0,
                "nominal_rescue_energy": 0,
                "max_energy_balance_error": 1e-7,
                "late_due_rate_max_absolute_increase": 0.02,
                "control_coverage_required": True,
                "exposure": (
                    'report completed meaningful charge sessions and WIP trend per held-out arm; withhold a '
                    'self-sustaining claim if unexposed'
                ),
            },
            "selection_rule": (
                'No automatic recommendation. Main study needs independent layout/seed evidence; require '
                'primary practical improvement with no guardrail regression and report censored/failed '
                'arms. Pilot never declares a winner.'
            ),
            "confidence": 0.95,
            "bootstrap_resamples": 5000,
            "bootstrap_seed": 20261114,
            "minimum_seed_pairs": 10,
            "minimum_layouts": 3,
            "layout_sampling_independent": False,
            "hardware_id": "local_mac_arm64_synthetic_research",
        },
        "cases": [],
    }


def row(case: Scenario, metadata: dict[str, Any], phase: str, seed: int) -> dict[str, Any]:
    """Retain design lambda separately from realized releases and provisional mu."""
    references = synthetic_references()
    fixed = metadata["input_variation"] == "deterministic"
    lam = (
        len(case.workload["jobs"]) / case.duration
        if fixed
        else metadata["factors"].get(
            "arrival_rate_tasks_per_s", references["cases"][metadata["family"]]["workload"]["rate"]
        )
    )
    if "reference_rho" in metadata["factors"]:
        lam = references["reference_mu"][metadata["family"]] * metadata["factors"]["reference_rho"]
    return {
        "id": phase + "_" + metadata["cell"],
        "phase": phase,
        "condition": metadata["cell"],
        "layout": phase + "_geometry_" + metadata["cell"],
        "seed": seed,
        "stochastic": metadata["input_variation"] == "stochastic",
        "demand_generation": {
            "generator": "design-v3 materialize e1-candidate",
            "seed": seed,
            "design_hash": fingerprint(design),
            "reference_hash": fingerprint(synthetic_references()),
            "factors": metadata["factors"],
        },
        "scenario": case.to_dict(),
        "policies": metadata["policies"],
        "window": window(case, metadata, phase),
        "load": {
            "lambda_tasks_per_second": lam,
            "reference_capacity_tasks_per_second": 0.2,
            "calibration_status": "UNCALIBRATED",
            "realized_release_rate_tasks_per_second": len(case.workload["jobs"]) / case.duration,
            "method": (
                'UNCALIBRATED design mu 0.2 tasks/s; rho is provisional, not measured bottleneck '
                'capacity. Stochastic lambda follows the generator; finite-batch lambda is '
                'releases/duration.'
            ),
        },
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate synthetic study inputs; no simulation is run.")
    parser.add_argument("--output", type=Path, required=True)
    DOC = parser.parse_args().output
    design = json.loads((ROOT / "docs/scenarios/experiment_design_v3.json").read_text())
    chosen = (
        "S00_low",
        "S01_eligible_1",
        "S02_width_1p3",
        "S03_on",
        "S04_mixed_soc",
        "S05_hotspot_70",
        "S06_burst_2",
        "S07_speed_mix",
        "S08_two_cars",
        "S09_station_active",
        "S10_lambda_0p26",
        "S11_budget_2",
    )
    body = base_spec()
    for phase, seed, scale in [
        ("training", 11001, 1),
        ("validation", 12001, 1.05),
        ("heldout", 21001, 1.15),
        ("stress", 31001, 1.25),
    ]:
        for case, metadata in materialize(design, synthetic_references(), "e1-candidate", (seed,), chosen):
            modify_geometry(case, scale)
            case.id = phase + "_" + metadata["cell"]
            body["cases"].append(row(case, metadata, phase, seed))
    validate_spec(body)
    write(DOC / "development_spec.json", body)
    smoke = base_spec()
    selected = [
        ("training", 11001, "S01_eligible_1"),
        ("validation", 12001, "S04_mixed_soc"),
        ("heldout", 21001, "S08_two_cars"),
        ("stress", 31001, "S11_budget_2"),
    ]
    for phase, seed, cell in selected:
        case, metadata = materialize(design, synthetic_references(), "e0", (seed,), (cell,))[0]
        metadata["policies"] = ["baseline", "optimized", "rh_milp_grid", "rh_cpsat_linear", "rh_cpsat_native"]
        case.id = "smoke_" + cell
        case.duration = 90
        case.workload["jobs"] = [j for j in case.workload["jobs"] if j["at"] < 90]
        entry = row(case, metadata, phase, seed)
        entry["window"]["minimum_completed_charge_sessions_per_robot"] = 0
        entry["demand_generation"]["generator"] = "design-v3 materialize e0; common 90s prefix"
        smoke["cases"].append(entry)
    validate_spec(smoke)
    write(DOC / "smoke_spec.json", smoke)
    charge = Scenario.from_dict(synthetic_references()["cases"]["S04"])
    charge.id = "charge_exposure"
    charge.duration = 7800
    charge.warmup = 600
    charge.workload["rate"] = 0.04
    for station in charge.factory.stations:
        station.process_time_std = 0
    charge.initial_robots = {rid: replace(value, soc_pct=60) for rid, value in charge.initial_robots.items()}
    charge.validate()
    contract = window(charge, {"input_variation": "stochastic", "family": "S04"}, "training")
    contract["nominal"] = True
    write(DOC / "charge_scenario.json", charge.to_dict())
    write(DOC / "charge_window.json", contract)
    write(DOC / "d1_v2.json", body["decision_settings"])
    write(DOC / "rh_reference.json", body["planning_settings"])
    print(
        json.dumps(
            {
                "development_cases": len(body["cases"]),
                "smoke_runs": sum(len(c["policies"]) for c in smoke["cases"]),
                "development_windows": {
                    c["condition"]: [c["scenario"]["warmup"], c["scenario"]["duration"]]
                    for c in body["cases"]
                    if c["phase"] == "training"
                },
            },
            ensure_ascii=False,
        )
    )
