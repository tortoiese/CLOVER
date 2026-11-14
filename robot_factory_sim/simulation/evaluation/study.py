"""Freeze synthetic/physical study inputs and execute phase-locked paired arms."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.decisions.contracts import DEADLINE_PROTOCOL, DecisionSettings
from simulation.evaluation.calibration import calibrate, validate_window
from simulation.evaluation.comparison_inputs import PLAN_SCHEMA, file_sha256, inside, read_json
from simulation.evaluation.runner import environment_manifest, run_suite, source_manifest
from simulation.evaluation.scenario import POLICIES, RH_POLICIES, Scenario, fingerprint, integer, make_trace, number
from simulation.planning.contracts import Settings

PHASES = ("training", "validation", "heldout", "stress")
FROZEN_SCHEMA = "clover-frozen-study-v1"
RUN_SCHEMA = "clover-study-runs-v1"
ANALYSIS_KEYS = ("control", "candidates", "metrics", "confidence", "bootstrap_resamples", "bootstrap_seed",
                 "minimum_seed_pairs", "minimum_layouts", "layout_sampling_independent", "hardware_id")


def save(path: Path, value: Any) -> None:
    """Checkpoint atomically without changing an earlier result file."""
    pending = path.with_suffix(path.suffix + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                       encoding="utf-8", newline="\n")
    pending.replace(path)


def code_identity() -> dict[str, Any]:
    """Verify every runtime/test Python file against Git HEAD blobs."""
    root = Path(__file__).resolve().parents[3]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        algorithm = subprocess.check_output(["git", "rev-parse", "--show-object-format"], cwd=root, text=True).strip()
        tree = subprocess.check_output(["git", "ls-tree", "-rz", "HEAD", "--", "robot_factory_sim"], cwd=root)
        blobs = {row.split(b"\t", 1)[1].decode(): row.split(b" ")[2].split(b"\t")[0].decode()
                 for row in tree.split(b"\0") if row}
        changed = []
        for name in source_manifest()["source_hashes"]:
            data = (root / "robot_factory_sim" / name).read_bytes()
            blob = hashlib.new(algorithm, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if blobs.get("robot_factory_sim/" + name) != blob:
                changed.append(name)
        return {"commit": commit, "python_matches_head": not changed, "changed_python": changed}
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return {"commit": None, "python_matches_head": False, "error": str(exc)}


def _body_hash(value: dict[str, Any]) -> str:
    return fingerprint({k: v for k, v in value.items() if k != "study_hash"})


def _geometry(case: Scenario) -> str:
    # Names, object IDs, viewport size and processing demand cannot make a map new.
    factory = case.factory
    nodes = {n.id: (float(n.x), float(n.y), n.floor, n.node_type, n.capacity) for n in factory.nodes}
    edges = []
    for edge in factory.edges:
        endpoints = [nodes[edge.from_node], nodes[edge.to_node]]
        if edge.is_bidirectional:
            endpoints.sort(key=lambda v: json.dumps(v))
        edges.append([endpoints, edge.is_bidirectional, float(edge.width),
                      float(edge.distance) if edge.distance is not None else None])
    profile = {k: float(getattr(factory.profile, k)) for k in (
        "floor_count", "edge_clearance", "side_clearance", "floor_travel_sec", "door_time_sec")}
    shafts = [[nodes[n.id] for n in factory.nodes if n.node_type == "elevator" and n.linked_station_id == sid]
              for sid in {n.linked_station_id for n in factory.nodes if n.node_type == "elevator"}]
    def order(values: Iterable[Any]) -> list[Any]:
        return sorted(values, key=lambda v: json.dumps(v, sort_keys=True))

    obstacles = [{k: v for k, v in asdict(o).items() if k not in {"id", "label"}} for o in factory.obstacles]
    return fingerprint({"profile": profile, "nodes": order(nodes.values()), "edges": order(edges),
                        "shafts": order([order(group) for group in shafts]), "obstacles": order(obstacles)})


def validate_spec(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject ambiguous units, phase leakage, unreviewed windows and load labels."""
    from simulation.evaluation.comparison import validate_plan

    if spec.get("schema") != "clover-study-spec-v1" or spec.get("stage") not in {"pilot", "confirmatory"}:
        raise ValueError("expected study spec with pilot/confirmatory stage")
    provenance = spec["provenance"]
    if provenance["kind"] not in {"synthetic_research", "team_factory"}:
        raise ValueError("explicit synthetic/physical provenance required")
    if not provenance.get("description") or not provenance.get("reviewed_by"):
        raise ValueError("input assumptions and reviewer are required")
    ds, math = DecisionSettings(**spec["decision_settings"]), Settings(**spec["planning_settings"])
    ds.validate()
    math.validate()
    if ds.timing_protocol != DEADLINE_PROTOCOL or math.timing_mode != "D0" or math.formulation != "grid":
        raise ValueError("study requires common staged D1 v2, with grid math settings")
    analysis = spec["analysis"]
    if (not analysis.get("primary_metrics") or set(analysis["primary_metrics"]) - set(analysis["metrics"])
            or not analysis.get("selection_rule") or not analysis.get("guardrails")):
        raise ValueError("preregister primary metrics, guardrails and practical selection rule")
    for metric in analysis["primary_metrics"]:
        number(analysis["practical_difference"][metric], "practical difference", .000001)
    selection = spec["selection"]
    if selection["method"] not in {"a_priori", "training_validation"} or not selection.get("rationale"):
        raise ValueError("settings require an a-priori or training/validation selection rationale")
    if selection.get("heldout_used") is not False:
        raise ValueError("held-out outcomes cannot select settings")
    seen, labels, physical_inputs, cases, blocks = set(), {}, {}, [], []
    geometries = {p: set() for p in PHASES}
    seeds = {p: set() for p in PHASES}
    for raw in spec["cases"]:
        row = copy.deepcopy(raw)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["id"]) or row["id"] in seen:
            raise ValueError("unique safe study case ids required")
        seen.add(row["id"])
        if row["phase"] not in PHASES:
            raise ValueError("unknown study phase")
        for field in ("condition", "layout"):
            if not isinstance(row[field], str) or not row[field]:
                raise ValueError("missing condition/layout label")
        integer(row["seed"], "study seed")
        case = Scenario.from_dict(row["scenario"])
        validate_window(case, row["window"])
        if row["phase"] == "stress" and row["window"]["nominal"]:
            raise ValueError("stress cases must declare non-nominal guardrails")
        if type(row["stochastic"]) is not bool:
            raise ValueError("stochastic status must be explicit")
        if row["stochastic"] and case.workload["kind"] == "trace" and not row.get("demand_generation"):
            raise ValueError("materialized stochastic traces require their generation recipe")
        load = row["load"]
        batch = load.get("kind") == "finite_batch"
        if batch:
            if (row["window"]["mode"] != "finite_batch" or load["lambda_tasks_per_second"] is not None
                    or load["reference_capacity_tasks_per_second"] is not None
                    or load["job_count"] != len(case.workload["jobs"])):
                raise ValueError("finite batch needs a job count, not a fictitious arrival rate/capacity")
            lam, mu = None, None
        else:
            lam = number(load["lambda_tasks_per_second"], "lambda")
            mu = number(load["reference_capacity_tasks_per_second"], "reference capacity", .000001)
        if not load.get("method") or (case.workload["kind"] == "poisson" and abs(lam - case.workload["rate"]) > 1e-12):
            raise ValueError("absolute load and its neutral capacity method must match input")
        if spec["stage"] == "confirmatory" and not batch and (
            load.get("calibration_status") not in {"REVIEWED_ANALYTICAL", "CALIBRATED"}
            or not load.get("capacity_evidence")
        ):
            raise ValueError("confirmatory requires reference capacity calibration/review and evidence")
        row["load"]["reference_rho"] = lam / mu if mu is not None else None
        trace = [asdict(t) for t in make_trace(case, row["seed"])]
        row.update(scenario=case.to_dict(), scenario_hash=fingerprint(case.to_dict()), trace_hash=fingerprint(trace),
                   geometry_hash=_geometry(case))
        physical = case.to_dict()
        physical.pop("id")
        physical["workload"] = {"kind": "trace", "jobs": trace}
        digest = fingerprint(physical)
        previous = physical_inputs.get(digest)
        if previous is not None:
            raise ValueError("duplicate physical input under new case/seed label: " + row["id"])
        physical_inputs[digest] = row["id"]
        row["physical_input_hash"] = digest
        if row["layout"] in labels and labels[row["layout"]] != row["geometry_hash"]:
            raise ValueError("one layout label refers to different geometry")
        labels[row["layout"]] = row["geometry_hash"]
        geometries[row["phase"]].add(row["geometry_hash"])
        seeds[row["phase"]].add(row["seed"])
        policies = row.get("policies", [analysis["control"], *analysis["candidates"]])
        if (len(policies) != len(set(policies)) or set(policies) - set(POLICIES)
                or analysis["control"] not in policies or len(policies) < 2
                or set(policies) - {analysis["control"], *analysis["candidates"]}):
            raise ValueError("invalid case-specific comparison policies")
        row["policies"] = policies
        blocks.append({"condition": row["id"], "layout": row["layout"], "replicate": str(row["seed"]),
                       "seed": row["seed"], "stochastic": row["stochastic"], "scenario": case.id,
                       "demand_kind": "finite_batch" if row["window"]["mode"] == "finite_batch" else "stream",
                       "suite": None, "unstarted_status": "NOT_STARTED",
                       "candidates": [p for p in policies if p != analysis["control"]]})
        cases.append(row)
    if not cases:
        raise ValueError("study has no cases")
    if geometries["heldout"] & (geometries["training"] | geometries["validation"]):
        raise ValueError("held-out geometry overlaps training/validation")
    if seeds["heldout"] & (seeds["training"] | seeds["validation"]):
        raise ValueError("held-out demand seeds overlap training/validation")
    if seeds["training"] & seeds["validation"]:
        raise ValueError("training and validation demand seeds overlap")
    if spec["stage"] == "confirmatory" and any(not geometries[p] for p in PHASES):
        raise ValueError("confirmatory protocol requires all four explicit phases")
    validate_plan({"schema": PLAN_SCHEMA, "purpose": "e0", **{k: analysis[k] for k in ANALYSIS_KEYS}, "blocks": blocks,
                   **{k: analysis[k] for k in ("effect_nulls", "multiplicity_scope", "inferential_metrics",
                                               "condition_effect_nulls", "condition_inferential_metrics")
                      if k in analysis}})
    return cases


