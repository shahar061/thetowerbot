"""The Fleet State page's builders: plain inputs in, JSON-safe sections out."""

from __future__ import annotations

import http.client
import io
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

import db
import workshop_levels
from events import EventBus
from fleet import state_view
from fleet.setup import FleetSetupService
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


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


def test_next_unlock_is_the_first_unowned_group_with_its_display_price() -> None:
    workshop = state_view.build_workshop({"workshop_stats": STATS}, [], [])
    # Range was read, so the next group contains both Multishot skills.
    assert workshop["categories"]["attack"]["next_unlock"] == {
        "id": "unlock_multishot", "name": "Unlock Multishot", "cost": 400,
        "upgrade_ids": ["multishot_chance", "multishot_targets"]}
    assert workshop["categories"]["defense"]["next_unlock"] == {
        "id": "unlock_defense_upgrades", "name": "Unlock Defense Upgrades", "cost": 75,
        "upgrade_ids": ["defense_percent", "defense_absolute"]}
    owned = state_view.build_workshop(
        {"workshop_stats": STATS,
         "unlocks": [{"concept_id": "unlocks.defense_upgrades", "value": True,
                      "status": "verified"}]}, [], [])
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


def test_workshop_defense_shows_four_owned_skills_and_only_thorns_as_next() -> None:
    workshop = state_view.build_workshop(
        {"workshop_stats": [_fact("health", "14", 14.),
                            _fact("defense_percent", "0%", 0.)]}, [], [])
    defense = workshop["categories"]["defense"]
    assert (defense["unlocked"], defense["total"]) == (4, 18)
    assert defense["next_unlock"]["upgrade_ids"] == ["thorns"]
    assert _skill(workshop, "defense", "thorns")["next_unlock"] is True
    for uid in ("shockwave_size", "land_mine_chance", "death_defy", "wall_health"):
        skill = _skill(workshop, "defense", uid)
        assert skill["locked"] is True and skill["next_unlock"] is False
        assert skill["unlock_id"]


def test_unseen_or_negative_workshop_facts_do_not_prove_unlock_ownership() -> None:
    unseen = {**_fact("thorns", "1%", 1.), "status": "unseen"}
    negative = _fact("shockwave_size", "-1.0", -1.)
    workshop = state_view.build_workshop({"workshop_stats": [unseen, negative],
        "unlocks": [{"concept_id": "unlocks.thorns", "value": True, "status": "unseen"}]}, [], [])
    assert _skill(workshop, "defense", "thorns")["locked"] is True
    assert _skill(workshop, "defense", "shockwave_size")["locked"] is True
    assert _skill(workshop, "defense", "shockwave_size")["level"] is None


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


def test_the_best_wave_is_the_highest_across_tiers_and_none_before_any_run() -> None:
    assert state_view.build_best_wave({11: 5020, 7: 1420}) == {"wave": 5020, "tier": 11}
    assert state_view.build_best_wave({3: 900, 5: 900}) == {"wave": 900, "tier": 5}
    assert state_view.build_best_wave({}) is None


def test_the_strategy_is_the_assignment_that_still_names_this_account() -> None:
    assignment = SimpleNamespace(account_id="account-a", strategy_id="s1",
                                 strategy_name="Coin Rush", strategy_version=3)
    assert state_view.build_strategy(assignment, "account-a") == {
        "id": "s1", "name": "Coin Rush", "version": 3}
    assert state_view.build_strategy(assignment, "account-b") is None
    assert state_view.build_strategy(None, "account-a") is None


