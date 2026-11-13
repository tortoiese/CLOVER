"""Projection for checking old physical event streams after adding measurements."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from simulation.telemetry import OBSERVATION_EVENTS


class PhysicalDigest:
    """Exclude only new observation events and renumber the remaining sequence."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()
        self.count = 0

    def record(self, row: dict[str, Any]) -> None:
        """Hash one projected row, keeping pre-existing event payloads intact."""
        if row["event"] in OBSERVATION_EVENTS:
            return
        projected = {k: v for k, v in row.items() if k not in {"sequence", "wall_seconds"}}
        projected["sequence"] = self.count
        self.digest.update(json.dumps(projected, sort_keys=True, allow_nan=False).encode() + b"\n")
        self.count += 1

    def hexdigest(self) -> str:
        """Return the current SHA256 for the complete observed prefix."""
        return self.digest.hexdigest()
