"""M0 proof: control latency never publishes state early or hides an overrun."""

from __future__ import annotations

import time
from typing import Any

import pytest
import simpy

from simulation.decisions.timing import DelayLedger


def test_staged_calls_preserve_arrivals_and_prepay_publication() -> None:
    env, now, plant, observed = simpy.Environment(), [0.0], [], []
    ledger = DelayLedger(env, 0.1, lambda: now[0])

    def compute() -> str:
        now[0] += 0.5
        return "plan"

    def prepare() -> str:
        assert plant == ["arrival"]
        now[0] += 0.25
        return "transaction"

    def publish() -> None:
        assert env.now == pytest.approx(0.85) and not observed
        now[0] += 0.03
        observed.append((env.now, "applied"))

    def controller() -> Any:
        assert (yield from ledger.call_process("compute", compute)) == "plan"
        assert (yield from ledger.call_process("prepare", prepare)) == "transaction"
        yield from ledger.publish_process(publish)

    def arrival() -> Any:
        yield env.timeout(0.2)
        plant.append("arrival")

    env.process(controller())
    env.process(arrival())
    env.run(until=0.8)
    assert not observed and plant == ["arrival"]
    env.run()
    assert ledger.coverage_ok and observed[0][0] == pytest.approx(0.85)
    assert sum(r["wall_seconds"] for r in ledger.records) == pytest.approx(0.78)
    assert sum(r["injected_seconds"] for r in ledger.records) == pytest.approx(0.85)


def test_failure_waits_for_spent_cost_and_fallback_has_own_delay() -> None:
    env, now, events = simpy.Environment(), [0.0], []
    ledger = DelayLedger(env, 0.1, lambda: now[0])

    def failed() -> None:
        now[0] += 0.4
        raise RuntimeError("failed")

    def fallback() -> None:
        now[0] += 0.2

    def controller() -> Any:
        with pytest.raises(RuntimeError, match="failed"):
            yield from ledger.call_process("compute", failed)
        assert env.now == pytest.approx(0.4)
        yield from ledger.call_process("fallback", fallback)
        yield from ledger.publish_process(lambda: events.append(env.now))

    env.process(controller())
    env.run()
    assert events == [pytest.approx(0.7)]


def test_publication_detects_intervening_change_and_reports_overrun() -> None:
    env, now, state = simpy.Environment(), [0.0], {"version": 0}
    ledger = DelayLedger(env, 0.1, lambda: now[0])

    def publish() -> str:
        now[0] += 0.2
        assert state["version"] == 1
        return "STALE"

    def change() -> Any:
        yield env.timeout(0.05)
        state["version"] = 1

    result = env.process(ledger.publish_process(publish))
    env.process(change())
    env.run()
    assert result.value == "STALE" and not ledger.coverage_ok
    assert env.now == 0.1  # No retroactive correction conceals the overrun.


def test_recorded_clock_replay_and_trailing_phase_rejection() -> None:
    records = [
        {"name": "compute", "wall_seconds": 0.5, "injected_seconds": 0.5, "at": 0},
        {
            "name": "publication",
            "wall_seconds": 0.03,
            "injected_seconds": 0.1,
            "at": 0.5,
        },
    ]
    env = simpy.Environment()
    ledger = DelayLedger(env, 0.1, lambda: 0, records)

    def controller() -> Any:
        yield from ledger.call_process("compute", lambda: 1)
        with pytest.raises(ValueError, match="unconsumed"):
            ledger.verify_consumed()
        yield from ledger.publish_process(lambda: 2)
        ledger.verify_consumed()

    env.process(controller())
    env.run()
    assert env.now == 0.6 and ledger.records == records


def test_short_phase_survives_coarse_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows 3.11's coarse clock must not turn real short work into zero."""
    from simulation.planning.profiling import PhaseTimer
    samples = iter((100.0, 100.00001, 100.00002))
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "perf_counter", lambda: next(samples))
    timer = PhaseTimer()
    timer.start("short_stage")
    timer.stop()
    assert timer.snapshot()["short_stage"] == pytest.approx(.00001)


def test_sub_tick_publication_overrun_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twenty microseconds cannot fit a one-microsecond prepaid allowance."""
    samples = iter((100.0, 100.00002))
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "perf_counter", lambda: next(samples))
    env = simpy.Environment()
    ledger = DelayLedger(env, .000001)
    env.process(ledger.publish_process(lambda: None))
    env.run()
    assert not ledger.coverage_ok
    assert ledger.records[0]["wall_seconds"] == pytest.approx(.00002)
    assert env.now == pytest.approx(.000001)