def test_the_next_buy_comes_from_this_accounts_plan_with_its_price() -> None:
    plan = {"account_id": "account-a", "state": "save_coins", "upgrade_id": "damage",
            "item": "Damage", "category": "Attack", "price": 120000000, "wallet_coins": 80000000,
            "price_source": "observed", "reason": "Saving", "goal": "Reach T1 W20",
            "observed_at": 0.0}
    assert state_view.build_next_buy(plan, "account-a") == {
        "state": "save_coins", "upgrade_id": "damage", "name": "Damage", "category": "attack",
        "price": 120000000, "price_source": "observed", "wallet": 80000000, "reason": "Saving",
        "goal": "Reach T1 W20", "observed_at": "1970-01-01T00:00:00+00:00"}
    unpriced = state_view.build_next_buy(
        {"account_id": "account-a", "state": "observe_price", "upgrade_id": "damage"}, "account-a")
    assert (unpriced["name"], unpriced["category"], unpriced["price"]) == ("Damage", "attack", None)
    assert state_view.build_next_buy(plan, "account-b") is None
    assert state_view.build_next_buy(None, "account-a") is None


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
        {"id": "labs.damage", "name": "Damage", "level": 11, "next_cost": 10_560}]
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 50000}  # Damage is running
    assert [row["name"] for row in labs["recent"]] == ["Damage", "Game Speed"]


def test_a_lab_job_past_its_completion_time_is_not_running() -> None:
    """Review Focus: the revision still holds a job the game has already finished."""
    revision = {"lab_levels": [{"concept_id": "labs.damage", "value": 11, "status": "owned"}],
                "lab_jobs": [{"concept_id": "labs.damage", "value": 999., "status": "researching"}]}
    labs = state_view.build_labs(revision, [], now=1000.)
    assert labs["running"] == []
    assert labs["next"] == {"id": "labs.damage", "name": "Damage", "cost": 10_560}


def test_labs_on_a_new_account_point_at_game_speed_level_one() -> None:
    labs = state_view.build_labs(None, [], now=1000.)
    assert (labs["slots"], labs["running"], labs["levels"]) == (None, [], [])
    assert labs["next"] == {"id": "labs.game-speed", "name": "Game Speed", "cost": 300}


def test_totals_come_from_the_saved_stats_reading_and_bot_claimed_gems() -> None:
    lifetime = {"lifetime_coins": 1860, "coins_incomplete": True, "lifetime_stones": 45,
                "observed_at": 10.0}
    assert state_view.build_totals(lifetime, 320) == {
        "coins": 1860, "coins_incomplete": True, "stones": 45, "gems_claimed": 320,
        "observed_at": "1970-01-01T00:00:10+00:00"}
    # A record saved before stones were kept reads as unobserved stones, not 0.
    assert state_view.build_totals({"lifetime_coins": 5, "observed_at": 10.0}, 0)["stones"] is None
    assert state_view.build_totals(None, 0) == {
        "coins": None, "coins_incomplete": False, "stones": None, "gems_claimed": 0,
        "observed_at": None}


def _registered(root: Path, worker: str, account: str, port: int) -> Path:
    worker_root = root / "workers" / worker
    checkpoint = worker_root / "checkpoints" / ("a" * 32 + ".json")
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(json.dumps({"worker_id": worker, "account_id": account,
                                      "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    (worker_root / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": worker, "account_id": account,
        "web_port": port, "binding": str(checkpoint),
        "endpoint": "127.0.0.1:5555", "lease_id": "lease"}))
    db.bind_account(worker_root / "tower_bot.db", account)
    return worker_root


def _fetch(statuses: dict[int, Any]) -> Callable[..., Any]:
    """A urlopen stand-in: a dict is the status JSON, an exception is raised."""
    def fetch(url: str, timeout: float) -> io.BytesIO:
        assert timeout == state_view.STATUS_TIMEOUT
        value = statuses.get(int(url.split(":")[2].split("/")[0]), OSError("refused"))
        if isinstance(value, Exception):
            raise value
        if callable(value):
            value = value()
        return io.BytesIO(json.dumps(value).encode())
    return fetch


def _state_workshop(root: Path) -> dict[str, Any]:
    (account,) = state_view.fleet_state(root, [{"name": "Air_1"}], fetch=_fetch({}), now=7000.)["accounts"]
    assert account["error"] is None
    return account["workshop"]


