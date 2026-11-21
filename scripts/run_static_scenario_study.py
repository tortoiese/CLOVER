"""Run static-study stages serially, retaining failed and unstarted arms without retries."""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

from audit_korean_case_pilot import execution_lock, inside
from audit_static_scenario_study import audit_bundle
from materialize_static_scenario_study import (
    POLICIES,
    REPO,
    Scenario,
    code_identity,
    fingerprint,
    integer,
    tool_hashes,
)
from simulation.evaluation.comparison_inputs import load_arm, read_json
from simulation.evaluation.main_protocol import hardware_id
from simulation.evaluation.runner import environment_manifest, replay, run_suite
from simulation.evaluation.study import save
from static_scenario_metrics import summarize

PHASES = ("M1_D1", "M1_D0", "M2a", "M2b", "M3")


def frozen_code() -> dict[str, Any]:
    """Require committed application and orchestration bytes, without changing Git identity."""
    code = code_identity()
    if not code["python_matches_head"]:
        raise ValueError("commit reviewed runtime source before timed experiments")
    for name in tool_hashes():
        path = "scripts/" + name
        saved = subprocess.check_output(["git", "show", "HEAD:" + path], cwd=REPO)
        if saved != (REPO / path).read_bytes():
            raise ValueError("commit reviewed orchestration tool before timed experiments: " + name)
    return code


def open_campaign(bundle: Path, output: Path) -> dict[str, Any]:
    """Predeclare all 620 arms and bind a resumed campaign to identical inputs and host."""
    audit = audit_bundle(bundle)
    code = frozen_code()
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    identity = {
        "bundle_manifest_sha256": audit["manifest_sha256"],
        "source_hash": audit["source_hash"],
        "tools": audit["tools"],
        "hardware_id": hardware_id(),
        "environment": environment_manifest(),
    }
    path = output / "campaign.json"
    if path.exists():
        campaign = json.loads(path.read_text(encoding="utf-8"))
        if campaign["identity"] != identity:
            raise ValueError("campaign identity changed; preserve this campaign and use a new output path")
        if campaign["planned_runs"] != 620 or len(campaign["jobs"]) != len(manifest["entries"]):
            raise ValueError("campaign planned denominator changed")
        for job, entry in zip(campaign["jobs"], manifest["entries"], strict=True):
            if (
                {k: job[k] for k in entry} != entry
                or job["directory"] != f"job_{entry['index']:03d}"
                or [a["policy"] for a in job["arms"]] != POLICIES
            ):
                raise ValueError("campaign job or arm identity changed")
        return campaign
    output.mkdir(parents=True, exist_ok=False)
    campaign = {
        "schema": "clover-static-study-campaign-v1",
        "identity": identity,
        "code": code,
        "planned_runs": manifest["planned_runs"],
        "status": "PREPARED",
        "jobs": [],
    }
    for entry in manifest["entries"]:
        campaign["jobs"].append(
            {
                **entry,
                "directory": f"job_{entry['index']:03d}",
                "status": "NOT_STARTED",
                "arms": [{"policy": policy, "status": "NOT_STARTED", "result": None} for policy in POLICIES],
            }
        )
    save(path, campaign)
    return campaign


def sync_job(job: dict[str, Any], output: Path) -> None:
    """Import checkpoints after a crash; never re-execute an interrupted or failed slot."""
    path = output / job["directory"] / "manifest.json"
    if not path.exists():
        return
    suite = json.loads(path.read_text(encoding="utf-8"))
    rows = {r["policy"]: r for r in suite["runs"]}
    for arm in job["arms"]:
        row = rows.get(arm["policy"])
        if row:
            arm.update(row)
    if suite["status"] == "RUNNING":
        job["status"] = "INTERRUPTED"
    else:
        job["status"] = suite["status"]


