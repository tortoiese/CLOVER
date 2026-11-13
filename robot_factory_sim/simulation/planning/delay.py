"""Revalidate a delayed proposal without claiming its old bound for the new plant."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from simulation.planning.contracts import Result
from simulation.planning.execution import prefixes
from simulation.planning.snapshot import capture, prepare

if TYPE_CHECKING:
    from simulation.engine import SimulationEngine


def rebase(engine: SimulationEngine, original: Result) -> tuple[Result | None, str]:
    """Shift only unchanged idle actors, retaining the request's visible job set.

    New arrivals are not supplied retroactively to the old optimization. A
    changed candidate window rejects the proposal and invokes current-state
    fallback. This conservative prototype never mixes two observation sets.
    """
    before = original.input.snapshot
    current = capture(engine, before.settings, before.token)
    if (
        current.epoch != before.epoch
        or current.layout_version != before.layout_version
        or current.topology != before.topology
    ):
        return None, "STALE_LAYOUT_OR_EPOCH"
    old_actors = {r.id: r for r in before.robots if r.available}
    current_actors = {r.id: r for r in current.robots}
    if any(current_actors.get(rid) != actor for rid, actor in old_actors.items()):
        return None, "STALE_ROBOT"
    if current.jobs != before.jobs or current.pending_count != before.pending_count:
        return None, "STALE_JOB_SET"
    if any(r.available and r.id not in old_actors for r in current.robots):
        return None, "STALE_AVAILABILITY"
    delay = engine.now - before.now
    if any(
        a.arc.kind != "wait" and a.arc.start * before.settings.time_unit + delay >= before.settings.horizon
        for a in prefixes(original)
    ):
        return None, "STALE_HORIZON"
    prepared = prepare(current)
    if prepared.routes != original.input.routes:
        return None, "STALE_ROUTE_OR_CABIN"
    report = replace(
        original.report,
        status="FEASIBLE",
        model_hash="",
        semantic_hash="",
        semantic_spec_hash="",
        phase1_status="REVALIDATED",
        phase1_proof="NONE",
        phase2_status="REVALIDATED",
        phase1_bound=None,
        phase2_bound=None,
        phase1_cost=None,
        phase2_cost=None,
        message="shifted execution proposal; original optimization bound applies only to request snapshot",
    )
    return replace(original, input=prepared, report=report), "REVALIDATED"
