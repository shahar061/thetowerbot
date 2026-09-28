"""Catalog v2: unlock condition lists, zero-second levels and optional values."""

from __future__ import annotations

import copy
import json

import pytest

import lab_catalog


def _shipped() -> dict:
    return json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))


def _payload(**lab_overrides: object) -> dict:
    payload = _shipped()
    lab = {"id": "labs.labs-speed", "name": "Labs Speed", "max_level": 3,
           "unlock": [{"tier": 1, "wave": 150}],
           "levels": [{"level": 1, "coins": 40, "seconds": 0, "value": "1.02"},
                      {"level": 2, "coins": 83, "seconds": 540, "value": "1.04"},
                      {"level": 3, "coins": 211, "seconds": 1320, "value": "1.06"}],
           "source_url": "https://the-tower-idle-tower-defense.fandom.com/wiki/Lab/Lab_Speed",
           "checked": "2026-09-28"}
    lab.update(lab_overrides)
    payload["labs"] = [item for item in payload["labs"] if item["id"] != "labs.labs-speed"] + [lab]
    return payload


def test_v2_loads_unlock_lists_zero_second_levels_and_values() -> None:
    catalog = lab_catalog.load(_payload())
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock == ({"tier": 1, "wave": 150},)
    assert labs_speed.levels[0].seconds == 0
    assert labs_speed.levels[0].value == "1.02" and labs_speed.levels[0].max_speed is None


def test_v2_accepts_lab_prerequisites_that_exist() -> None:
    catalog = lab_catalog.load(_payload(unlock=[{"tier": 4, "wave": 80},
                                                {"lab": "labs.game-speed", "level": 1}]))
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock[1] == {"lab": "labs.game-speed", "level": 1}


@pytest.mark.parametrize("unlock", [
    [{"lab": "labs.not-in-catalog", "level": 1}],
    [{"tier": 0, "wave": 10}],
    [{"tier": 1}],
    {"tier": 1, "wave": 150},
    [{"lab": "labs.labs-speed", "level": 1}],
])
def test_v2_rejects_bad_unlocks(unlock: object) -> None:
    with pytest.raises(ValueError, match="unlock"):
        lab_catalog.load(_payload(unlock=unlock))


def test_v2_rejects_decreasing_seconds_or_coins() -> None:
    levels = _payload()["labs"][-1]["levels"]
    slower = copy.deepcopy(levels)
    slower[2]["seconds"] = 10
    with pytest.raises(ValueError, match="rise"):
        lab_catalog.load(_payload(levels=slower))
    cheaper = copy.deepcopy(levels)
    cheaper[2]["coins"] = 1
    with pytest.raises(ValueError, match="rise"):
        lab_catalog.load(_payload(levels=cheaper))


def _minimal_v1() -> dict:
    source = {"source_url": "https://the-tower-idle-tower-defense.fandom.com/wiki/Lab_Upgrades",
              "checked": "2026-09-26"}
    return {"version": 1, "labs_unlock": {"best_tier_1_wave": 30, **source},
            "labs": [{"id": "labs.game-speed", "name": "Game Speed", "unlock": None, "max_level": 1,
                      "levels": [{"level": 1, "coins": 300, "seconds": 540, "max_speed": 2.0}], **source},
                     {"id": "labs.labs-speed", "name": "Labs Speed", "unlock": {"best_tier_1_wave": 150},
                      "max_level": None, "levels": None, **source}],
            "lab_slots": [{"slot": slot, "gems": 100, **source} for slot in range(2, 6)],
            "card_slots": [{"slot": slot, "gems": 50, **source} for slot in range(2, 11)],
            "card_gems": {"gems": 20, **source}}


def test_v1_unlock_reads_as_a_tier_one_condition() -> None:
    catalog = lab_catalog.load(_minimal_v1())
    game_speed, labs_speed = catalog.labs
    assert labs_speed.unlock == ({"tier": 1, "wave": 150},) and labs_speed.levels is None
    assert game_speed.unlock == ()


def test_tier_one_wave_reads_the_tier_one_condition() -> None:
    assert lab_catalog.tier_one_wave("labs.labs-speed") == 150
    assert lab_catalog.tier_one_wave("labs.game-speed") is None


TEMPLATE_LABS = ("labs.game-speed", "labs.unlock-perks", "labs.first-perk-choice", "labs.perk-option-quantity",
                 "labs.ban-perks", "labs.light-speed-shots", "labs.coins-wave", "labs.labs-speed",
                 "labs.coins-kill-bonus", "labs.cash-bonus", "labs.attack-speed", "labs.health", "labs.damage",
                 "labs.standard-perks-bonus", "labs.improve-trade-off-perks", "labs.workshop-attack-discount",
                 "labs.workshop-defense-discount", "labs.workshop-utility-discount")


def test_shipped_catalog_is_v2_and_knows_every_template_lab() -> None:
    assert json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))["version"] == 2
    for lab_id in TEMPLATE_LABS:
        assert lab_catalog.lab(lab_id) is not None, lab_id


def test_shipped_labs_speed_matches_the_wiki() -> None:
    labs_speed = lab_catalog.lab("labs.labs-speed")
    assert labs_speed.max_level == 99 and labs_speed.unlock == ({"tier": 1, "wave": 150},)
    assert lab_catalog.level("labs.labs-speed", 99).coins == 19_360_000


@pytest.mark.parametrize(("lab_id", "unlock"), [
    ("labs.labs-speed", ({"tier": 1, "wave": 150},)),
    ("labs.perk-option-quantity", ({"tier": 4, "wave": 80}, {"lab": "labs.unlock-perks", "level": 1})),
    ("labs.light-speed-shots", ({"tier": 7, "wave": 10},)),
    ("labs.unlock-perks", ({"tier": 2, "wave": 150},)),
])
def test_shipped_catalog_carries_the_wiki_unlocks(lab_id: str, unlock: tuple[dict, ...]) -> None:
    assert lab_catalog.lab(lab_id).unlock == unlock
