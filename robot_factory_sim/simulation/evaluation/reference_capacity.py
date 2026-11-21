"""Empirical finite-window service reference, separate from physical maximum capacity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from simulation.artifacts import archive_run
from simulation.evaluation.calibration import calibrate
from simulation.evaluation.comparison_inputs import file_sha256, read_json
from simulation.evaluation.main_inputs import free_flow_cycle, layout, model_key, seed_for, window
from simulation.evaluation.runner import run_case, source_manifest
from simulation.evaluation.scenario import Scenario, fingerprint
from simulation.evaluation.study import save


def probe_case(family: str, phase: str, repeat: int) -> tuple[Scenario, int, dict[str, Any]]:
    """Offer twice the station service upper bound to a prespecified reference policy."""
    case, draw = layout(family, "calibration" if phase == "training" else "validation")
    seed = seed_for(family, "calibration" if phase == "training" else "validation", 90, repeat)
    case.id = f"capacity_{phase}_{family}_{repeat}"
    case.duration, case.warmup = 1800, 300
    case.check_interval = 10
    upper = sum(1 / max(.5, s.avg_process_time) for s in case.factory.stations if s.is_active)
    case.workload["rate"] = 2 * upper
    # S05's fixed destination mixture is part of its bottleneck definition.
    if family == "S05":
        from dataclasses import asdict

        from simulation.evaluation.scenario import make_trace

        jobs = [asdict(job) for job in make_trace(case, seed)]
        rng = np.random.default_rng([repeat, 551])
        for job in jobs:
            station = case.factory.stations[0 if rng.random() < .7 else int(rng.integers(1, 4))]
            job.update(station=station.id, allowed=[station.id])
        case.workload = {"kind": "trace", "jobs": jobs}
    case.validate()
    return case, seed, draw


def summarize_probe(report: dict[str, Any]) -> dict[str, Any]:
    """Require a continuously backlogged, audited final fixed window."""
    tail = report["segments"][-3:]
    rate = sum(s["completions"] for s in tail) / sum(s["end"] - s["start"] for s in tail)
    fleet = sum(r["is_active"] for r in report["scenario"]["factory"]["robots"])
    saturated = all(s["wip_before_start"] >= fleet and s["completions"] > 0 for s in tail)
    return {"rate_tasks_per_second": rate, "backlogged_final_bins": saturated,
            "audit_pass": report["status"] == "PASS", "faults": report["faults"],
            "rescue_energy": report["rescue_energy"], "segments": tail}


def family_statistics(probes: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate the fixed 3+2 probe matrix and recompute every stored estimate."""
    if (len(probes) != 5 or {(p["phase"], p["repeat"]) for p in probes}
            != {("training", 0), ("training", 1), ("training", 2), ("validation", 0), ("validation", 1)}):
        raise ValueError("capacity probe matrix is incomplete or duplicated")
    if len({p["seed"] for p in probes}) != 5:
        raise ValueError("capacity demand seeds overlap")
    row: dict[str, Any] = {"probes": probes}
    training = [p for p in row["probes"] if p["phase"] == "training"]
    row["training_rate"] = float(np.mean([p["rate_tasks_per_second"] for p in training]))
    row["training_rate_sd"] = float(np.std([p["rate_tasks_per_second"] for p in training], ddof=1))
    row["training_cycle_seconds"] = training[0]["free_flow_cycle_seconds"]
    ratios = []
    for probe in row["probes"]:
        if probe["phase"] == "validation":
            predicted = row["training_rate"] * row["training_cycle_seconds"] / probe["free_flow_cycle_seconds"]
            ratios.append(probe["rate_tasks_per_second"] / predicted if predicted > 0 else 0)
    row["validation_ratios"] = ratios
    valid = (all(p["audit_pass"] and p["backlogged_final_bins"] for p in row["probes"])
             and row["training_rate"] > 0 and row["training_rate_sd"] / row["training_rate"] <= .35
             and all(.67 <= ratio <= 1.5 for ratio in ratios))
    row["status"] = "PASS" if valid else "FAIL"
    return {k: v for k, v in row.items() if k != "probes"}


