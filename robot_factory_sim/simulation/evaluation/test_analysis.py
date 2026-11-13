"""Offline receipts must detect missing raw events and changed stored metrics."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from simulation.evaluation.__main__ import main
from simulation.evaluation.analysis import analyze
from simulation.evaluation.fixtures import joint_batch
from simulation.evaluation.runner import run_case


def test_offline_reconstruction_and_cli_match_live_observations(tmp_path: Path) -> None:
    result = run_case(joint_batch(), "optimized", 17, output=tmp_path)
    assert result["status"] == "SUCCESS"
    path = tmp_path / (result["case_id"] + ".json")
    checked = analyze(path)
    assert checked["stored_metrics_match"] and checked["events"] > 0
    output = tmp_path / "analysis.json"
    assert main(["analyze", "--run", str(path), "--output", str(output)]) == 0
    assert json.loads(output.read_text())["resource_waits"] == result["resource_waits"]


def test_truncated_stream_is_rejected_even_if_every_remaining_line_is_valid_json(tmp_path: Path) -> None:
    result = run_case(joint_batch(), "optimized", 17, output=tmp_path)
    path = tmp_path / (result["case_id"] + ".json")
    events = path.with_suffix(".events.jsonl")
    lines = events.read_text().splitlines()
    events.write_text("\n".join(lines[:-1]) + "\n")
    with pytest.raises(ValueError, match="digest mismatch"):
        analyze(path)


def test_changed_stored_statistics_are_reported_without_altering_raw_receipt(tmp_path: Path) -> None:
    result = run_case(joint_batch(), "optimized", 17, output=tmp_path)
    path = tmp_path / (result["case_id"] + ".json")
    result["resource_waits"]["total"]["queue_robot_seconds"] += 10
    path.write_text(json.dumps(result))
    assert not analyze(path)["stored_metrics_match"]
    assert main(["analyze", "--run", str(path)]) == 1
