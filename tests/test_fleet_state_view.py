"""The Fleet State page's builders: plain inputs in, JSON-safe sections out."""

from __future__ import annotations

from typing import Any

import workshop_levels
from fleet import state_view


def _fact(upgrade_id: str, raw: str, value: float) -> dict[str, Any]:
    return {"concept_id": f"stats.{upgrade_id}", "value": value, "status": "verified",
            "evidence": {"observed_at": 100., "raw_value": raw}}


def _skill(workshop: dict[str, Any], category: str, upgrade_id: str) -> dict[str, Any]:
    return next(s for s in workshop["categories"][category]["skills"] if s["id"] == upgrade_id)


# Health "154" is level 14, Attack Speed "1.55" level 11, Range "30.50m" level 1,
# Critical Factor "x1.20" level 0 - pinned in tests/test_workshop_levels.py.
STATS = [_fact("health", "154", 154.), _fact("attack_speed", "1.55", 1.55),
         _fact("range", "30.50m", 30.5), _fact("critical_factor", "x1.20", 1.2)]


def test_category_totals_sum_known_levels_and_skip_unseen_and_unmatched() -> None:
    rows = workshop_levels.workshop_state(STATS)
    assert state_view.category_totals(rows) == {"attack": 12, "defense": 14, "utility": 0}
    unmatched = workshop_levels.workshop_state([_fact("attack_speed", "1.57", 1.57),
                                                _fact("health", "154", 154.)])
    assert state_view.category_totals(unmatched) == {"attack": 0, "defense": 14, "utility": 0}


def test_invested_is_the_sum_of_the_ladder_rungs_below_the_level() -> None:
    ladder = workshop_levels.ladders()["health"]
    assert state_view.invested_coins("health", 14) == sum(ladder.next_coins[:14])
    assert state_view.invested_coins("health", 0) == 0
    assert state_view.invested_coins("health", None) is None
    assert state_view.invested_coins("unlock_multishot", 3) is None  # no ladder
    # A maxed read never reaches the ladder's final None rung.
    assert state_view.invested_coins("health", ladder.max_level + 5) == sum(
        ladder.next_coins[:ladder.max_level])


def test_bot_spent_resolves_display_names_and_ids_and_sums_per_upgrade() -> None:
    spent = state_view.bot_spent([("Damage", "ATTACK", 300), ("damage", None, 50),
                                  ("Health", "DEFENSE", None), ("Not a row", "ATTACK", 9)])
    assert spent == {"damage": 350}


def test_the_workshop_section_carries_levels_invested_bot_spent_and_next_cost() -> None:
    workshop = state_view.build_workshop(
        {"workshop_stats": STATS}, [("Health", "DEFENSE", 1200)],
        [{"ts": 1700000000., "item": "Health", "category": "DEFENSE", "price": 1100}])

    assert workshop["totals"] == {"attack": 12, "defense": 14, "utility": 0}
    health = _skill(workshop, "defense", "health")
    assert (health["level"], health["bot_spent"], health["next_cost"], health["status"]) == (
        14, 1200, 1233, "exact")
    assert health["invested"] == sum(workshop_levels.ladders()["health"].next_coins[:14])
    assert workshop["recent"] == [{"ts": "2023-11-14T22:13:20+00:00", "id": "health",
                                   "name": "Health", "category": "defense", "level": None,
                                   "price": 1100}]


def test_next_unlock_is_the_first_unowned_tile_with_its_catalog_price_or_none() -> None:
    workshop = state_view.build_workshop({"workshop_stats": STATS}, [], [])
    # Range was read, so Unlock Range Upgrades is owned; Multishot has no catalog price.
    assert workshop["categories"]["attack"]["next_unlock"] == {
        "id": "unlock_multishot", "name": "Unlock Multishot", "cost": None}
    assert workshop["categories"]["defense"]["next_unlock"] == {
        "id": "unlock_defense_upgrades", "name": "Unlock Defense Upgrades", "cost": 75}
    owned = state_view.build_workshop(
        {"workshop_stats": STATS,
         "unlocks": [{"concept_id": "unlocks.defense_upgrades", "value": True}]}, [], [])
    assert owned["categories"]["defense"]["next_unlock"]["id"] == "unlock_thorns"
    assert owned["categories"]["defense"]["next_unlock"]["cost"] == 500


def test_a_bought_unlock_counts_as_owned_and_unlocks_its_rows() -> None:
    workshop = state_view.build_workshop(None, [("Unlock Multishot", "ATTACK", 900)], [])
    attack = workshop["categories"]["attack"]
    assert _skill(workshop, "attack", "multishot_chance")["locked"] is False
    assert _skill(workshop, "attack", "range")["locked"] is True
    assert attack["unlocked"] == attack["total"] - sum(s["locked"] for s in attack["skills"])


def test_a_new_account_or_a_revision_without_workshop_stats_reads_as_unseen() -> None:
    """Review Focus: a brand-new account has no revision, or one with no Workshop reads."""
    for revision in (None, {}, {"lab_levels": []}):
        workshop = state_view.build_workshop(revision, [], [])
        assert workshop["totals"] == {"attack": 0, "defense": 0, "utility": 0}
        damage = _skill(workshop, "attack", "damage")
        assert (damage["level"], damage["invested"], damage["next_cost"], damage["status"]) == (
            None, None, None, "unseen")
        assert workshop["categories"]["attack"]["next_unlock"]["id"] == "unlock_range_upgrades"
        assert workshop["recent"] == []


def test_recent_workshop_buys_are_capped() -> None:
    rows = [{"ts": float(i), "item": "Damage", "category": "ATTACK", "price": i}
            for i in range(50)]
    assert len(state_view.workshop_recent(rows)) == state_view.RECENT_LIMIT == 30