def calibrate_reference(output: Path, *, families: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Calibrate only development maps/seeds; never inspect held-out policy runs."""
    families = families or tuple(f"S{i:02d}" for i in range(12) if i != 1)
    if output.exists():
        raise ValueError("capacity output already exists")
    output.mkdir(parents=True)
    source = source_manifest()
    receipt = {"schema": "clover-reference-capacity-v1", "status": "RUNNING", "source": source,
               "reference_policy": "optimized", "timing": "D0", "training_repeats": 3, "validation_repeats": 2,
               "definition": "mean final-three-bin saturated finite-window completion rate; not physical optimum",
               "validation_ratio_bounds": [.67, 1.5], "families": {}, "planned_runs": len(families) * 5}
    save(output / "receipt.json", receipt)
    for family in families:
        row: dict[str, Any] = {"probes": [], "status": "RUNNING"}
        receipt["families"][family] = row
        for phase, repeats in (("training", 3), ("validation", 2)):
            for repeat in range(repeats):
                case, seed, draw = probe_case(family, phase, repeat)
                folder = output / family / f"{phase}_{repeat}"
                result = run_case(case, "optimized", seed, output=folder, wall_limit=180)
                run_path = folder / f"{result['case_id']}.json"
                run_path, _ = archive_run(run_path)
                report = calibrate(run_path, window(case, family))
                save(folder / "calibration.json", report)
                probe = {"phase": phase, "repeat": repeat, "seed": seed,
                         "run": str(run_path.resolve()), "result_sha256": file_sha256(run_path),
                         "report": str((folder / "calibration.json").resolve()),
                         "report_sha256": file_sha256(folder / "calibration.json"),
                         "draw": draw, "model_key": model_key(case), "free_flow_cycle_seconds": free_flow_cycle(case),
                         **summarize_probe(report)}
                row["probes"].append(probe)
                save(output / "receipt.json", receipt)
        row.update(family_statistics(row["probes"]))
        save(output / "receipt.json", receipt)
    receipt["status"] = "PASS" if all(r["status"] == "PASS" for r in receipt["families"].values()) else "FAIL"
    if source_manifest() != source:
        receipt["status"] = "SOURCE_CHANGED"
    receipt["receipt_hash"] = fingerprint({k: v for k, v in receipt.items() if k != "receipt_hash"})
    save(output / "receipt.json", receipt)
    return receipt


def verify_reference(path: Path) -> dict[str, Any]:
    """Rebuild every capacity calibration from raw evidence before accepting it."""
    receipt, _ = read_json(path)
    if (receipt.get("schema") != "clover-reference-capacity-v1" or receipt.get("status") != "PASS"
            or receipt.get("receipt_hash") != fingerprint({k: v for k, v in receipt.items() if k != "receipt_hash"})
            or receipt["source"] != source_manifest()):
        raise ValueError("unverified capacity reference/source")
    if not receipt["families"] or receipt["planned_runs"] != 5 * len(receipt["families"]):
        raise ValueError("capacity reference has an invalid planned matrix")
    for family_name, family in receipt["families"].items():
        if family_statistics(family["probes"]) != {k: v for k, v in family.items() if k != "probes"}:
            raise ValueError("capacity reference aggregates differ from probes")
        for probe in family["probes"]:
            report_path, run_path = Path(probe["report"]), Path(probe["run"])
            report, sha = read_json(report_path)
            expected, seed, draw = probe_case(family_name, probe["phase"], probe["repeat"])
            if (report["scenario"] != expected.to_dict() or report["seed"] != seed
                    or probe["draw"] != draw or probe["model_key"] != model_key(expected)
                    or probe["free_flow_cycle_seconds"] != free_flow_cycle(expected)
                    or report["source"] != receipt["source"]):
                raise ValueError("capacity probe geometry/seed/source differs")
            if (sha != probe["report_sha256"] or file_sha256(run_path) != probe["result_sha256"]
                    or calibrate(run_path, report["window"]) != report
                    or summarize_probe(report) != {k: probe[k] for k in summarize_probe(report)}):
                raise ValueError("capacity probe evidence changed")
    return receipt


def predict_reference(case: Scenario, family: str, receipt: dict[str, Any]) -> dict[str, Any]:
    """Transfer the fixed reference using input-only travel and processing geometry."""
    row = receipt["families"][family]
    cycle = free_flow_cycle(case)
    rate = row["training_rate"] * row["training_cycle_seconds"] / cycle
    return {"rate_tasks_per_second": rate, "free_flow_cycle_seconds": cycle,
            "training_rate_tasks_per_second": row["training_rate"],
            "anchor_model_key": model_key(case), "validation_ratios": row["validation_ratios"],
            "scope": "input-predicted finite-window reference, not measured held-out or physical maximum capacity"}
