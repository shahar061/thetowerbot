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


def test_bot_and_decision_come_from_the_worker_status() -> None:
    status = {"screen": "IN_RUN", "activity": {"label": "Navigating · RETRY", "at": 5.},
              "decision": {"phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "at": 5.}}
    assert state_view.build_bot(status) == {"screen": "IN_RUN", "now": "Navigating · RETRY",
                                            "live": True}
    assert state_view.build_bot(None) == {"screen": None, "now": None, "live": False}
    assert state_view.build_decision(status["decision"]) == {
        "phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "category": "attack",
        "name": "Damage", "cost": None}
    assert state_view.build_decision({"phase": "saving", "reason": "wait"})["name"] is None
    assert state_view.build_decision(None) is None


def test_the_battle_is_live_only_in_a_run_and_measured_against_its_tier_best() -> None:
    status = {"screen": "IN_RUN", "wave": 4812, "wallet": 8420000000, "run": {"elapsed": 5780.4}}
    assert state_view.build_battle(status, 11, {11: 5020, 7: 1420}) == {
        "tier": 11, "wave": 4812, "cash": 8420000000, "elapsed_s": 5780.4, "best_wave": 5020}
    assert state_view.build_battle({**status, "screen": "GAME_OVER"}, 11, {11: 5020}) is None
    assert state_view.build_battle(None, 11, {11: 5020}) is None


def test_a_new_account_with_zero_runs_has_no_tier_no_best_and_no_upgrades() -> None:
    """Review Focus: a brand-new account has not finished a single run."""
    assert state_view.build_runs([]) == []
    battle = state_view.build_battle({"screen": "IN_RUN", "wave": None, "wallet": None}, None, {})
    assert battle == {"tier": None, "wave": None, "cash": None, "elapsed_s": None,
                      "best_wave": None}
    assert state_view.build_run_upgrades([], None) is None


def test_runs_keep_the_newest_five_with_their_duration() -> None:
    rows = [{"tier": 11, "wave": 5020 - i, "coins": 310000000, "started_at": 1000. * i,
             "ended_at": 1000. * i + 6020, "abandoned": i == 1} for i in range(7)]
    runs = state_view.build_runs(rows)
    assert len(runs) == 5
    assert runs[0] == {"tier": 11, "wave": 5020, "coins": 310000000, "duration_s": 6020.0,
                       "ended_at": "1970-01-01T01:40:20+00:00", "abandoned": False}
    assert runs[1]["abandoned"] is True


def test_run_upgrades_total_split_by_category_and_sorted() -> None:
    upgrades_view = state_view.build_run_upgrades(
        [{"upgrade_id": "health", "levels": 4}, {"upgrade_id": "damage", "levels": 31},
         {"upgrade_id": "mystery", "levels": 2}, {"upgrade_id": "", "levels": 9}], "current")
    assert upgrades_view == {
        "scope": "current", "total": 37,
        "by_category": {"attack": 31, "defense": 4, "utility": 0},
        "items": [{"id": "damage", "name": "Damage", "category": "attack", "levels": 31},
                  {"id": "health", "name": "Health", "category": "defense", "levels": 4},
                  {"id": "mystery", "name": "mystery", "category": None, "levels": 2}]}
    assert state_view.build_run_upgrades([], "last") == {
        "scope": "last", "total": 0, "by_category": {"attack": 0, "defense": 0, "utility": 0},
        "items": []}


def test_balances_prefer_the_live_scope_and_never_invent_stones() -> None:
    live = {"coins_lower": 3840000000, "gems": 1842}
    assert state_view.build_balances(live, {"coins": 1, "gems": 2}) == {
        "coins": 3840000000, "gems": 1842, "stones": None}
    assert state_view.build_balances({"coins_lower": None, "gems": None},
                                     {"coins": 500, "gems": None}) == {
        "coins": 500, "gems": None, "stones": None}
    assert state_view.build_balances(None, None) == {"coins": None, "gems": None, "stones": None}


def test_cards_read_slots_per_card_facts_and_the_next_slot_price() -> None:
    revision = {"cards": [{"concept_id": "cards.slots.equipped", "value": 1},
                          {"concept_id": "cards.slots.capacity", "value": 1},
                          {"concept_id": "cards.damage.level", "value": 5},
                          {"concept_id": "cards.damage.copies", "value": 12}]}
    view = state_view.build_cards(revision, 70, [{"ts": 10., "item": "Damage", "price": 20}])
    assert view["slots"] == {"equipped": 1, "capacity": 1, "next_slot_gems": 50}
    assert view["items"] == [{"name": "Damage", "level": 5, "copies": 12}]
    assert view["gems_invested"] == 70
    assert view["recent"] == [{"ts": "1970-01-01T00:00:10+00:00", "name": "Damage", "gems": 20}]
    empty = state_view.build_cards(None, 0, [])
    assert empty["slots"] == {"equipped": None, "capacity": None, "next_slot_gems": None}
    assert empty["items"] == []


def test_labs_list_levels_running_jobs_and_the_cheapest_known_next() -> None:
    revision = {"lab_slots_owned": 2,
                "lab_levels": [{"concept_id": "labs.game-speed", "value": 3, "status": "owned"},
                               {"concept_id": "labs.damage", "value": 11, "status": "owned"}],
                "lab_jobs": [{"concept_id": "labs.damage", "value": 5000., "status": "researching"}]}
    labs = state_view.build_labs(revision, [{"ts": 10., "item": "labs.damage", "price": 4100000},
                                            {"ts": 9., "item": "Game Speed", "price": 12000}],
                                 now=1000.)
    assert labs["slots"] == 2
    assert labs["running"] == [{"id": "labs.damage", "name": "Damage", "to_level": 12,
                                "completes_at": "1970-01-01T01:23:20+00:00"}]
    assert labs["levels"] == [
        {"id": "labs.game-speed", "name": "Game Speed", "level": 3, "next_cost": 50000},
        {"id": "labs.damage", "name": "Damage", "level": 11, "next_cost": None}]
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 50000}
    assert [row["name"] for row in labs["recent"]] == ["Damage", "Game Speed"]


def test_a_lab_job_past_its_completion_time_is_not_running() -> None:
    """Review Focus: the revision still holds a job the game has already finished."""
    revision = {"lab_levels": [{"concept_id": "labs.damage", "value": 11, "status": "owned"}],
                "lab_jobs": [{"concept_id": "labs.damage", "value": 999., "status": "researching"}]}
    labs = state_view.build_labs(revision, [], now=1000.)
    assert labs["running"] == []
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 300}


def test_labs_on_a_new_account_point_at_game_speed_level_one() -> None:
    labs = state_view.build_labs(None, [], now=1000.)
    assert (labs["slots"], labs["running"], labs["levels"]) == (None, [], [])
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 300}
