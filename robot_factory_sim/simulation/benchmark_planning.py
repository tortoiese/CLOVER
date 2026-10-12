"""Common external arrival traces and task-keyed processing for offline comparisons."""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import Counter
from collections.abc import Generator
from datetime import datetime

import numpy as np

import config
from database.seed_data import build_default_config
from simulation.engine import SimulationEngine


def compare(duration: float = 120.0, seed: int = 42, rate: float = 0.08) -> dict[str, object]:
    """Run all modes on identical external task releases without queue-dependent dropping.

    Args:
        duration: Simulated seconds including a final drain window.
        seed: External trace and task-keyed processing seed.
        rate: Arrivals per simulated second before the drain window.

    Returns:
        Comparable counts, waiting KPIs and separate computational diagnostics.
    """
    if duration <= 0 or rate <= 0:
        raise ValueError("duration and rate must be positive")
    template = build_default_config()
    rng = np.random.default_rng(seed)
    trace: list[tuple[float, str]] = []
    now = 0.0
    while True:
        now += float(rng.exponential(1 / rate))
        if now >= duration * 0.75:
            break
        sid = template.stations[int(rng.integers(len(template.stations)))].id
        trace.append((now, sid))
    epoch = datetime(2026, 1, 1)
    results = {}
    for mode in config.SCHEDULER_MODES:
        engine = SimulationEngine(
            build_default_config(), mode, seed=seed, epoch=epoch, params={"background_enabled": 0.0}
        )
        engine.trace_processing = True

        def arrivals(target: SimulationEngine = engine) -> Generator[object, object, None]:
            for stamp, sid in trace:
                yield target.env.timeout(stamp - target.now)
                target.add_task("trace", sid, 2, allowed_stations=(sid,))

        engine.env.process(arrivals())
        started = time.monotonic()
        engine.env.run(until=duration)
        kpis = {k: float(v) if math.isfinite(v) else None for k, v in engine.kpis().items()}
        diagnostics = {}
        if engine.scheduler.planning_active:
            reports = engine.scheduler.stats.reports
            times = sorted(r.seconds for r in reports)
            diagnostics = {
                "statuses": dict(Counter(r.status for r in reports)),
                "mean_backend_seconds": sum(times) / len(times) if times else 0.0,
                "p95_backend_seconds": times[min(len(times) - 1, int(len(times) * 0.95))] if times else 0.0,
                "time_shifted_plans": 0,
            }
        results[mode] = {
            "received": len(trace),
            "completed": engine.metrics.total_completed,
            "unfinished": len(trace) - engine.metrics.total_completed,
            "pending": len(engine.pending_tasks),
            "wall_seconds": time.monotonic() - started,
            "kpis": kpis,
            "solver": diagnostics,
        }
    return {"duration": duration, "seed": seed, "rate": rate, "trace_count": len(trace), "results": results}


def main() -> None:
    """Print a machine-readable small benchmark, without saving a database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rate", type=float, default=0.08)
    args = parser.parse_args()
    print(json.dumps(compare(args.duration, args.seed, args.rate), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
