"""Serializable decision identities; no plant or optional solver imports."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any

PROTOCOL = "d1-staged-budget-v1"
POLICIES = ("baseline", "optimized", "assignment_milp_legacy")


def digest(value: Any) -> str:
    """Hash a JSON value independently of dictionary insertion order."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class DecisionSettings:
    """Explicit latency/lifecycle settings, separate from the RH math settings."""

    timing_protocol: str = PROTOCOL
    startup_mode: str = "on_demand"
    startup_timeout_seconds: float = 10
    end_to_end_soft_deadline_seconds: float = 10
    publication_allowance_seconds: float = 0.1
    max_admission_attempts: int = 2
    max_worker_restarts: int = 2
    restart_backoff_seconds: tuple[float, ...] = (0.1, 1.0)
    fallback_budget_seconds: float = 0.1
    memory_limit_mib: float = 1024
    request_watchdog_seconds: float = 12

    def validate(self) -> None:
        """Reject ambiguous timing modes, nonfinite costs and unbounded retries."""
        if self.timing_protocol != PROTOCOL or self.startup_mode not in {
            "on_demand",
            "eager",
        }:
            raise ValueError("unsupported decision timing/startup mode")
        for key in (
            "startup_timeout_seconds",
            "end_to_end_soft_deadline_seconds",
            "publication_allowance_seconds",
            "fallback_budget_seconds",
            "memory_limit_mib",
            "request_watchdog_seconds",
        ):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid decision setting: " + key)
        for key in ("max_admission_attempts", "max_worker_restarts"):
            value = getattr(self, key)
            if type(value) is not int or not (0 <= value <= 100) or (key == "max_admission_attempts" and value == 0):
                raise ValueError("invalid bounded attempts: " + key)
        if len(self.restart_backoff_seconds) != self.max_worker_restarts:
            raise ValueError("one backoff duration is required per restart")
        for value in self.restart_backoff_seconds:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("invalid restart backoff")

    def to_dict(self) -> dict[str, Any]:
        """Return the manifest representation."""
        return asdict(self)


@dataclass(frozen=True)
class Intent:
    """A proposed action; it owns no live robot, task, resource or process."""

    kind: str
    robot: str
    job: str | None = None
    destination: str | None = None
    target: float | None = None


@dataclass(frozen=True)
class Observation:
    """Immutable JSON observation of visible plant and policy state."""

    epoch: str
    layout: int
    policy: str
    token: str
    now: float
    payload: str

    @property
    def hash(self) -> str:
        """Identify the complete immutable input, including its observation time."""
        return digest(asdict(self))


@dataclass(frozen=True)
class Proposal:
    """Actions and staged scheduler state selected from one frozen observation."""

    observation: Observation
    intents: tuple[Intent, ...]
    state: str
    diagnostics: str = "{}"


def encode(proposal: Proposal) -> dict[str, Any]:
    """Serialize a detached proposal for run artifacts and exact replay."""
    return asdict(proposal)


def decode(value: dict[str, Any]) -> Proposal:
    """Restore typed values, retaining no mutable handles from saved data."""
    return Proposal(
        Observation(**value["observation"]),
        tuple(Intent(**i) for i in value["intents"]),
        value["state"],
        value["diagnostics"],
    )
