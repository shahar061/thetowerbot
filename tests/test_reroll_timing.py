"""One fleet-wide menu pace, applied to every reroll worker's profile."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from fleet import reroll_timing
from fleet.reroll_strategy import ensure_reroll_strategy
from fleet.reroll_timing import FleetTiming
from fleet.runtime import WorkerRuntime
from strategy import ControlError, StrategyStore


def test_no_file_means_the_fleet_has_not_chosen(tmp_path: Path) -> None:
    assert reroll_timing.load(tmp_path) is None


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    reroll_timing.save(tmp_path, FleetTiming(menu_interval=0.7))
    assert reroll_timing.load(tmp_path) == FleetTiming(menu_interval=0.7)


def test_an_unreadable_file_is_treated_as_unset(tmp_path: Path) -> None:
    (tmp_path / reroll_timing.FILE_NAME).write_text("{not json")
    assert reroll_timing.load(tmp_path) is None


@pytest.mark.parametrize("value", [0.0, 3601.0, True, "fast"])
def test_menu_interval_is_validated_like_the_strategy_field(value: object) -> None:
    with pytest.raises(ControlError) as caught:
        FleetTiming(menu_interval=value)  # type: ignore[arg-type]
    assert caught.value.field == "menu_interval"


def test_a_new_worker_launches_with_the_fleet_pace(tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    ensure_reroll_strategy(runtime, FleetTiming(menu_interval=0.6))
    assert StrategyStore(runtime.strategy_root).ensure_seeded().menu_interval == 0.6


def test_an_existing_worker_takes_the_fleet_pace_and_keeps_its_other_edits(
        tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    ensure_reroll_strategy(runtime)
    store = StrategyStore(runtime.strategy_root)
    store.save(replace(store.load("reroll"), auto_navigate=False))

    ensure_reroll_strategy(runtime, FleetTiming(menu_interval=0.6))

    active = store.ensure_seeded()
    assert active.menu_interval == 0.6
    assert active.auto_navigate is False


def test_without_a_fleet_pace_a_worker_keeps_its_own(tmp_path: Path) -> None:
    runtime = WorkerRuntime.for_worker(tmp_path, "Tiramisu64_20", 10020)
    ensure_reroll_strategy(runtime)
    store = StrategyStore(runtime.strategy_root)
    store.save(replace(store.load("reroll"), menu_interval=1.3))
    ensure_reroll_strategy(runtime, None)
    assert store.ensure_seeded().menu_interval == 1.3


def test_push_patches_each_running_worker_and_reports_the_unreachable() -> None:
    sent: list[tuple[int, dict]] = []

    def patch(port: int, body: dict) -> None:
        if port == 10002:
            raise OSError("connection refused")
        sent.append((port, body))

    unreachable = reroll_timing.push([10001, 10002], FleetTiming(menu_interval=0.5),
                                     patch=patch)
    assert sent == [(10001, {"menu_interval": 0.5})]
    assert unreachable == [10002]
