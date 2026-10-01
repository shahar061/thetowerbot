from __future__ import annotations

import lab_catalog
from lab_starter_rollout import RehearsedLab, StarterState
from tools.lab_observed_prices import differences


def test_only_observations_that_differ_from_the_catalog_are_listed() -> None:
    attack_speed_level = lab_catalog.level("labs.attack-speed", 1)
    damage_level = lab_catalog.level("labs.damage", 1)

    state = StarterState({}, {
        "labs.attack-speed": RehearsedLab("rehearsed", level=1, price=attack_speed_level.coins, seconds=float(attack_speed_level.seconds)),
        "labs.damage": RehearsedLab("rehearsed", level=1, price=damage_level.coins, seconds=float(damage_level.seconds) + 99),
        "labs.ban-perks": RehearsedLab("needs_review", level=1, price=5000, seconds=60.),
        "labs.health": RehearsedLab("missing", misses=(1.,)),
    })
    rows = differences(state)
    assert [(row["lab_id"], row["field"]) for row in rows] == [
        ("labs.ban-perks", "coins"), ("labs.damage", "seconds")]
    assert rows[1]["observed"] == float(damage_level.seconds) + 99 and rows[1]["catalog"] is not None


def test_an_abbreviated_price_within_its_rounding_is_not_a_difference() -> None:
    level = lab_catalog.level("labs.attack-speed", 6)
    assert level.coins >= 1000                      # shown abbreviated, e.g. "1.34K"
    state = StarterState({}, {
        "labs.attack-speed": RehearsedLab("rehearsed", level=6, price=level.coins + 9),
        "labs.damage": RehearsedLab("rehearsed", level=6,
                                    price=lab_catalog.level("labs.damage", 6).coins + 100),
    })
    assert [(row["lab_id"], row["field"]) for row in differences(state)] == [("labs.damage", "coins")]