def read_run(output: Path, job: dict[str, Any], arm: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Read saved evidence through a path confined to its individual suite."""
    run, digest = read_json(inside((output / job["directory"]).resolve(), arm["result"]))
    if (
        run["scenario_hash"] != job["scenario_hash"]
        or run["policy"] != arm["policy"]
        or run["seed"] != job["seed"]
        or bool(run.get("control")) != (job["mode"] == "D1")
    ):
        raise ValueError("saved result does not belong to this frozen input/seed/policy/timing arm")
    return run, digest


def record_metrics(output: Path, job: dict[str, Any]) -> None:
    """Derive small metrics only after workers exit, retaining a result-byte receipt."""
    for arm in job["arms"]:
        if arm.get("result"):
            run, digest = read_run(output, job, arm)
            arm["metrics"] = summarize(run)
            arm["result_sha256"] = digest


def readiness(bundle: Path, output: Path, *, verify_raw: bool = True) -> dict[str, Any]:
    """Compute gates from all 20 canary outcomes, raw audits and recorded replay evidence."""
    bundle, output = bundle.resolve(), output.resolve()
    campaign = open_campaign(bundle, output)
    technical = [j for j in campaign["jobs"] if j["phase"] in {"M1_D1", "M1_D0"}]
    issues, verified, commits, searches, replays, charge = [], [], Counter(), Counter(), {}, 0
    observed_due, observed_urgent = 0, 0
    d1_archive_bytes, logical_arm_bytes, run_wall = [], [], []
    for job in technical:
        if job["status"] not in {"SUCCESS", "PARTIAL"}:
            issues.append(f"{job['index']}: technical job {job['status']}")
        for arm in job["arms"]:
            label = f"{job['index']}:{arm['policy']}"
            if arm["status"] != "SUCCESS" or not arm.get("result"):
                issues.append(label + ": arm " + arm["status"])
                continue
            run, digest = read_run(output, job, arm)
            if digest != arm.get("result_sha256"):
                issues.append(label + ": result changed after metric collection")
            if verify_raw:
                validated = load_arm(
                    output / job["directory"] / "manifest.json", run["scenario"]["id"], job["seed"], arm["policy"]
                )
                if not validated["full_observation"]:
                    issues.append(label + ": raw evidence not complete")
                verified.append({"arm": label, "result_sha256": digest})
            metrics = summarize(run)
            run_wall.append(run["wall_seconds"])
            logical_arm_bytes.append(arm.get("logical_bytes", 0))
            if (
                not (run.get("static_layout_audit") or {}).get("ok")
                or not run["final_audit"]["ok"]
                or run["faults"]
                or run["energy"]["rescue"]
                or run["max_energy_error"] >= 1e-7
            ):
                issues.append(label + ": static, resource, task or energy conservation failed")
            for values in metrics["decision_summary"].get("by_kind", {}).values():
                if values["late_accepted"]:
                    issues.append(label + ": late primary acceptance")
            control = run.get("control")
            if control and any(not r["coverage_ok"] for r in control["decisions"]):
                issues.append(label + ": publication coverage violation")
            if job["phase"] == "M1_D1":
                d1_archive_bytes.append(arm.get("archive_bytes", 0))
                charge += run["measurement_energy"]["charged"] > 0
                observed_due += run["due_jobs"]
                observed_urgent += metrics["cohorts"]["urgent"]["due"]
                commits[arm["policy"]] += metrics["rh_execution"]["primary_prefix_commits"]
                searches[arm["policy"]] += metrics["rh_execution"]["reports_with_native_search"]
                # One independent recorded replay per policy; no fresh native optimization is timed here.
                if arm["policy"] not in replays and verify_raw:
                    repeated = replay(run, input_root=output / job["directory"])
                    replays[arm["policy"]] = repeated["matches"]
                    if not repeated["matches"]:
                        issues.append(label + ": recorded replay mismatch")
    for policy in ("rh_milp_grid", "rh_cpsat_linear"):
        if commits[policy] < 1:
            issues.append(policy + ": no primary plan prefix committed in D1 canaries")
        if searches[policy] < 1:
            issues.append(policy + ": no returned native search evidence in D1 canaries")
    if not charge or not observed_due or not observed_urgent:
        issues.append("measurement window did not expose charging and urgent/ordinary due cohorts")
    if not verify_raw:
        issues.append("raw event audits and recorded replays have not been executed")
    design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
    reserve = design["run_limits"]["disk_reserve_bytes"]
    free = shutil.disk_usage(output).free
    # Conservative planning estimate, not a guaranteed upper bound for unseen maps.
    estimated_remaining = int(max(d1_archive_bytes, default=0) * 600 * 1.5)
    scratch = max(logical_arm_bytes, default=0)
    if free < reserve + estimated_remaining + scratch:
        issues.append("estimated remaining archives plus scratch and disk reserve exceed free space")
    receipt = {
        "schema": "clover-static-study-readiness-v1",
        "status": "PASS" if not issues else "NOT_READY",
        "identity": campaign["identity"],
        "technical_planned_runs": 20,
        "verified_runs": verified,
        "primary_prefix_commits": dict(commits),
        "returned_native_search_reports": dict(searches),
        "replays": replays,
        "charge_exposed_d1_arms": charge,
        "due_jobs_across_d1_arms": observed_due,
        "urgent_due_jobs_across_d1_arms": observed_urgent,
        "issues": issues,
        "window_review": "charging and due cohorts observed; steady-state capacity not established",
        "resource_budget": {
            "free_disk_bytes": free,
            "reserved_bytes": reserve,
            "estimated_remaining_archive_bytes": estimated_remaining,
            "scratch_estimate_bytes": scratch,
            "estimate_method": "1.5 times maximum canary D1 stored arm size times 600; unseen-map size not guaranteed",
            "maximum_observed_run_wall_seconds": max(run_wall, default=None),
            "maximum_observed_d1_archive_bytes": max(d1_archive_bytes, default=None),
        },
        "scope": "technical readiness only; does not register thresholds, sample size or algorithm superiority",
    }
    receipt["receipt_hash"] = fingerprint(receipt)
    save(output / "readiness.json", receipt)
    return receipt


def check_gate(campaign: dict[str, Any], output: Path, phase: str) -> None:
    """Development requires audited canaries; validation additionally requires all development arms."""
    if phase.startswith("M1"):
        return
    receipt = json.loads((output / "readiness.json").read_text(encoding="utf-8"))
    content = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if (
        receipt["receipt_hash"] != fingerprint(content)
        or receipt["status"] != "PASS"
        or receipt["identity"] != campaign["identity"]
    ):
        raise ValueError("valid same-source technical readiness receipt required")
    evidence = {r["arm"]: r["result_sha256"] for r in receipt.get("verified_runs", [])}
    if len(evidence) != 20 or receipt.get("replays") != dict.fromkeys(POLICIES, True):
        raise ValueError("all twenty raw audits and four recorded replays required")
    for job in campaign["jobs"]:
        if job["phase"].startswith("M1"):
            for arm in job["arms"]:
                if arm["status"] != "SUCCESS" or read_run(output, job, arm)[1] != evidence.get(
                    f"{job['index']}:{arm['policy']}"
                ):
                    raise ValueError("technical evidence changed after readiness review")
    if phase == "M3" and any(j["status"] != "SUCCESS" for j in campaign["jobs"] if j["phase"] in {"M2a", "M2b"}):
        raise ValueError("complete successful development matrix required before map validation")


def run_phase(bundle: Path, output: Path, phase: str, *, limit: int | None = None) -> dict[str, Any]:
    """Execute only unstarted jobs under the shared host lock; preserve every other outcome."""
    bundle, output = bundle.resolve(), output.resolve()
    if phase not in PHASES:
        raise ValueError("unknown phase")
    if limit is not None:
        integer(limit, "job limit", 1)
    with execution_lock():
        campaign = open_campaign(bundle, output)
        for job in campaign["jobs"]:
            if job["status"] == "RUNNING":
                sync_job(job, output)
                job["status"] = "INTERRUPTED"
                record_metrics(output, job)
        check_gate(campaign, output, phase)
        design = json.loads((bundle / "design.json").read_text(encoding="utf-8"))
        limits = design["run_limits"]
        selected = [j for j in campaign["jobs"] if j["phase"] == phase and j["status"] == "NOT_STARTED"]
        random.Random("static-study-order-v1:" + phase).shuffle(selected)
        campaign["status"] = "RUNNING"
        save(output / "campaign.json", campaign)
        for job in selected[:limit]:
            # Audit before/after each paired block; no source or input mixing.
            audit_bundle(bundle)
            frozen_code()
            if shutil.disk_usage(output).free < limits["disk_reserve_bytes"]:
                campaign["status"] = "STOPPED_DISK_RESERVE"
                break
            job["status"] = "RUNNING"
            save(output / "campaign.json", campaign)
            started = time.perf_counter()
            try:
                case = Scenario.from_dict(json.loads(inside(bundle, job["scenario"]).read_text(encoding="utf-8")))
                run_suite(
                    (case,),
                    (job["seed"],),
                    output / job["directory"],
                    policies=tuple(POLICIES),
                    run_wall_limit=limits["run_wall_seconds"],
                    suite_wall_limit=limits["job_wall_seconds"],
                    planning_settings=design["rh_settings"],
                    decision_settings=design["d1_settings"] if job["mode"] == "D1" else None,
                    compress_artifacts=True,
                    archive_codec=limits["archive_codec"],
                    static_layout=True,
                )
                sync_job(job, output)
                record_metrics(output, job)
                audit_bundle(bundle)
                frozen_code()
            except Exception as exc:
                sync_job(job, output)
                job.update(status="INFRASTRUCTURE_FAILURE", failure=f"{type(exc).__name__}: {exc}")
                campaign["status"] = "STOPPED_INFRASTRUCTURE_FAILURE"
                save(output / "campaign.json", campaign)
                raise
            job["job_wall_seconds"] = time.perf_counter() - started
            job["free_disk_bytes_after"] = shutil.disk_usage(output).free
            save(output / "campaign.json", campaign)
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "index": job["index"],
                        "condition": job["condition"],
                        "seed": job["seed"],
                        "status": job["status"],
                        "wall_seconds": job["job_wall_seconds"],
                    }
                ),
                flush=True,
            )
            if job["status"] != "SUCCESS":
                campaign["status"] = "STOPPED_REQUIRES_REVIEW"
                break
        if campaign["status"] == "RUNNING":
            failed = any(
                j["phase"] == phase and j["status"] not in {"SUCCESS", "NOT_STARTED"} for j in campaign["jobs"]
            )
            campaign["status"] = (
                "STOPPED_REQUIRES_REVIEW"
                if failed
                else "COMPLETE"
                if all(j["status"] == "SUCCESS" for j in campaign["jobs"])
                else "STAGE_COMPLETE_OR_LIMIT"
            )
        campaign["counts"] = dict(Counter(a["status"] for j in campaign["jobs"] for a in j["arms"]))
        save(output / "campaign.json", campaign)
        return campaign


def report(bundle: Path, output: Path) -> dict[str, Any]:
    """Report all arms and within-input differences, preserving exploratory sample sizes."""
    bundle, output = bundle.resolve(), output.resolve()
    campaign = open_campaign(bundle, output)
    pairs = []
    for job in campaign["jobs"]:
        for arm in job["arms"]:
            if arm.get("result") and read_run(output, job, arm)[1] != arm.get("result_sha256"):
                raise ValueError("result evidence changed after collection")
        control = next(a for a in job["arms"] if a["policy"] == "optimized")
        for arm in job["arms"]:
            if arm["policy"] == "optimized":
                continue
            complete = arm["status"] == control["status"] == "SUCCESS"
            delta = {}
            for metric in ("throughput_per_min", "late_due_rate", "energy_per_completed", "backlog_slope_tasks_per_s"):
                a, b = arm.get("metrics", {}).get(metric), control.get("metrics", {}).get(metric)
                delta[metric] = a - b if complete and a is not None and b is not None else None
            pairs.append(
                {k: job[k] for k in ("phase", "layout", "condition", "seed")}
                | {
                    "policy": arm["policy"],
                    "control": "optimized",
                    "complete_pair": complete,
                    "arm_status": arm["status"],
                    "control_status": control["status"],
                    "difference": delta,
                }
            )
    result = {
        "schema": "clover-static-study-report-v1",
        "identity": campaign["identity"],
        "planned_runs": 620,
        "counts": dict(Counter(a["status"] for j in campaign["jobs"] for a in j["arms"])),
        "pairs": pairs,
        "confirmatory": False,
        "scope": ("M1 technical checks excluded from inference; M2/M3 five-seed exploratory differences only; "
                  "no algorithm winner declared"),
    }
    save(output / "report.json", result)
    lines = [
        "# CLOVER 고정 지도 실험 진행 보고서",
        "",
        "합성 연구환경이며 기존 실험과 합산하지 않습니다.",
        "",
        f"계획: 620회. 상태: `{json.dumps(result['counts'], ensure_ascii=False)}`.",
        "",
        "M1은 기술 검사, M2는 부하·충전·급증 비교, M3는 새 지도 검증입니다. 확인 실험 M4는 아직 등록하지 않았습니다.",
        "",
        "| 단계 | 지도 | 조건 | seed | 정책 | 상태 | 처리량(건/분) | 계획 반영 |",
        "|---|---|---|---|---|---|---:|---:|",
    ]
    for job in campaign["jobs"]:
        if job["status"] == "NOT_STARTED":
            continue
        for arm in job["arms"]:
            metrics = arm.get("metrics", {})
            lines.append(
                f"| {job['phase']} | {job['layout']} | {job['condition']} | {job['seed']} | "
                f"{arm['policy']} | {arm['status']} | {metrics.get('throughput_per_min')} | "
                f"{metrics.get('rh_execution', {}).get('primary_prefix_commits')} |"
            )
    lines += [
        "",
        ("`report.json`은 모든 미실행·실패 쌍을 포함합니다. 차이는 같은 입력에서 해당 정책−optimized입니다. "
         "음수·양수의 좋고 나쁨은 지표에 따라 다릅니다."),
        ("계획 반영 횟수는 작업 완료 수가 아닙니다. 반환된 실행 가능한 해와 실제 반영을 구분합니다. "
         "120초 초과 대기는 교착 검출이 아닙니다."),
        "",
    ]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main() -> None:
    """Expose explicit stage execution and a separately computed readiness gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=(*PHASES, "readiness", "report"), required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    if args.phase == "report":
        with execution_lock():
            result = report(args.bundle.resolve(), args.output.resolve())
        print(json.dumps(result["counts"]))
    elif args.phase == "readiness":
        with execution_lock():
            result = readiness(args.bundle.resolve(), args.output.resolve())
        print(json.dumps({k: v for k, v in result.items() if k != "verified_runs"}, ensure_ascii=False, indent=2))
    else:
        result = run_phase(args.bundle.resolve(), args.output.resolve(), args.phase, limit=args.limit)
        print(json.dumps({"status": result["status"], "counts": result["counts"]}))


if __name__ == "__main__":
    main()
