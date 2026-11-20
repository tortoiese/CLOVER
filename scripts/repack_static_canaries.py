"""Copy completed technical evidence losslessly after an archive-codec-only revision."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import lzma
import time
from pathlib import Path
from typing import Any

from audit_korean_case_pilot import execution_lock, inside
from audit_static_scenario_study import audit_bundle
from materialize_static_scenario_study import file_sha
from run_static_scenario_study import open_campaign, record_metrics
from simulation.artifacts import open_saved, stored_path
from simulation.evaluation.study import save


def validate_storage_revision(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Permit a storage-only revision, never changed physics, solver settings or samples."""
    old, new = copy.deepcopy(before), copy.deepcopy(after)
    old["run_limits"].pop("archive_codec")
    new["run_limits"].pop("archive_codec")
    if old != new or after["run_limits"]["archive_codec"] != "xz":
        raise ValueError("only an archive codec change to XZ is eligible for evidence copying")


def transcode(source: Path, target: Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Stream one artifact, verifying stored and logical bytes before retaining the copy."""
    if file_sha(stored_path(source)) != expected["stored_sha256"]:
        raise ValueError("source stored hash mismatch")
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with open_saved(source) as original, lzma.open(target, "xb", preset=6) as compressed:
        for chunk in iter(lambda: original.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
            compressed.write(chunk)
    if digest.hexdigest() != expected["logical_sha256"] or size != expected["logical_bytes"]:
        raise ValueError("source logical hash/size mismatch; original evidence was not changed")
    verified, observed = hashlib.sha256(), 0
    with open_saved(target) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            verified.update(chunk)
            observed += len(chunk)
    if verified.hexdigest() != digest.hexdigest() or observed != size:
        raise ValueError("copied artifact does not preserve logical bytes")
    return {**expected, "stored_sha256": file_sha(target), "stored_bytes": target.stat().st_size}


def repack(old_bundle: Path, old_output: Path, new_bundle: Path, new_output: Path) -> dict[str, Any]:
    """Import exactly twenty successful M1 executions; no simulator or solver is invoked."""
    old_bundle, old_output, new_bundle, new_output = (
        p.resolve() for p in (old_bundle, old_output, new_bundle, new_output)
    )
    with execution_lock():
        old_audit, new_audit = audit_bundle(old_bundle), audit_bundle(new_bundle)
        if old_audit["source_hash"] != new_audit["source_hash"] or old_audit["tools"] != new_audit["tools"]:
            raise ValueError("application and operating tools must be byte-identical")
        before = json.loads((old_bundle / "design.json").read_text(encoding="utf-8"))
        after = json.loads((new_bundle / "design.json").read_text(encoding="utf-8"))
        validate_storage_revision(before, after)
        old = open_campaign(old_bundle, old_output)
        if any(j["status"] != "NOT_STARTED" for j in old["jobs"] if not j["phase"].startswith("M1")):
            raise ValueError("only a technical campaign without development/validation runs may be imported")
        technical = [j for j in old["jobs"] if j["phase"].startswith("M1")]
        if len(technical) != 5 or any(j["status"] != "SUCCESS" for j in technical):
            raise ValueError("all five paired technical blocks must be successful before copying")
        if new_output.exists():
            raise FileExistsError(new_output)
        new = open_campaign(new_bundle, new_output)
        receipt = {
            "schema": "clover-static-storage-copy-v1",
            "status": "COPYING",
            "tool_sha256": file_sha(Path(__file__)),
            "old_manifest_sha256": old_audit["manifest_sha256"],
            "new_manifest_sha256": new_audit["manifest_sha256"],
            "old_campaign_sha256": file_sha(old_output / "campaign.json"),
            "source_hash": old_audit["source_hash"],
            "copied_operating_runs": 20,
            "additional_solver_runs": 0,
            "artifacts": [],
            "scope": "same M1 executions with identical logical bytes; storage-only copies are not extra samples",
        }
        save(new_output / "storage_copy.json", receipt)
        started = time.perf_counter()
        for old_job in technical:
            new_job = new["jobs"][old_job["index"]]
            if any(
                old_job[k] != new_job[k]
                for k in ("scenario_hash", "sha256", "trace_hash", "initial_hash", "seed", "mode")
            ):
                raise ValueError("technical inputs changed across archive revision")
            old_root, new_root = old_output / old_job["directory"], new_output / new_job["directory"]
            suite = json.loads((old_root / "manifest.json").read_text(encoding="utf-8"))
            new_root.mkdir()
            for row in suite["runs"]:
                converted = {}
                for logical, expected in row["archive_files"].items():
                    source = inside(old_root, logical)
                    target = inside(new_root, logical + ".xz")
                    converted[logical] = transcode(source, target, expected)
                    receipt["artifacts"].append(
                        {
                            "job": old_job["index"],
                            "logical_path": logical,
                            "source_stored_sha256": expected["stored_sha256"],
                            **converted[logical],
                        }
                    )
                row["result"] = row["result"].removesuffix(".gz").removesuffix(".xz") + ".xz"
                row["events"] = row["events"].removesuffix(".gz").removesuffix(".xz") + ".xz"
                row["original_archive_bytes"] = row["archive_bytes"]
                row["archive_bytes"] = sum(r["stored_bytes"] for r in converted.values())
                row["archive_files"] = converted
                row["archive_storage_revision"] = "gzip-to-xz; original archive wall time retained"
            save(new_root / "manifest.json", suite)
            new_job.update(copy.deepcopy(old_job))
            rows = {r["policy"]: r for r in suite["runs"]}
            for arm in new_job["arms"]:
                arm.update(rows[arm["policy"]])
            new_job["evidence_origin"] = {"campaign_sha256": receipt["old_campaign_sha256"], "job": old_job["index"]}
            record_metrics(new_output, new_job)
            save(new_output / "campaign.json", new)
            save(new_output / "storage_copy.json", receipt)
            print(json.dumps({"copied_job": old_job["index"], "files": len(receipt["artifacts"])}), flush=True)
        receipt.update(status="VERIFIED", wall_seconds=time.perf_counter() - started)
        new.update(status="TECHNICAL_EVIDENCE_IMPORTED", storage_copy_receipt="storage_copy.json")
        save(new_output / "storage_copy.json", receipt)
        save(new_output / "campaign.json", new)
        return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("old-bundle", "old-output", "new-bundle", "new-output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    result = repack(args.old_bundle, args.old_output, args.new_bundle, args.new_output)
    print(json.dumps({"status": result["status"], "artifacts": len(result["artifacts"])}))