def freeze_study(spec_path: Path, output: Path) -> dict[str, Any]:
    """Seal code, environment, all inputs/settings and predeclared analysis."""
    if output.exists():
        raise ValueError("frozen study output already exists")
    spec, digest = read_json(spec_path)
    cases = validate_spec(spec)
    if spec.get("main_study_design"):
        from simulation.evaluation.main_protocol import verify_main_design

        verify_main_design(spec, cases, spec_path.resolve().parent)
    identity, source = code_identity(), source_manifest()
    evidence = []
    for reference in spec["selection"].get("calibration_reports", []):
        path = (spec_path.resolve().parent / reference).resolve()
        report, sha = read_json(path)
        actual = calibrate(Path(report["run"]), report["window"])
        if actual != report or report["status"] != "PASS":
            raise ValueError("calibration evidence failed independent reconstruction")
        if report["source"]["source_hash"] != source["source_hash"]:
            raise ValueError("calibration used a different source")
        if report["seed"] in {r["seed"] for r in cases if r["phase"] in {"heldout", "stress"}}:
            raise ValueError("held-out/stress seeds cannot calibrate or select settings")
        evidence.append({"path": str(path), "sha256": sha, "report": report})
    if spec["stage"] == "confirmatory":
        if not identity["python_matches_head"]:
            raise ValueError("confirmatory code must match a committed Git revision")
        # Calibrate the observation requirements on development inputs only.
        # Held-out outcomes remain unseen; their faults/exposure are result guardrails.
        for row in cases:
            if row["phase"] not in {"training", "validation"}:
                continue
            if not any(e["report"]["scenario_hash"] == row["scenario_hash"]
                       and e["report"]["window"] == row["window"]
                       and e["report"].get("trace_hash") == row["trace_hash"]
                       and e["report"]["seed"] == row["seed"] for e in evidence):
                raise ValueError("training/validation case lacks passing window calibration: " + row["id"])
        if spec["selection"]["method"] == "training_validation":
            raise ValueError("data-driven profile selection needs a separate reviewed selection receipt; use a_priori")
    frozen = {"schema": FROZEN_SCHEMA, "spec_sha256": digest, "stage": spec["stage"],
              "provenance": spec["provenance"], "selection": spec["selection"], "evidence": evidence,
              "source": source, "code": identity, "environment": environment_manifest(),
              "planning_settings": asdict(Settings(**spec["planning_settings"])),
              "decision_settings": DecisionSettings(**spec["decision_settings"]).to_dict(),
              "analysis": spec["analysis"], "cases": cases,
              "execution": spec.get("execution"), "main_study_design": spec.get("main_study_design"),
              "scope": "synthetic research" if spec["provenance"]["kind"] == "synthetic_research" else "team inputs"}
    frozen["study_hash"] = _body_hash(frozen)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(frozen, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return frozen


def load_frozen(path: Path) -> tuple[dict[str, Any], str]:
    """Validate the sealed content before consuming a study."""
    frozen, sha = read_json(path)
    if frozen.get("schema") != FROZEN_SCHEMA or frozen.get("study_hash") != _body_hash(frozen):
        raise ValueError("frozen study content/hash mismatch")
    return frozen, sha


def run_study(frozen_path: Path, output: Path, *, phase: str,
              run_wall_limit: float | None = None, suite_wall_limit: float = 3600,
              shard_index: int = 0, shard_count: int = 1) -> dict[str, Any]:
    """Run every planned arm, retaining failures and unstarted rows explicitly."""
    frozen_path = frozen_path.resolve()
    frozen, sha = load_frozen(frozen_path)
    if phase not in PHASES:
        raise ValueError("unknown study execution phase")
    identity = code_identity()
    same_code = identity == frozen["code"] or (
        identity.get("python_matches_head") and frozen["code"].get("python_matches_head"))
    if (source_manifest() != frozen["source"] or environment_manifest() != frozen["environment"] or not same_code):
        raise ValueError("code/environment differs from frozen study")
    execution = frozen.get("execution") or {}
    if execution.get("require_readiness") and phase in {"heldout", "stress"}:
        ready_path = Path(execution["readiness_path"])
        if not ready_path.exists():
            raise ValueError("main study requires its verified readiness receipt")
        ready, _ = read_json(ready_path)
        if (ready.get("status") != "READY" or ready.get("frozen_sha256") != sha
                or ready.get("study_hash") != frozen["study_hash"]
                or file_sha256(Path(ready["canary_manifest"])) != ready["canary_manifest_sha256"]):
            raise ValueError("main readiness receipt differs or is no longer valid")
    if execution.get("hardware_id"):
        from simulation.evaluation.main_protocol import hardware_id

        if execution["hardware_id"] != hardware_id():
            raise ValueError("hardware differs from locked main-study host")
    locked_wall = execution.get("run_wall_limit_seconds")
    run_wall_limit = run_wall_limit if run_wall_limit is not None else (locked_wall or 240)
    if locked_wall is not None and run_wall_limit != locked_wall:
        raise ValueError("per-arm wall limit differs from registration")
    number(run_wall_limit, "run wall limit", .001)
    number(suite_wall_limit, "study wall limit", .001)
    phase_cases = [c for c in frozen["cases"] if c["phase"] == phase]
    integer(shard_count, "shard count", 1, max(1, len(phase_cases)))
    integer(shard_index, "shard index", 0, shard_count - 1)
    selected = [c for i, c in enumerate(phase_cases) if i % shard_count == shard_index]
    if not selected:
        raise ValueError("phase has no planned cases")
    output.mkdir(parents=True, exist_ok=False)
    entries = [{"id": c["id"], "status": "NOT_STARTED", "suite": None,
                "arms": [{"policy": p, "status": "NOT_STARTED"} for p in c["policies"]]} for c in selected]
    manifest = {"schema": RUN_SCHEMA, "status": "RUNNING", "phase": phase, "stage": frozen["stage"],
                "frozen": str(frozen_path), "frozen_sha256": sha, "study_hash": frozen["study_hash"],
                "planned_runs": sum(len(c["policies"]) for c in selected), "entries": entries,
                "phase_case_ids": [c["id"] for c in phase_cases],
                "shard_index": shard_index, "shard_count": shard_count,
                "execution_code": identity,
                "run_wall_limit": run_wall_limit, "suite_wall_limit": suite_wall_limit}
    manifest["owner_pid"] = os.getpid()
    try:
        import psutil

        manifest["owner_created_at"] = psutil.Process().create_time()
    except ImportError:
        manifest["owner_created_at"] = None
    path, started, stop = output / "manifest.json", time.perf_counter(), None
    save(path, manifest)
    for case, entry in zip(selected, entries):
        if stop is None and source_manifest() != frozen["source"]:
            stop = "SOURCE_CHANGED"
        if stop is None and file_sha256(frozen_path) != sha:
            stop = "FROZEN_INPUT_CHANGED"
        if stop or time.perf_counter() - started >= suite_wall_limit:
            entry["status"] = "NOT_RUN_" + (stop or "BUDGET")
            for arm in entry["arms"]:
                arm["status"] = entry["status"]
            save(path, manifest)
            continue
        entry["status"] = "RUNNING"
        entry["suite"] = case["id"] + "/manifest.json"
        for arm in entry["arms"]:
            arm["status"] = "PLANNED"
        save(path, manifest)
        try:
            result = run_suite((Scenario.from_dict(case["scenario"]),), (case["seed"],), output / case["id"],
                               policies=tuple(case["policies"]), run_wall_limit=run_wall_limit,
                               suite_wall_limit=max(.001, suite_wall_limit - (time.perf_counter() - started)),
                               planning_settings=frozen["planning_settings"],
                               decision_settings=frozen["decision_settings"],
                               compress_artifacts=execution.get("archive_artifacts", False))
            entry["status"] = result["status"]
            entry["arms"] = [{"policy": r["policy"], "status": r["status"], "failure": r.get("failure")}
                             for r in result["runs"]]
        except Exception as exc:
            stop = "EVALUATION_ERROR"
            entry["status"] = stop
            entry["failure"] = f"{type(exc).__name__}: {exc}"
            for arm in entry["arms"]:
                arm["status"] = "UNKNOWN_AFTER_EVALUATION_ERROR"
        save(path, manifest)
    unchanged = source_manifest() == frozen["source"] and file_sha256(frozen_path) == sha
    manifest["source_and_registration_unchanged"] = unchanged
    manifest["status"] = "SUCCESS" if unchanged and all(e["status"] == "SUCCESS" for e in entries) else "PARTIAL"
    manifest["wall_seconds"] = time.perf_counter() - started
    save(path, manifest)
    return manifest


def study_plan(manifest_path: Path) -> dict[str, Any]:
    """Derive the exact registered plan, including failed/unstarted arms."""
    manifest_path = manifest_path.resolve()
    parent, digest = read_json(manifest_path)
    if parent.get("schema") != RUN_SCHEMA or parent["phase"] not in PHASES:
        raise ValueError("expected phase-specific study run manifest")
    frozen, sha = load_frozen(Path(parent["frozen"]))
    if sha != parent["frozen_sha256"] or frozen["study_hash"] != parent["study_hash"]:
        raise ValueError("registered study changed after execution")
    locked_wall = (frozen.get("execution") or {}).get("run_wall_limit_seconds")
    if locked_wall is not None and parent.get("run_wall_limit") != locked_wall:
        raise ValueError("saved wall budget differs from main registration")
    phase_cases = [c for c in frozen["cases"] if c["phase"] == parent["phase"]]
    if "parts" in parent:
        from simulation.evaluation.study_shards import verify_collection

        verify_collection(manifest_path, parent)
        cases = phase_cases
    else:
        count, index = parent.get("shard_count", 1), parent.get("shard_index", 0)
        integer(count, "saved shard count", 1, len(phase_cases))
        integer(index, "saved shard index", 0, count - 1)
        if parent.get("phase_case_ids", [c["id"] for c in phase_cases]) != [c["id"] for c in phase_cases]:
            raise ValueError("saved phase matrix differs from registration")
        cases = [c for i, c in enumerate(phase_cases) if i % count == index]
    if ([e["id"] for e in parent["entries"]] != [c["id"] for c in cases]
            or parent["planned_runs"] != sum(len(c["policies"]) for c in cases)):
        raise ValueError("phase parent matrix differs from frozen study")
    blocks = []
    for case, entry in zip(cases, parent["entries"]):
        if entry.get("suite_origin"):
            from simulation.evaluation.study_shards import collected_suite

            suite = collected_suite(entry)
        else:
            suite = inside(manifest_path.parent, entry["suite"]) if entry["suite"] else None
        # An unexpected evaluator exception may leave an incomplete suite; keep
        # all planned arms visible rather than treating missing files as successes.
        if entry["status"] == "EVALUATION_ERROR" or (suite is not None and not suite.exists()):
            suite = None
        if suite is not None:
            suite_data, _ = read_json(suite)
            if (suite_data["source"] != frozen["source"] or list(suite_data["policies"]) != case["policies"]
                    or suite_data["scenario_hashes"] != {case["scenario"]["id"]: case["scenario_hash"]}
                    or suite_data["seeds"] != [case["seed"]]):
                raise ValueError("saved suite differs from preregistered case")
            for arm in suite_data["runs"]:
                if not arm.get("result"):
                    continue
                run, _ = read_json(inside(suite.parent, arm["result"]))
                math = {**frozen["planning_settings"],
                        "formulation": "native" if arm["policy"] == "rh_cpsat_native" else "grid"}
                if arm["policy"] not in RH_POLICIES:
                    math = asdict(Settings())
                if (run["trace_hash"] != case["trace_hash"] or run["environment"] != frozen["environment"]
                        or run["control"]["settings"] != frozen["decision_settings"]
                        or run["control"]["math_settings"] != math):
                    raise ValueError("run trace/environment/settings differ from preregistration")
        blocks.append({"condition": case["condition"], "layout": case["layout"], "replicate": str(case["seed"]),
                       "seed": case["seed"], "stochastic": case["stochastic"], "scenario": case["scenario"]["id"],
                       "demand_kind": "finite_batch" if case["window"]["mode"] == "finite_batch" else "stream",
                       "suite": str(suite) if suite is not None else None, "unstarted_status": entry["status"],
                       "inferential": case.get("inferential", True),
                       "candidates": [p for p in case["policies"] if p != frozen["analysis"]["control"]]})
    complete_phase = len(cases) == len(phase_cases)
    purpose = parent["phase"] if frozen["stage"] == "confirmatory" and complete_phase else "e0"
    return {"schema": PLAN_SCHEMA, "purpose": purpose, **{k: frozen["analysis"][k] for k in ANALYSIS_KEYS},
            **{k: frozen["analysis"][k] for k in ("effect_nulls", "multiplicity_scope", "inferential_metrics",
                                                "condition_effect_nulls", "condition_inferential_metrics")
               if k in frozen["analysis"]},
            "blocks": blocks, "registered_study": {"manifest": str(manifest_path), "sha256": digest,
                                                   "study_hash": frozen["study_hash"], "phase": parent["phase"]},
            "registered_criteria": {k: frozen["analysis"][k] for k in (
                "primary_metrics", "practical_difference", "guardrails", "selection_rule")},
            "condition_criteria": frozen["analysis"].get("condition_criteria"),
            "scope": "registered phase; synthetic pilot remains descriptive"}


def verify_registered_plan(plan: dict[str, Any]) -> None:
    """Reject post-outcome edits to metrics, arms, settings or phase labels."""
    reference = plan.get("registered_study")
    if not reference:
        raise ValueError("held-out/stress/validation analysis requires a registered study")
    expected = study_plan(Path(reference["manifest"]))
    if plan != expected or file_sha256(Path(reference["manifest"])) != reference["sha256"]:
        raise ValueError("analysis plan differs from frozen registration")
