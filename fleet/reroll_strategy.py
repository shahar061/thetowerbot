"""Seed a worker's isolated Strategy page with the existing battle policies."""

from __future__ import annotations

from dataclasses import replace

from fleet.runtime import WorkerRuntime
from policy import AutopilotPolicy
from strategy import Claims, Strategy, StrategyStore


def ensure_reroll_strategy(runtime: WorkerRuntime) -> None:
    """Enable the existing navigator, battle policy and bounded Workshop visit once.

    A worker's own Strategy page owns subsequent edits. Never reset a saved
    profile when the coordinator or bot restarts.
    """
    store = StrategyStore(runtime.strategy_root)
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
                         coin_budget=None, coin_budget_pct=0.5, allow_unlocks=True),
        claims=Claims(enabled=True),
    ))
    store.set_active("reroll")
