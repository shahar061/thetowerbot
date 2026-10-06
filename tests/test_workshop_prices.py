from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.workshop_prices import WorkshopPrices, catalog_price


def test_coin_prices_are_indexed_by_current_level_and_not_cash() -> None:
    assert catalog_price("coins_per_kill_bonus", 0) == 50
    assert catalog_price("coins_per_kill_bonus", 1) == 83
    assert catalog_price("damage", 0) == 30
    assert catalog_price("unlock_defense_upgrades", 0) == 75
    assert catalog_price("coins_per_kill_bonus", 10000) is None
    assert catalog_price("damage", -1) is None


def test_observed_price_calibrates_tutorial_levels_and_survives_restart(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "account-a")
    memory.observe("damage", 383, 0, now=100)
    memory.save()
    restored = WorkshopPrices(tmp_path, "account-a")
    quotes = restored.quotes({"damage": 1})
    assert quotes["damage"].price == 472
    assert quotes["damage"].level == 8
    assert quotes["damage"].source == "catalog_estimate"
    assert restored.quotes({})["damage"].source == "observed"
    assert "damage" not in WorkshopPrices(tmp_path, "account-b").quotes({})


def test_unmatched_live_price_is_preserved_but_never_extrapolated(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 37, 0, now=100)
    assert memory.quotes({})["damage"].price == 37
    assert memory.quotes({})["damage"].level is None
    assert "damage" not in memory.quotes({"damage": 1})


def test_uncertain_purchase_or_manual_change_invalidates_older_quotes(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 30, 0, now=100)
    memory.observe("health", 30, 0, now=100)
    quotes = memory.quotes({}, invalidated={"damage": 101})
    assert "damage" not in quotes and "health" in quotes
    assert not memory.quotes({}, changed_at=101)
    memory.observe("damage", 55, 0, now=102)
    assert memory.quotes({}, changed_at=101)["damage"].price == 55


def test_maxed_row_is_account_bound_and_lasts_until_a_buy_or_price_contradicts_it(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 30, 3, now=100)
    memory.observe_maxed("damage", 3, now=101)
    memory.observe_maxed("discovered:mystery", 0, now=101)
    memory.save()
    restored = WorkshopPrices(tmp_path, "a")
    assert restored.maxed_ids({"damage": 3}) == ("damage",)
    # A maxed row has no price to quote, whatever was read before it.
    assert "damage" not in restored.quotes({"damage": 3})
    # A confirmed buy after the read, or a ledger that went backwards
    # (a reset account history), proves the read no longer holds.
    assert restored.maxed_ids({"damage": 4}) == ()
    assert restored.maxed_ids({}) == ()
    assert WorkshopPrices(tmp_path, "b").maxed_ids({"damage": 3}) == ()
    # A later priced read of the row (say, a raised max level) clears it.
    restored.observe("damage", 900, 3, now=102)
    assert restored.maxed_ids({"damage": 3}) == ()
    assert restored.quotes({"damage": 3})["damage"].price == 900


def test_a_malformed_maxed_record_keeps_valid_prices(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 30, 0, now=100)
    memory.save()
    payload = json.loads((tmp_path / "workshop-prices.json").read_text())
    payload["maxed"] = ["damage"]
    (tmp_path / "workshop-prices.json").write_text(json.dumps(payload))
    restored = WorkshopPrices(tmp_path, "a")
    assert restored.quotes({})["damage"].price == 30
    assert restored.maxed_ids({}) == ()


def test_unsupported_or_changed_discounts_require_a_fresh_read(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 30, 0, now=100, discount_signature="none")
    assert "damage" not in memory.quotes({}, discount_signature="attack:1")


@pytest.mark.parametrize("price", [-1, True, None])
def test_unreadable_prices_do_not_destroy_a_valid_observation(tmp_path: Path, price: int | None) -> None:
    memory = WorkshopPrices(tmp_path, "a")
    memory.observe("damage", 30, 0, now=100)
    memory.observe("damage", price, 0, now=101)
    assert memory.quotes({})["damage"].price == 30
