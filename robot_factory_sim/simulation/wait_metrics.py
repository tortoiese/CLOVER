"""Observed wait distributions with explicit censoring and bounded sample counts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

import config

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def wait_distributions(engine: SimulationEngine) -> dict:
    """Return means, P95s and raw retained samples; empty observations stay null."""
    metrics = engine.metrics
    channels = {"arrival_to_start": list(metrics.wait_times), "station_queue": list(metrics.station_waits),
                "charger_queue": list(metrics.charge_waits), "corridor_queue": list(metrics.corridor_waits),
                "intersection_queue": list(metrics.intersection_waits), "elevator_queue": list(metrics.ev_waits),
                "arrival_to_assignment": [e["time"] - e["created"] for e in engine.task_events
                                          if e["event"] == "assigned"],
                "assignment_to_start": [e["time"] - e["assigned"] for e in engine.task_events
                                         if e["event"] == "started" and e["assigned"] is not None]}
    return {key: {"mean": sum(values) / len(values) if values else None,
                  "p95": float(np.quantile(values, .95)) if values else None,
                  "sample_count": len(values), "samples": values,
                  "basis": "observed admissions/starts; interrupted queues and unfinished jobs are censored",
                  "retention_limit": 50_000 if key in ("arrival_to_assignment", "assignment_to_start")
                  else config.METRIC_SAMPLE_LIMIT}
            for key, values in channels.items()}
