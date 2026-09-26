"""The lab catalog is data with provenance, and it fails loudly when wrong."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

import pytest

import lab_catalog


def payload() -> dict[str, Any]:
    return json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))


def test_game_speed_table_matches_the_wiki() -> None:
    game_speed = lab_catalog.lab(lab_catalog.GAME_SPEED)
    assert game_speed is not None and game_speed.levels is not None
    assert game_speed.max_level == 7
    assert [(row.level, row.coins, row.seconds, row.max_speed) for row in game_speed.levels] == [
        (1, 300, 540, 2.0), (2, 2_500, 9_000, 2.5), (3, 12_000, 35_280, 3.0),
        (4, 50_000, 122_520, 3.5), (5, 150_000, 329_940, 4.0),
        (6, 500_000, 1_215_960, 4.5), (7, 1_000_000, 2_199_960, 5.0)]
    assert lab_catalog.level(lab_catalog.GAME_SPEED, 3).coins == 12_000
    assert lab_catalog.level(lab_catalog.GAME_SPEED, 8) is None


def test_gem_prices_for_lab_slots_card_slots_and_cards() -> None:
    assert [lab_catalog.lab_slot_gems(slot) for slot in range(2, 6)] == [100, 400, 1_400, 3_000]
    assert [lab_catalog.card_slot_gems(slot) for slot in range(2, 11)] == [
        50, 100, 200, 300, 400, 500, 600, 750, 1_000]
    assert lab_catalog.CATALOG.card_gems == 20
    assert lab_catalog.CATALOG.labs_unlock_wave == 30
    assert lab_catalog.lab_slot_gems(1) is None
    assert lab_catalog.card_slot_gems(11) is None


def test_labs_without_a_price_table_are_unknown_not_free() -> None:
    labs_speed = lab_catalog.lab("labs.labs-speed")
    assert labs_speed is not None
    assert labs_speed.levels is None and labs_speed.max_level is None
    assert labs_speed.unlock == {"best_tier_1_wave": 150}
    assert lab_catalog.level("labs.labs-speed", 1) is None
    assert lab_catalog.lab("labs.invented") is None


def test_every_entry_records_its_source_and_checked_date() -> None:
    for entry in lab_catalog.CATALOG.labs:
        assert entry.source_url.startswith("https://")
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry.checked)
    reference = lab_catalog.reference()
    assert len(reference["game_speed"]) == 7
    assert reference["sources"] and all(source["url"].startswith("https://")
                                        for source in reference["sources"])


def _append_unknown_lab(raw: dict[str, Any]) -> None:
    raw["labs"].append({**raw["labs"][1], "id": "labs.invented"})


@pytest.mark.parametrize(("mutate", "message"), [
    (_append_unknown_lab, "unknown lab concept id"),
    (lambda raw: raw["lab_slots"].pop(), "lab_slots must list slots 2 to 5"),
    (lambda raw: raw["labs"][0]["levels"][2].update(coins=1), "must rise"),
    (lambda raw: raw["labs"][0].update(checked="yesterday"), "checked must be"),
    (lambda raw: raw["labs"][0].update(levels=None), "Game Speed needs its price table"),
    (lambda raw: raw.update(version=2), "unsupported lab catalog version"),
])
def test_invalid_catalog_fails_loudly(mutate: Callable[[dict[str, Any]], object],
                                      message: str) -> None:
    raw = payload()
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        lab_catalog.load(raw)