def _price_anchor(root: Path, account: str = "account-a") -> None:
    from fleet.workshop_prices import WorkshopPrices
    memory = WorkshopPrices(root, account)
    memory.observe("damage", 30, 99, now=100.)
    memory.wallet = {"coins": 100, "run_id": 0, "observed_at": 100.}
    memory.save()


def _workshop_receipt(root: Path, ts: float, verdict: str = "bought", *,
                      item: str = "Damage", price: int = 30) -> None:
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                     "VALUES(?,'WORKSHOP_BUY',?,'ATTACK','coins',?,?,0,?)",
                     (ts, item, -price if verdict == "bought" else None, price,
                      json.dumps({"verdict": verdict})))


def test_fleet_workshop_advances_an_anchored_quote_from_confirmed_actions_only(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    _price_anchor(root)
    _workshop_receipt(root, 99.)  # A delayed receipt already included in the observation.
    _workshop_receipt(root, 101.)
    damage = _skill(_state_workshop(tmp_path), "attack", "damage")
    assert damage["next_cost"] == 55
    assert damage["next_cost_source"] == "catalog_estimate"
    assert damage["level"] is None and damage["level_source"] is None
    assert damage["max_level"] == workshop_levels.ladders()["damage"].max_level


def test_fleet_workshop_advances_a_stat_anchor_without_using_lifetime_purchase_counts(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                     (json.dumps({"account_id": "account-a", "workshop_stats": [
                         _fact("critical_factor", "x1.20", 1.2)]}),))
    _workshop_receipt(root, 99., item="Critical Factor", price=50)
    _workshop_receipt(root, 101., item="Critical Factor", price=50)
    skill = _skill(_state_workshop(tmp_path), "attack", "critical_factor")
    assert skill["level"] == skill["level_min"] == skill["level_max"] == 1
    assert skill["level_source"] == "confirmed_actions"
    assert skill["next_cost"] == 75
    assert skill["next_cost_source"] == "stat_ladder"


@pytest.mark.parametrize("kind", ["unproven", "unconfirmed", "unexplained", "discount"])
def test_uncertainty_does_not_fall_back_to_a_stale_stat_price(tmp_path: Path, kind: str) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    _price_anchor(root)
    revision = {"account_id": "account-a", "workshop_stats": [_fact("damage", "3", 3.)]}
    with db.connect(root / "tower_bot.db") as conn:
        if kind == "discount":
            revision["lab_levels"] = [{"concept_id": "labs.workshop-attack-discount",
                                       "status": "verified", "value": 1}]
        elif kind == "unconfirmed":
            conn.execute("INSERT INTO ledger(ts,kind,item,category,reason,dry_run) "
                         "VALUES(101,'BUY_SKIPPED','Damage','ATTACK','unconfirmed',0)")
        elif kind == "unexplained":
            conn.execute("INSERT INTO ledger(ts,kind,currency,delta,dry_run) "
                         "VALUES(101,'UNEXPLAINED','coins',-30,0)")
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)", (json.dumps(revision),))
    if kind == "unproven":
        _workshop_receipt(root, 101., "unproven")
    skill = _skill(_state_workshop(tmp_path), "attack", "damage")
    assert skill["next_cost"] is None and skill["next_cost_source"] is None
    if kind != "discount":
        assert skill["level"] is None and skill["level_source"] is None


