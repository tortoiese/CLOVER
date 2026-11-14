"""Lossless archival, stored/logical digests, replay and independent plan export."""

from __future__ import annotations

import hashlib
import json
import lzma
import shutil
from pathlib import Path

import pytest

from simulation.artifacts import archive_run, compress_file, read_saved, stored_sha256
from simulation.decisions.input_store import PlanningInputStore, load_planning_input
from simulation.evaluation.analysis import analyze
from simulation.evaluation.comparison_inputs import load_arm, read_json
from simulation.evaluation.fixtures import joint_batch
from simulation.evaluation.frozen_snapshot import freeze_snapshot
from simulation.evaluation.profiling import profile_manifest
from simulation.evaluation.runner import replay, run_suite
from simulation.evaluation.test_snapshot_benchmark import saved as saved
from simulation.planning.contracts import Settings
from simulation.planning.snapshot import capture


@pytest.mark.parametrize("codec", ("gzip", "xz"))
def test_archive_keeps_original_on_conflicting_target(tmp_path: Path, codec: str) -> None:
    first = tmp_path / "data.txt"
    first.write_bytes(b"original" * 100)
    original = first.read_bytes()
    receipt = compress_file(first, codec)
    assert read_saved(first) == original
    assert receipt["logical_sha256"] == hashlib.sha256(original).hexdigest()
    assert receipt["stored_sha256"] == stored_sha256(first)
    first.write_bytes(b"different")
    with pytest.raises(ValueError, match="original preserved"):
        compress_file(first, codec)
    assert first.read_bytes() == b"different"


@pytest.mark.parametrize("codec", ("gzip", "xz"))
def test_archived_suite_raw_audit_profile_and_replay(tmp_path: Path, codec: str) -> None:
    suite = run_suite((joint_batch(),), (101,), tmp_path / "suite",
                      policies=("baseline", "optimized"), compress_artifacts=True, archive_codec=codec)
    assert suite["status"] == "SUCCESS"
    for row in suite["runs"]:
        path = tmp_path / "suite" / row["result"]
        assert path.suffix == (".gz" if codec == "gzip" else ".xz")
        run, sha = read_json(path)
        assert analyze(path)["input_result_sha256"] == sha
        assert replay(run, input_root=path.parent)["matches"]
        assert load_arm(tmp_path / "suite/manifest.json", row["scenario"], 101, row["policy"])["full_observation"]
    assert profile_manifest(tmp_path / "suite/manifest.json")["loaded_runs"] == 2


@pytest.mark.parametrize("codec", ("gzip", "xz"))
def test_compressed_cas_checks_logical_hash_and_path(tmp_path: Path, codec: str) -> None:
    from simulation.decisions.contracts import digest
    from simulation.engine import SimulationEngine

    engine = SimulationEngine(joint_batch().factory, mode="optimized")
    try:
        snapshot = capture(engine, Settings(), "cas-test")
        store = PlanningInputStore(tmp_path)
        ref = store.put(snapshot)
        path = tmp_path / store.records[ref]["path"]
        compress_file(path, codec)
        control = {"planning_inputs": store.records, "planning_inputs_hash": digest(store.records)}
        assert load_planning_input(control, ref, tmp_path) == snapshot
        assert PlanningInputStore(tmp_path).put(snapshot) == ref
        raw = path.with_name(path.name + (".gz" if codec == "gzip" else ".xz"))
        raw.write_bytes(raw.read_bytes()[:20])
        with pytest.raises((EOFError, OSError, lzma.LZMAError)):
            load_planning_input(control, ref, tmp_path)
    finally:
        engine.close()


@pytest.mark.parametrize("codec", ("gzip", "xz"))
def test_archived_real_rh_result_exports_same_model(saved: tuple, tmp_path: Path, codec: str) -> None:
    run_path, frozen_path, index = saved
    folder = tmp_path / "copy"
    shutil.copytree(run_path.parent, folder)
    copied = folder / run_path.name
    archived, receipt = archive_run(copied, codec)
    assert receipt["archive_bytes"] < receipt["logical_bytes"]
    frozen = freeze_snapshot(archived, index, tmp_path / "export.json")
    original = json.loads(frozen_path.read_text())
    assert frozen["model_hash"] == original["model_hash"]
    assert frozen["origin"]["run_sha256"] == stored_sha256(archived)


def test_urgent_sla_uses_input_priority_and_its_own_denominator(tmp_path: Path) -> None:
    case = joint_batch()
    case.workload["jobs"][0]["priority"] = 0
    for job in case.workload["jobs"]:
        job["deadline"] = 5
    suite = run_suite((case,), (103,), tmp_path / "urgent", policies=("baseline",), compress_artifacts=True)
    row = suite["runs"][0]
    arm = load_arm(tmp_path / "urgent/manifest.json", row["scenario"], 103, "baseline")
    assert arm["result"]["due_jobs"] == 2
    assert arm["result"]["urgent_due_jobs"] == 1
    assert arm["result"]["urgent_late_due_rate"] == 1
