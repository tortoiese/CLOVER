"""Queue censoring and cabin movement accounting from raw execution events."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from simulation.evaluation.statistics import distribution


def overlap(start: float, end: float, warmup: float, cutoff: float) -> float:
    """Return the intersection length with the measurement window."""
    return max(0.0, min(end, cutoff) - max(start, warmup))


class ResourceMetrics:
    """Keep open episodes, observed end samples and exact clipped exposure."""

    def __init__(self, warmup: float, end: float, threshold: float) -> None:
        self.warmup, self.end, self.threshold = warmup, end, threshold
        self.waits: dict[str, dict[str, Any]] = {}
        self.wait_closed: list[tuple[str, str, float, float, str]] = []
        self.phases: dict[str, dict[str, Any]] = {}
        self.phase_closed: list[tuple[dict[str, Any], float, float, bool]] = []

    def record(self, row: dict[str, Any]) -> None:
        """Accept one start/end; reject missing, duplicate or inconsistent records."""
        event = row["event"]
        if event == "resource_wait_start":
            identity = row["wait_id"]
            if identity in self.waits:
                raise ValueError("duplicate active wait")
            self.waits[identity] = dict(row)
        elif event == "resource_wait_end":
            start = self.waits.pop(row["wait_id"])
            if any(start[k] != row[k] for k in ("robot", "robot_instance", "channel", "resource")):
                raise ValueError("wait identity changed")
            if row["outcome"] not in {"granted", "withdrawn", "failed", "shutdown"}:
                raise ValueError("unknown wait outcome")
            elapsed = row["time"] - start["time"]
            if elapsed < 0 or abs(elapsed - row["elapsed"]) > 1e-7:
                raise ValueError("wait elapsed mismatch")
            if row["time"] >= self.warmup:
                self.wait_closed.append(
                    (start["channel"], start["resource"], start["time"], row["time"], row["outcome"])
                )
        elif event == "cabin_phase_start":
            identity = row["operation_id"]
            if identity in self.phases:
                raise ValueError("duplicate active cabin phase")
            if any(
                r["cabin"] == row["cabin"] and r["cabin_instance"] == row["cabin_instance"]
                for r in self.phases.values()
            ):
                raise ValueError("overlapping phases in one cabin")
            if row["kind"] not in {"empty_move", "loaded_move", "boarding", "alighting", "other_move"}:
                raise ValueError("unknown cabin phase")
            self.phases[identity] = dict(row)
        elif event == "cabin_phase_end":
            start = self.phases.pop(row["operation_id"])
            if any(start[k] != row[k] for k in ("cabin", "cabin_instance", "kind", "origin", "destination")):
                raise ValueError("cabin phase identity changed")
            if abs(row["time"] - start["time"] - row["elapsed"]) > 1e-7 or row["elapsed"] < 0:
                raise ValueError("cabin elapsed mismatch")
            if abs(abs(row["actual_destination"] - start["origin"]) - row["distance_floors"]) > 1e-7:
                raise ValueError("cabin distance mismatch")
            if row["completed"] and abs(row["actual_destination"] - start["destination"]) > 1e-7:
                raise ValueError("completed cabin phase missed destination")
            if row["time"] >= self.warmup:
                self.phase_closed.append((start, row["time"], row["distance_floors"], row["completed"]))

    def _queue(
        self, closed: list[tuple[float, float, str]], active: list[dict[str, Any]], cutoff: float
    ) -> dict[str, Any]:
        cohorts = [(start, end, outcome) for start, end, outcome in closed if self.warmup <= start < cutoff]
        active_window = [r for r in active if r["time"] < cutoff and cutoff > self.warmup]
        active_cohort = [r for r in active_window if r["time"] >= self.warmup]
        outcomes = Counter(outcome for _, _, outcome in cohorts)
        exposure = sum(overlap(start, end, self.warmup, cutoff) for start, end, _ in closed)
        exposure += sum(overlap(r["time"], cutoff, self.warmup, cutoff) for r in active_window)
        return {
            "cohort_episodes": len(cohorts) + len(active_cohort),
            "cohort_granted": outcomes["granted"],
            "cohort_withdrawn": sum(n for outcome, n in outcomes.items() if outcome != "granted"),
            "cohort_right_censored": len(active_cohort),
            "closed_outcomes": dict(outcomes),
            "granted_wait": distribution([end - start for start, end, outcome in cohorts if outcome == "granted"]),
            "withdrawn_elapsed": distribution([end - start for start, end, outcome in cohorts if outcome != "granted"]),
            "right_censored_ages": distribution([cutoff - r["time"] for r in active_cohort]),
            "carry_in_episodes": sum(start < self.warmup < end for start, end, _ in closed)
            + sum(r["time"] < self.warmup for r in active_window),
            "active_at_end": len(active_window),
            "active_over_threshold": sum(cutoff - r["time"] > self.threshold for r in active_window),
            "queue_robot_seconds": exposure,
            "mean_queue_length": exposure / (cutoff - self.warmup) if cutoff > self.warmup else None,
        }

    def waits_snapshot(self, observed: float) -> dict[str, Any]:
        """Keep unfinished episodes visible instead of inserting them as completions."""
        cutoff = min(observed, self.end)
        by_queue: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
        by_channel: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
        open_queue: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        open_channel: dict[str, list[dict[str, Any]]] = defaultdict(list)
        active_rows = []
        for channel, resource, start, end, outcome in self.wait_closed:
            by_queue[channel, resource].append((start, end, outcome))
            by_channel[channel].append((start, end, outcome))
        for row in self.waits.values():
            open_queue[row["channel"], row["resource"]].append(row)
            open_channel[row["channel"]].append(row)
            active_rows.append(
                {k: v for k, v in row.items() if k not in {"event", "sequence", "time"}}
                | {
                    "started": row["time"],
                    "age": max(0, cutoff - row["time"]),
                    "age_in_window": overlap(row["time"], cutoff, self.warmup, cutoff),
                    "started_in_window": self.warmup <= row["time"] < cutoff,
                }
            )
        queues = [
            {"channel": key[0], "resource": key[1], **self._queue(by_queue[key], open_queue[key], cutoff)}
            for key in sorted(by_queue.keys() | open_queue.keys())
        ]
        channels = {
            key: self._queue(by_channel[key], open_channel[key], cutoff)
            for key in sorted(by_channel.keys() | open_channel.keys())
        }
        closed = [(start, end, outcome) for _, _, start, end, outcome in self.wait_closed]
        total = self._queue(closed, list(self.waits.values()), cutoff)
        assert (
            total["cohort_episodes"]
            == total["cohort_granted"] + total["cohort_withdrawn"] + total["cohort_right_censored"]
        )
        return {
            "window": [min(self.warmup, cutoff), cutoff],
            "configured_start": self.warmup,
            "threshold_seconds": self.threshold,
            "cohort": "episodes starting in measurement window; carry-in contributes exposure separately",
            "total": total,
            "by_channel": channels,
            "by_queue": queues,
            "open_episodes": active_rows,
            "interpretation": "granted distributions exclude censored/withdrawn waits; long wait is not deadlock",
        }

    def cabins_snapshot(self, observed: float) -> dict[str, Any]:
        """Include partial ongoing movement and clip realized travel to the window."""
        cutoff = min(observed, self.end)
        cells: dict[tuple[str, int, str], dict[str, Any]] = {}

        def cell(row: dict[str, Any]) -> dict[str, Any]:
            key = row["cabin"], row["cabin_instance"], row["kind"]
            if key not in cells:
                cells[key] = {
                    "cabin": key[0],
                    "cabin_instance": key[1],
                    "kind": key[2],
                    "seconds": 0.0,
                    "distance_floors": 0.0,
                    "completed": 0,
                    "interrupted": 0,
                    "active": 0,
                }
            return cells[key]

        for row, end, distance, completed in self.phase_closed:
            duration = end - row["time"]
            spent = overlap(row["time"], end, self.warmup, cutoff)
            stats = cell(row)
            stats["seconds"] += spent
            stats["distance_floors"] += (
                distance * spent / duration if duration > 0 else distance if self.warmup <= row["time"] < cutoff else 0
            )
            if self.warmup <= end <= cutoff:
                stats["completed" if completed else "interrupted"] += 1
        active_rows = []
        for row in self.phases.values():
            elapsed = max(0, cutoff - row["time"])
            spent = overlap(row["time"], cutoff, self.warmup, cutoff)
            duration = row["planned_seconds"]
            distance = abs(row["destination"] - row["origin"])
            position_fraction = min(1, elapsed / duration) if duration > 0 else 1
            before_window = min(elapsed, max(0, self.warmup - row["time"]))
            window_fraction = (
                max(0, position_fraction - min(1, before_window / duration))
                if duration > 0
                else int(self.warmup <= row["time"] < cutoff)
            )
            stats = cell(row)
            stats["seconds"] += spent
            stats["distance_floors"] += distance * window_fraction
            stats["active"] += int(row["time"] < cutoff and cutoff > self.warmup)
            active_rows.append(
                {k: v for k, v in row.items() if k not in {"event", "sequence", "time"}}
                | {
                    "started": row["time"],
                    "elapsed": elapsed,
                    "elapsed_in_window": spent,
                    "traveled_floors": distance * position_fraction,
                    "current_floor": row["origin"] + (row["destination"] - row["origin"]) * position_fraction,
                }
            )
        totals: dict[str, dict[str, float]] = {}
        for stats in cells.values():
            target = totals.setdefault(
                stats["kind"], {key: 0 for key in ("seconds", "distance_floors", "completed", "interrupted", "active")}
            )
            for key in target:
                target[key] += stats[key]
        empty = totals.get("empty_move", {}).get("distance_floors", 0)
        loaded = totals.get("loaded_move", {}).get("distance_floors", 0)
        return {
            "window": [min(self.warmup, cutoff), cutoff],
            "configured_start": self.warmup,
            "by_cabin_phase": list(cells.values()),
            "totals": totals,
            "active_at_end": active_rows,
            "empty_distance_fraction": empty / (empty + loaded) if empty + loaded else None,
            "distance_unit": "floor intervals; no assumed height, energy or power conversion",
        }