def test_foreign_price_memory_never_supplies_state(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    _price_anchor(root, account="account-old")
    _workshop_receipt(root, 101.)
    damage = _skill(_state_workshop(tmp_path), "attack", "damage")
    assert damage["next_cost"] is None and damage["level"] is None


def test_unchanged_stat_reobservation_restores_a_level_after_an_uncertain_attempt(tmp_path: Path) -> None:
    from account_state import AccountRepository, AccountState
    from evidence_scope import FactScope, IdentityEvidence
    from tests.test_account_state import reading
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    state = AccountState(AccountRepository(root / "tower_bot.db"))
    state.bind_scope(FactScope("account-a", "lease", "generation", 0),
                     identity=IdentityEvidence("account-a", .5, "identity"))
    state.observe_account(reading(value=10., now=1.))
    state.observe_account(reading(value=10., now=2.))
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,reason,dry_run) "
                     "VALUES(3,'BUY_SKIPPED','Health','DEFENSE','unconfirmed',0)")
    assert _skill(_state_workshop(tmp_path), "defense", "health")["level"] is None
    state.observe_account(reading(value=10., now=63.))
    state.observe_account(reading(value=10., now=64.))
    health = _skill(_state_workshop(tmp_path), "defense", "health")
    assert health["level"] == 1 and health["level_source"] == "observed"
    assert health["next_cost"] == 55


LIVE = {"screen": "IN_RUN", "scans": 18442, "wallet": 8420000000, "wave": 4812,
        "run": {"id": 2, "elapsed": 5780.0},
        "activity": {"label": "Shopping", "at": 1.0},
        "decision": {"phase": "buying", "reason": "cheapest", "upgrade_id": "damage", "at": 1.0}}


def test_an_online_worker_becomes_a_full_account_column(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier) "
                     "VALUES (1, 0, 6020, 5020, 310000000, 11)")
    member = {"name": "Air_1", "endpoint": "127.0.0.1:5555", "lease_id": "lease"}

    state = state_view.fleet_state(tmp_path, [member], fetch=_fetch({8001: LIVE}), now=7000.)

    (account,) = state["accounts"]
    assert state["generated_at"] == "1970-01-01T01:56:40+00:00"
    assert {k: account[k] for k in ("id", "serial", "online", "stale_seconds", "scan", "error")} == {
        "id": "Air_1", "serial": "127.0.0.1:5555", "online": True, "stale_seconds": 0,
        "scan": 18442, "error": None}
    assert account["bot"] == {"screen": "IN_RUN", "now": "Shopping", "live": True}
    assert account["battle"] == {"tier": 11, "wave": 4812, "cash": 8420000000,
                                 "elapsed_s": 5780.0, "best_wave": 5020}
    assert account["decision"]["name"] == "Damage"
    assert account["balances"] == {"coins": None, "gems": None, "stones": None}
    assert account["run_upgrades"]["scope"] == "current"
    assert [run["wave"] for run in account["runs"]] == [5020]
    assert account["workshop"]["totals"] == {"attack": 0, "defense": 0, "utility": 0}
    assert account["best_wave"] == {"wave": 5020, "tier": 11}
    assert account["strategy"] is None and account["next_buy"] is None


def test_an_account_column_carries_lifetime_totals(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    (root / "reroll-lifetime.json").write_text(json.dumps({
        "account_id": "account-a", "lifetime_coins": 1000, "lifetime_stones": 12,
        "observed_at": 10.0, "baseline_run_id": 0}))
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier) "
                     "VALUES (1, 20, 60, 50, 250, 1)")
        conn.executemany(
            "INSERT INTO ledger(ts, kind, currency, delta, dry_run) VALUES (?, ?, ?, ?, ?)",
            [(1, "MISSION_CLAIM", "gems", 20, 0), (2, "MAIL_CLAIM", "gems", 5, 0),
             (3, "MAIL_CLAIM", "coins", 900, 0), (4, "GEM_CLAIM", "gems", 2, 1),
             (5, "UNEXPLAINED", "gems", 140, 0), (6, "CARD_BUY", "gems", -30, 0)])
    member = {"name": "Air_1", "endpoint": "127.0.0.1:5555", "lease_id": "lease"}

    (account,) = state_view.fleet_state(tmp_path, [member], fetch=_fetch({}), now=7000.)["accounts"]

    assert account["totals"] == {"coins": 1250, "coins_incomplete": False, "stones": 12,
                                 "gems_claimed": 25, "observed_at": "1970-01-01T00:00:10+00:00"}


