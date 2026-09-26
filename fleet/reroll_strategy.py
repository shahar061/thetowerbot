"""Seed a worker's isolated Strategy page with the existing battle policies."""

from __future__ import annotations

from dataclasses import replace

from fleet import reroll_timing
from fleet.reroll_timing import FleetTiming
from fleet.runtime import WorkerRuntime
from policy import AutopilotPolicy
from strategy import Claims, Strategy, StrategyStore


def ensure_reroll_strategy(runtime: WorkerRuntime, timing: FleetTiming | None = None) -> None:
    """Enable the existing navigator, battle policy and bounded Workshop visit once.

    A worker's own Strategy page owns subsequent edits. Never reset a saved
    profile when the coordinator or bot restarts. The one exception is the
    fleet-wide menu pace in `timing`, which is re-applied on every launch.
    """
    store = StrategyStore(runtime.strategy_root)
    _seed(store)
    if timing is not None:
        reroll_timing.apply(store, timing)


def _seed(store: StrategyStore) -> None:
    names = store.names()
    if names and not (names == ["default"]
                      and store.load("default") == Strategy.from_config("default")):
        return
    base = Strategy.from_config("reroll")
    store.save(replace(
        base,
        auto_navigate=True,
        autopilot=AutopilotPolicy(enabled=True, preset="turtle", purpose="milestone"),
        shopping=replace(base.shopping, enabled=True, armed=True,
                         coin_budget=None, coin_budget_pct=None, allow_unlocks=True),
        claims=Claims(enabled=True),
    ))
    store.set_active("reroll")
