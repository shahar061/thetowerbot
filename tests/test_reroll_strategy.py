"""Reroll workers use the existing editable battle and Workshop strategy."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fleet.reroll_strategy import ensure_reroll_strategy
from fleet.runtime import WorkerRuntime
from strategy import Strategy, StrategyStore


def test_new_worker_has_active_battle_and_bounded_workshop(tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    ensure_reroll_strategy(runtime)
    strategy = StrategyStore(runtime.strategy_root).ensure_seeded()
    assert strategy.name == "reroll"
    assert strategy.auto_navigate
    assert strategy.autopilot.enabled and strategy.autopilot.preset == "turtle"
    assert strategy.shopping.enabled and strategy.shopping.armed
    assert strategy.shopping.coin_budget is None
    assert strategy.shopping.coin_budget_pct == 0.5
    assert strategy.claims.enabled


def test_restart_preserves_worker_strategy_edits(tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    ensure_reroll_strategy(runtime)
    store = StrategyStore(runtime.strategy_root)
    edited = replace(store.load("reroll"), auto_navigate=False)
    store.save(edited)
    ensure_reroll_strategy(runtime)
    assert store.ensure_seeded() == edited


def test_existing_tuned_default_is_not_overwritten(tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    store = StrategyStore(runtime.strategy_root)
    tuned = replace(Strategy.from_config(), auto_navigate=True)
    store.save(tuned)
    ensure_reroll_strategy(runtime)
    assert store.ensure_seeded() == tuned
    assert store.names() == ["default"]