def test_an_account_column_carries_its_strategy_and_next_buy(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    (root / "reroll-plan.json").write_text(json.dumps({
        "account_id": "account-a", "state": "buy", "upgrade_id": "damage", "item": "Damage",
        "category": "attack", "price": 900, "wallet_coins": 1000, "observed_at": 6990.0}))
    (tmp_path / "build-route.json").write_text("{")  # an unreadable route hides only the strategy
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}], fetch=_fetch({}),
                                        now=7000.)["accounts"]
    assert (account["next_buy"]["name"], account["next_buy"]["price"]) == ("Damage", 900)
    assert account["strategy"] is None and account["error"] is None


def test_an_offline_worker_is_built_from_its_database_with_a_stale_age(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO events(seq, ts, type) VALUES (1, 6958.0, 'ScanCompleted')")
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}], fetch=_fetch({}),
                                        now=7000.)["accounts"]
    assert (account["online"], account["stale_seconds"], account["scan"]) == (False, 42, None)
    assert account["battle"] is None and account["bot"]["screen"] is None
    assert account["workshop"] is not None and account["error"] is None


def test_a_reachable_worker_whose_database_is_missing_keeps_its_live_status(
        tmp_path: Path) -> None:
    """Review Focus: the bot answers /api/status but its database file is gone."""
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    (root / "tower_bot.db").unlink()
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}],
                                        fetch=_fetch({8001: LIVE}), now=7000.)["accounts"]
    assert account["online"] is True and account["scan"] == 18442
    assert account["bot"]["screen"] == "IN_RUN"
    assert account["error"] == "Worker database is missing"
    assert all(account[key] is None for key in ("workshop", "cards", "labs", "runs", "battle"))


def test_a_corrupt_revision_keeps_live_status_and_reads_workshop_as_unseen(tmp_path: Path) -> None:
    """Review Focus: a bad JSON blob in account_revisions must not blank the account."""
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('{bad')")
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}],
                                        fetch=_fetch({8001: LIVE}), now=7000.)["accounts"]
    assert account["online"] is True and account["scan"] == 18442
    assert account["error"] is None
    assert account["workshop"]["totals"] == {"attack": 0, "defense": 0, "utility": 0}


def test_a_null_revision_keeps_live_status_and_reads_workshop_as_unseen(tmp_path: Path) -> None:
    """Review Focus: a valid-but-non-dict revision (e.g. null) must not crash .get()."""
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('null')")
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}],
                                        fetch=_fetch({8001: LIVE}), now=7000.)["accounts"]
    assert account["online"] is True and account["scan"] == 18442
    assert account["error"] is None
    assert account["workshop"]["totals"] == {"attack": 0, "defense": 0, "utility": 0}


