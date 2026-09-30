from __future__ import annotations

from lab_starter_rollout import RehearsedLab, StarterState
from tools.lab_observed_prices import differences


def test_only_observations_that_differ_from_the_catalog_are_listed() -> None:
    state = StarterState({}, {
        "labs.attack-speed": RehearsedLab("rehearsed", level=1, price=30, seconds=15., catalog_price=30),
        "labs.damage": RehearsedLab("rehearsed", level=1, price=30, seconds=99., catalog_price=30),
        "labs.ban-perks": RehearsedLab("needs_review", level=1, price=5000, seconds=60., catalog_price=None),
        "labs.health": RehearsedLab("missing", misses=(1.,)),
    })
    rows = differences(state)
    assert [(row["lab_id"], row["field"]) for row in rows] == [
        ("labs.ban-perks", "coins"), ("labs.damage", "seconds")]
    assert rows[1]["observed"] == 99. and rows[1]["catalog"] is not None
