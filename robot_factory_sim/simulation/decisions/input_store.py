"""Content-addressed planning observations retained before worker requests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from simulation.artifacts import read_saved, stored_path
from simulation.decisions.contracts import digest
from simulation.planning.contracts import Snapshot
from simulation.planning.replay import decode_snapshot

INPUT_SCHEMA = "clover-planning-input-v1"


class PlanningInputStore:
    """Store each observation once, on disk or inline for in-memory runs.

    Args:
        root: Run artifact directory; None retains inline serializable evidence.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        self.records: dict[str, dict[str, Any]] = {}

    def put(self, snapshot: Snapshot) -> str:
        """Persist a detached snapshot before computation and return its hash."""
        body = {"schema": INPUT_SCHEMA, "snapshot": asdict(snapshot)}
        ref = digest(body)
        if ref in self.records:
            return ref
        if self.root is None:
            self.records[ref] = {"inline": body}
        else:
            relative = Path("planning_inputs") / f"{ref}.json"
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode()
            if stored_path(target).exists():
                if read_saved(target) != raw:
                    raise ValueError("planning input already exists with different content")
            else:
                temporary = target.with_suffix(".pending")
                with temporary.open("xb") as stream:
                    stream.write(raw)
                temporary.replace(target)
            self.records[ref] = {"path": relative.as_posix(), "file_sha256": hashlib.sha256(raw).hexdigest()}
        return ref


def load_planning_input(control: dict[str, Any], ref: str, root: Path | None = None) -> Snapshot:
    """Verify stored bytes and observation identity without requiring a plan.

    Args:
        control: Saved control object containing the content-addressed index.
        ref: Exact observation hash from a decision record.
        root: Directory containing a saved run, required for external entries.

    Returns:
        Detached, typed planning snapshot.
    """
    if not isinstance(ref, str) or len(ref) != 64 or any(c not in "0123456789abcdef" for c in ref):
        raise ValueError("invalid planning input reference")
    index = control["planning_inputs"]
    if digest(index) != control["planning_inputs_hash"]:
        raise ValueError("planning input index hash mismatch")
    entry = index[ref]
    if ("inline" in entry) == ("path" in entry):
        raise ValueError("ambiguous planning input storage")
    if "inline" in entry:
        body = entry["inline"]
    else:
        if root is None:
            raise ValueError("saved planning input requires the run directory")
        relative = Path(entry["path"])
        target = stored_path(root / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(root.resolve()):
            raise ValueError("planning input path escapes run directory")
        raw = read_saved(target)
        if hashlib.sha256(raw).hexdigest() != entry["file_sha256"]:
            raise ValueError("planning input file hash mismatch")
        body = json.loads(raw)
    if body.get("schema") != INPUT_SCHEMA or digest(body) != ref:
        raise ValueError("planning input content hash/schema mismatch")
    snapshot = decode_snapshot(body["snapshot"])
    snapshot.settings.validate()
    return snapshot