def test_an_unexpected_read_records_failure_keeps_live_status_and_sets_the_error(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review Focus: any other read_records failure must not blank the whole account."""
    _registered(tmp_path, "Air_1", "account-a", 8001)

    def broken(*args: Any) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(state_view, "read_records", broken)
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}],
                                        fetch=_fetch({8001: LIVE}), now=7000.)["accounts"]
    assert account["online"] is True and account["scan"] == 18442
    assert account["bot"]["screen"] == "IN_RUN"
    assert account["error"] == "Worker database unreadable"


def test_an_incomplete_read_is_treated_as_offline_not_blank(tmp_path: Path) -> None:
    """Review Focus: http.client.HTTPException (IncompleteRead, BadStatusLine) must be
    caught the same as OSError/ValueError, so the account still builds from its DB."""
    root = _registered(tmp_path, "Air_1", "account-a", 8001)
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO events(seq, ts, type) VALUES (1, 6958.0, 'ScanCompleted')")
    (account,) = state_view.fleet_state(
        tmp_path, [{"name": "Air_1"}],
        fetch=_fetch({8001: http.client.IncompleteRead(b"")}), now=7000.)["accounts"]
    assert account["online"] is False
    assert account["workshop"] is not None and account["error"] is None


def test_a_locked_database_errors_one_account_and_leaves_the_others(tmp_path: Path) -> None:
    """Review Focus: one worker holds a write lock while the page polls."""
    locked = _registered(tmp_path, "Air_1", "account-a", 8001)
    _registered(tmp_path, "Air_2", "account-b", 8002)
    with db.connect(locked / "tower_bot.db") as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    locker = sqlite3.connect(locked / "tower_bot.db")
    locker.execute("BEGIN EXCLUSIVE")
    try:
        accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                          fetch=_fetch({}), now=7000.)["accounts"]
    finally:
        locker.rollback()
        locker.close()
    assert accounts[0]["error"].startswith("Worker database unavailable")
    assert accounts[0]["workshop"] is None
    assert accounts[1]["error"] is None and accounts[1]["workshop"] is not None


def test_workers_are_fetched_concurrently(tmp_path: Path) -> None:
    _registered(tmp_path, "Air_1", "account-a", 8001)
    _registered(tmp_path, "Air_2", "account-b", 8002)
    both = threading.Barrier(2, timeout=2)

    def waiting() -> dict[str, Any]:
        both.wait()  # raises BrokenBarrierError if the two fetches were sequential
        return LIVE

    accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                      fetch=_fetch({8001: waiting, 8002: waiting}),
                                      now=7000.)["accounts"]
    assert [account["online"] for account in accounts] == [True, True]


def test_unregistered_and_foreign_workers_are_error_accounts(tmp_path: Path) -> None:
    root = _registered(tmp_path, "Air_2", "account-b", 8002)
    (root / "tower_bot.db").unlink()
    db.bind_account(root / "tower_bot.db", "account-other")
    accounts = state_view.fleet_state(tmp_path, [{"name": "Air_1"}, {"name": "Air_2"}],
                                      fetch=_fetch({}), now=7000.)["accounts"]
    assert [account["error"] for account in accounts] == [
        "Worker is not registered to an account", "Worker database belongs to another account"]


def test_a_failing_builder_blanks_only_its_own_section(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _registered(tmp_path, "Air_1", "account-a", 8001)

    def broken(*args: Any) -> dict[str, Any]:
        raise KeyError("boom")

    monkeypatch.setattr(state_view, "build_cards", broken)
    (account,) = state_view.fleet_state(tmp_path, [{"name": "Air_1"}], fetch=_fetch({}),
                                        now=7000.)["accounts"]
    assert account["cards"] is None
    assert account["labs"] is not None and account["workshop"] is not None
    assert account["error"] is None


def _client(root: Path, names: tuple[str, ...]) -> TestClient:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [{"name": name} for name in names])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: {"Air_9"})
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def test_the_state_endpoint_lists_visible_members_in_name_order(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_view, "urlopen", _fetch({}))
    _registered(tmp_path, "Air_2", "account-b", 8002)
    response = _client(tmp_path, ("Air_2", "Air_9", "Air_1")).get("/api/fleet/state")
    assert response.status_code == 200
    assert [account["id"] for account in response.json()["accounts"]] == ["Air_1", "Air_2"]


def test_the_state_endpoint_is_unavailable_without_the_fleet() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None))
    assert client.get("/api/fleet/state").status_code == 503


def test_state_snapshot_filters_out_hidden_members(tmp_path: Path) -> None:
    """Review Focus: FleetSetupService.state_snapshot must not surface hidden members."""
    fleet = FleetSetupService(tmp_path, qualification_root=tmp_path / "qualifications")
    fleet._reroll_pool = SimpleNamespace(
        members=lambda: [{"name": "Air_1"}, {"name": "Air_9"}])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: {"Air_9"})
    accounts = fleet.state_snapshot()["accounts"]
    assert [account["id"] for account in accounts] == ["Air_1"]
