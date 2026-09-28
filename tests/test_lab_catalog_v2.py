"""Catalog v2: unlock condition lists, zero-second levels and optional values."""

from __future__ import annotations

import copy
import json

import pytest

import lab_catalog


def _v1() -> dict:
    return json.loads(lab_catalog.CATALOG_PATH.read_text(encoding="utf-8"))


def _payload(**lab_overrides: object) -> dict:
    payload = _v1()
    payload["version"] = 2
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


def test_v1_unlock_reads_as_a_tier_one_condition() -> None:
    catalog = lab_catalog.load(_v1()) if _v1()["version"] == 1 else None
    if catalog is None:
        pytest.skip("shipped catalog is already v2")
    labs_speed = next(item for item in catalog.labs if item.id == "labs.labs-speed")
    assert labs_speed.unlock == ({"tier": 1, "wave": 150},)


def test_tier_one_wave_reads_the_tier_one_condition() -> None:
    assert lab_catalog.tier_one_wave("labs.labs-speed") == 150
    assert lab_catalog.tier_one_wave("labs.game-speed") is None
