"""Reroll progress only trusts a bound database and confirmed purchases."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import db
import pytest
from account_state import AccountState
from fleet.build_route_runtime import BuildRouteRuntime
from fleet.reroll_planner import RerollFacts, choose_next
from fleet.reroll_progress import RerollProgress
from lab_plan import LabDecision
from policy import AutopilotPolicy, choose
from strategy import Strategy


def worker(tmp_path: Path, account_id: str = "ACCOUNT-A") -> RerollProgress:
    root = tmp_path / "workers" / "Tiramisu64_20"
    root.mkdir(parents=True)
    db.bind_account(root / "tower_bot.db", account_id)
    return RerollProgress(root, account_id, AccountState())


def test_lab_check_cadence_survives_worker_restart(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    assert not progress.lab_due(now=1000.)
    with db.connect(progress.root / "tower_bot.db") as connection:
        run_id = 1
        db.start_run(connection, run_id, started_at=900.)
        db.finish_run(connection, run_id, started_at=900., ended_at=950.,
                      wave=30, coins=0, tier=1, abandoned=False,
                      scan_count=1, tap_count=0)
        connection.execute(
            "INSERT INTO ledger(ts, kind, item, dry_run) "
            "VALUES(951, 'MILESTONE_CLAIM', 'Unlock Lab', 0)")
    assert progress.lab_due(now=1000.)
    progress.note_lab_observation(
        LabDecision("wait_coins", price=300, wallet_coins=122,
                    game_speed_level=1), now=1000.)
    progress.note_lab_slot2("locked", 65, now=1000.)
    restarted = RerollProgress(progress.root, "ACCOUNT-A", AccountState())
    assert not restarted.lab_due(now=1100., wallet_coins=122, wallet_gems=65)
    assert restarted.lab_due(now=1300., wallet_coins=300, wallet_gems=65)
    restarted.note_labs_unavailable(now=1301.)
    assert not restarted.lab_due(now=1400., wallet_coins=300, wallet_gems=65)
    assert restarted.lab_due(now=1901., wallet_coins=300, wallet_gems=65)


def test_first_workshop_visit_is_due_until_a_purchase_is_confirmed(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    assert progress.initial_workshop_due()
    complete_starter(progress)
    assert not progress.initial_workshop_due()


def test_reroll_holds_card_gems_until_second_lab_is_owned(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    base = Strategy.from_config().shopping
    enabled = replace(base, cards=replace(base.cards, enabled=True, gem_floor=0))
    assert not progress.shopping_policy(enabled).cards.enabled
    progress.note_lab_slot2("owned", 19, now=1000.)
    assert progress.shopping_policy(enabled).cards.enabled


def worker_without_verified_utility_debits(tmp_path: Path) -> RerollProgress:
    progress = worker(tmp_path)
    progress._utility_spent = lambda: None
    return progress



def complete_starter(progress: RerollProgress) -> None:
    with db.connect(progress.root / "tower_bot.db") as conn:
        for name, category in (("Damage", "ATTACK"), ("Attack Speed", "ATTACK"),
                               ("Health", "DEFENSE"), ("Unlock Defense Upgrades", "DEFENSE"),
                               ("Defense Absolute", "DEFENSE")):
            conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                         "VALUES(1,'WORKSHOP_BUY',?,?,'coins',-30,0,?)",
                         (name, category, json.dumps({"verdict": "bought"})))

def test_utility_budget_counts_only_proven_workshop_coin_debits(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    complete_starter(progress)
    with db.connect(progress.root / "tower_bot.db") as connection:
        for delta, dry_run, verdict in ((-40, 0, "bought"),
                                        (-50, 1, "bought"),
                                        (-60, 0, "unproven")):
            connection.execute(
                "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                "VALUES(1,'WORKSHOP_BUY','Unlock Cash Bonuses','UTILITY','coins',?,?,?)",
                (delta, dry_run, json.dumps({"verdict": verdict})))
    assert progress._utility_spent() == 40
    assert progress.decision().upgrade_id == "cash_per_wave"
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
            "VALUES(1,'WORKSHOP_BUY','Cash / Wave','UTILITY','coins',NULL,0,?)",
            (json.dumps({"verdict": "bought"}),))
    assert progress._utility_spent() is None


def test_early_utility_visit_cannot_spend_past_400_coins(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    complete_starter(progress)
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
            "VALUES(1,'WORKSHOP_BUY','Unlock Cash Bonuses','UTILITY','coins',-340,0,?)",
            (json.dumps({"verdict": "bought"}),))
    policy = progress.shopping_policy(replace(Strategy.from_config().shopping,
                                             coin_budget=None))
    assert policy.workshop[0].category == "UTILITY"
    assert policy.coin_budget == 60


def test_known_cheap_filler_uses_bounded_budget_after_starter(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    complete_starter(progress)
    progress.observe_prices({"unlock_cash_bonuses": 200, "attack_speed": 15}, 100)
    assert progress.decision().filler
    policy = progress.shopping_policy(replace(Strategy.from_config().shopping, coin_budget=None))
    assert policy.workshop[0].name == "Attack Speed"
    assert policy.coin_budget == 20
    restarted = RerollProgress(progress.root, "ACCOUNT-A", AccountState())
    assert restarted.decision().filler
    assert restarted.shopping_policy(replace(Strategy.from_config().shopping, coin_budget=None)).coin_budget == 20


def test_cheap_defense_filler_can_spend_its_known_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = worker(tmp_path)
    plan = choose_next(RerollFacts(
        account_id="ACCOUNT-A", best_tier_1_wave=25,
        purchases={"unlock_defense_upgrades": 1, "unlock_thorns": 1,
                   "defense_absolute": 5, "thorns": 7},
        values={"thorns": 7.}, wallet_coins=300,
        prices={"defense_absolute": 254, "thorns": 409},
        draw_sharpness=None,
    ))
    assert plan.filler and plan.upgrade_id == "defense_absolute"
    monkeypatch.setattr(progress, "decision", lambda: plan)
    monkeypatch.setattr(progress, "_publish", lambda decision: None)
    policy = progress.shopping_policy(replace(Strategy.from_config().shopping,
                                              coin_budget=None))
    assert policy.workshop[0].name == "Defense Absolute"
    assert policy.coin_budget == 254


def test_single_planned_row_uses_existing_shopping_executor(tmp_path: Path) -> None:
    """A fresh account establishes survival before utility spending."""
    progress = worker(tmp_path)
    base = Strategy.from_config().shopping
    policy = progress.shopping_policy(base)
    assert [(row.name, row.category) for row in policy.workshop] == [
        ("Damage", "ATTACK")]
    assert policy.enabled == base.enabled and policy.armed == base.armed
    record = json.loads((progress.root / "reroll-plan.json").read_text())
    assert record["account_id"] == "ACCOUNT-A"
    assert record["state"] == "observe_price"


def test_reroll_unlock_rule_is_armed_even_if_saved_strategy_disabled_unlocks(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    complete_starter(progress)
    with db.connect(progress.root / "tower_bot.db") as connection:
        # Two of each puts attack at its allowance, so only unlocks remain
        # buyable - whichever one the draw picks.
        for item in ("Damage", "Attack Speed") * 2:
            connection.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                               "VALUES(1,'WORKSHOP_BUY',?,'ATTACK','coins',0,?)",
                               (item, json.dumps({"verdict": "bought"})))
    policy = progress.shopping_policy(Strategy.from_config().shopping)
    assert policy.workshop[0].name.startswith("Unlock ")
    assert policy.allow_unlocks is True
    assert policy.coin_budget_pct is None


def test_only_confirmed_ledger_purchase_advances_plan(tmp_path: Path) -> None:
    """An unproven debit is not a purchase, so it must not move the plan.

    Was one insert of an unproven and a bought Damage, asserting the plan
    had moved on by exactly one row (`attack_speed`). A milestone is met or
    not rather than divided by a purchase count, so counting one Damage or
    two now gives the same answer and that assertion no longer separates the
    two cases at all. The rows are inserted one at a time instead, against
    the row the planner actually recommends, which puts the unproven debit
    and the confirmed one on either side of a visible change of plan.
    """
    progress = worker(tmp_path)
    path = progress.root / "tower_bot.db"
    planned = progress.decision().upgrade_id

    def buy(verdict: str) -> None:
        with db.connect(path) as connection:
            connection.execute(
                "INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                "VALUES (1,'WORKSHOP_BUY','Damage','ATTACK','coins',-30,0,?)",
                (json.dumps({"verdict": verdict}),))

    buy("unproven")
    assert progress.decision().upgrade_id == planned
    buy("bought")
    assert progress.decision().upgrade_id != planned


def test_visible_granted_rows_advance_unlock_after_unproven_debit(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    with db.connect(progress.root / "tower_bot.db") as connection:
        for item in ("Damage", "Attack Speed"):
            connection.execute("INSERT INTO ledger(ts,kind,item,category,dry_run,detail) "
                               "VALUES(1,'WORKSHOP_BUY',?,'ATTACK',0,?)",
                               (item, json.dumps({"verdict": "bought"})))
        connection.execute("INSERT INTO ledger(ts,kind,item,category,reason,detail) "
                           "VALUES(2,'BUY_SKIPPED','Unlock Cash Bonuses','UTILITY',"
                           "'already_unlocked',?)",
                           (json.dumps({"detail": "the rows it grants are on the tab"}),))
    assert progress._history()[1]["unlock_cash_bonuses"] == 1
    # Attack is still under its allowance (1 of 2), so the next pick may be
    # attack; what matters is that the proven unlock is not planned again.
    assert progress.decision().upgrade_id != "unlock_cash_bonuses"
    assert "cash_per_wave" in [r.upgrade_id for r in progress.battle_policy(
        AutopilotPolicy(enabled=True, preset="turtle")).rules]


def test_price_and_wallet_are_fresh_and_specific_to_planned_item(tmp_path: Path) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    # "damage" is the planned row; "attack_speed" is the unplanned row whose
    # price must be ignored.
    progress.observe_price("attack_speed", 100, 10)
    assert progress.decision().state == "observe_price"
    progress.observe_price("damage", 80, 120)
    assert progress.decision().state == "save_coins"
    progress.observe_price("damage", 130, 120)
    assert progress.decision().state == "buy"


def test_changed_database_account_blocks_planning(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.account_id = "ACCOUNT-B"
    with pytest.raises(ValueError, match="binding changed"):
        progress.shopping_policy(Strategy.from_config().shopping)


def test_verified_account_readings_and_lifetime_coins_feed_decision(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    class Readings:
        def snapshot(self) -> dict:
            return {"revision": {"workshop_stats": [
                {"concept_id": "stats.thorns", "value": 51, "status": "verified"}]},
                "screen_readings": {"readings": [{"screen_id": "account.stats.summary",
                    "observed_at": 10, "fields": [{"key": "coins_earned",
                    "status": "observed", "raw_value": "1.5T"}]}]}}
    progress.account_state = Readings()
    assert progress.decision().lifetime_coins == 1_500_000_000_000
    restarted = RerollProgress(progress.root, "ACCOUNT-A", AccountState())
    assert restarted.decision().lifetime_coins == 1_500_000_000_000
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave,coins) "
                           "VALUES(2,11,12,1,10,250)")
    assert restarted.decision().lifetime_coins == 1_500_000_000_250


def test_stats_summary_persists_game_start_and_recent_coin_rate(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    class Readings:
        def snapshot(self) -> dict:
            return {"revision": {}, "screen_readings": {"readings": [{
                "screen_id": "account.stats.summary", "observed_at": 10,
                "fields": [
                    {"key": "game_started", "status": "observed", "raw_value": "August 29 2026"},
                    {"key": "coins_earned", "status": "observed", "raw_value": "1.5K"},
                    {"key": "recent_coins_per_hour", "status": "observed", "raw_value": "720"},
                ],
            }]}}
    progress.account_state = Readings()
    progress.decision()
    saved = json.loads((progress.root / "reroll-lifetime.json").read_text())
    assert saved["game_started"] == "2026-08-29"
    assert saved["recent_coins_per_hour"] == 720


def test_read_only_route_facts_do_not_persist_lifetime_summary(tmp_path: Path) -> None:
    root = tmp_path / "workers" / "Tiramisu64_20"
    root.mkdir(parents=True)
    db.bind_account(root / "tower_bot.db", "ACCOUNT-A")
    progress = RerollProgress(root, "ACCOUNT-A", AccountState(), read_only=True)

    class Readings:
        def snapshot(self) -> dict:
            return {"revision": {}, "screen_readings": {"readings": [{
                "screen_id": "account.stats.summary", "observed_at": time.time(),
                "fields": [{"key": "coins_earned", "status": "observed", "raw_value": "1500"}],
            }]}}

    progress.account_state = Readings()
    progress.route_runtime = BuildRouteRuntime(tmp_path, root.name, "ACCOUNT-A")
    facts = progress.route_facts()

    assert facts.lifetime_coins == 1500
    assert not (root / "reroll-lifetime.json").exists()


def test_unreadable_game_start_or_rate_does_not_create_a_fake_stat(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    class Readings:
        def snapshot(self) -> dict:
            return {"revision": {}, "screen_readings": {"readings": [{
                "screen_id": "account.stats.summary", "observed_at": 10,
                "fields": [
                    {"key": "game_started", "status": "unreadable", "raw_value": "August 29 2026"},
                    {"key": "coins_earned", "status": "observed", "raw_value": "1500"},
                    {"key": "recent_coins_per_hour", "status": "unreadable", "raw_value": "720"},
                ],
            }]}}
    progress.account_state = Readings()
    progress.decision()
    saved = json.loads((progress.root / "reroll-lifetime.json").read_text())
    assert "game_started" not in saved
    assert "recent_coins_per_hour" not in saved


def test_lifetime_coins_from_another_account_are_rejected(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    (progress.root / "reroll-lifetime.json").write_text(json.dumps({
        "account_id": "ACCOUNT-B", "lifetime_coins": 500, "observed_at": 10,
    }))
    assert progress.decision().lifetime_coins is None


def test_observed_price_survives_time_until_account_state_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    progress.observe_price("damage", 1000, 50)
    assert progress.decision().state == "buy"
    import fleet.reroll_progress as module
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + 3600)
    assert progress.decision().state == "buy"
    # This remains a plan: Shopping still verifies the on-screen price.


def test_battle_policy_follows_verified_account_stage(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    base = AutopilotPolicy(enabled=True, preset="turtle")
    opening = progress.battle_policy(base)
    assert opening.enabled and opening.preset == "manual"
    assert [rule.upgrade_id for rule in opening.rules] == ["damage", "attack_speed", "health"]
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave) VALUES(1,1,2,1,20)")
    progress.decision()
    later = progress.battle_policy(base)
    assert later.preset == "manual"
    assert [rule.upgrade_id for rule in later.rules] == ["damage", "attack_speed", "health"]


def test_battle_policy_only_uses_confirmed_workshop_unlocks(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    base = AutopilotPolicy(enabled=True, preset="turtle")
    path = progress.root / "tower_bot.db"
    with db.connect(path) as connection:
        connection.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                           "VALUES(1,'WORKSHOP_BUY','Unlock Cash Bonuses','UTILITY','coins',0,?)",
                           (json.dumps({"verdict": "unproven"}),))
    assert all(rule.upgrade_id != "cash_per_wave" for rule in progress.battle_policy(base).rules)
    with db.connect(path) as connection:
        connection.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                           "VALUES(2,'WORKSHOP_BUY','Unlock Cash Bonuses','UTILITY','coins',0,?)",
                           (json.dumps({"verdict": "bought"}),))
    opening = progress.battle_policy(base)
    assert "cash_per_wave" in [rule.upgrade_id for rule in opening.rules]
    assert "coins_per_wave" not in [rule.upgrade_id for rule in opening.rules]
    assert "damage" in [rule.upgrade_id for rule in opening.rules]


def test_opening_battle_prioritizes_verified_turtle_defense(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    with db.connect(progress.root / "tower_bot.db") as connection:
        for item in ("Unlock Defense Upgrades", "Unlock Thorns"):
            connection.execute("INSERT INTO ledger(ts,kind,item,category,dry_run,detail) "
                               "VALUES(1,'WORKSHOP_BUY',?,'DEFENSE',0,?)",
                               (item, json.dumps({"verdict": "bought"})))
    rules = progress.battle_policy(AutopilotPolicy(enabled=True, preset="turtle")).rules
    assert [rule.upgrade_id for rule in rules[:2]] == ["defense_absolute", "thorns"]


def test_battle_uses_affordable_economy_when_turtle_defense_is_too_costly(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    with db.connect(progress.root / "tower_bot.db") as connection:
        for item, category in (("Unlock Defense Upgrades", "DEFENSE"),
                               ("Unlock Thorns", "DEFENSE"),
                               ("Unlock Cash Bonuses", "UTILITY"),
                               ("Unlock Coin Bonuses", "UTILITY")):
            connection.execute(
                "INSERT INTO ledger(ts,kind,item,category,dry_run,detail) "
                "VALUES(1,'WORKSHOP_BUY',?,?,0,?)",
                (item, category, json.dumps({"verdict": "bought"})))
        connection.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave) "
                           "VALUES(1,1,2,1,20)")
    policy = progress.battle_policy(AutopilotPolicy(enabled=True, preset="turtle"))
    assert [rule.upgrade_id for rule in policy.rules[:2]] == ["defense_absolute", "thorns"]
    observations = {
        "defense_absolute": {"status": "available", "value": 10, "price": 100},
        "thorns": {"status": "available", "value": 10, "price": 80},
        "health": {"status": "available", "value": 20, "price": 90},
        "cash_per_wave": {"status": "available", "value": 10, "price": 2},
        "coins_per_kill_bonus": {"status": "available", "value": 1.1, "price": 5},
        "cash_bonus": {"status": "available", "value": 1.1, "price": 6},
        "damage": {"status": "available", "value": 12, "price": 7},
    }
    observations["cash_per_wave"]["value"] = 9
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "cash_per_wave"
    observations["cash_per_wave"]["value"] = 10
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "coins_per_kill_bonus"
    observations["coins_per_kill_bonus"]["value"] = 1.25
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "cash_bonus"
    observations["cash_bonus"]["value"] = 1.25
    policy = progress.battle_policy(AutopilotPolicy(enabled=True, preset="turtle"), observations)
    assert choose(policy, observations, {"cash": 20}).upgrade_id == "damage"


def test_published_worker_plan_contains_ten_account_bound_buys(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.shopping_policy(Strategy.from_config().shopping)
    payload = json.loads((progress.root / "reroll-plan.json").read_text())
    assert len(payload["next_purchases"]) == 10
    assert payload["next_purchases"][0]["account_id"] == "ACCOUNT-A"
    assert payload["confirmed_purchases"] == progress._history()[1]
    assert payload["price_source"] in {"observed", "catalog_estimate", None}


def test_the_worker_variant_file_reaches_the_planner(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    (progress.root / "reroll-variant.json").write_text('{"variant": "income_first"}')
    assert "(Income first)" in progress.decision().reason


def end_run(progress: RerollProgress, run_id: int, coins: int | None) -> None:
    ended_at = time.time()
    with db.connect(progress.root / "tower_bot.db") as connection:
        db.finish_run(connection, run_id, started_at=ended_at - .5, ended_at=ended_at,
                          wave=5, coins=coins, tier=1, abandoned=False,
                          scan_count=0, tap_count=0)


def test_workshop_visit_is_worthwhile_until_a_price_is_known(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    assert progress.workshop_worthwhile()


def test_unaffordable_target_skips_visits_until_run_coins_cover_it(tmp_path: Path) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    end_run(progress, 1, 10)
    progress.observe_price("damage", 80, 120)
    assert not progress.workshop_worthwhile()
    base = Strategy.from_config().shopping
    assert not progress.shopping_policy(base).enabled
    end_run(progress, 2, 25)
    assert not progress.workshop_worthwhile()
    end_run(progress, 3, None)
    end_run(progress, 4, 15)
    assert progress.workshop_worthwhile()
    assert progress.shopping_policy(base).workshop


def test_cached_target_survives_a_restart(tmp_path: Path) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    progress.observe_price("damage", 80, 120)
    restarted = RerollProgress(progress.root, "ACCOUNT-A", AccountState())
    restarted._utility_spent = lambda: None
    assert not restarted.workshop_worthwhile()


def test_cached_target_is_ignored_for_another_account(tmp_path: Path) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    progress.observe_price("damage", 80, 120)
    record = json.loads((progress.root / "workshop-prices.json").read_text())
    record["account_id"] = "ACCOUNT-B"
    (progress.root / "workshop-prices.json").write_text(json.dumps(record))
    restarted = RerollProgress(progress.root, "ACCOUNT-A", AccountState())
    assert restarted.workshop_worthwhile()


def test_changed_plan_or_unread_price_forces_a_visit(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_price("damage", None, 120)
    assert progress.workshop_worthwhile()
    progress.observe_price("damage", 80, None)
    assert progress.workshop_worthwhile()
    progress.observe_price("damage", 80, 120)
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute(
            "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
            "VALUES (1,'WORKSHOP_BUY','Damage','ATTACK','coins',0,?)",
            (json.dumps({"verdict": "bought"}),))
    assert progress.workshop_worthwhile()


def test_unaffordable_price_does_not_force_a_ten_run_detour(tmp_path: Path) -> None:
    progress = worker_without_verified_utility_debits(tmp_path)
    progress.observe_price("damage", 0, 10_000)
    for run_id in range(1, 10):
        end_run(progress, run_id, 1)
    assert not progress.workshop_worthwhile()
    end_run(progress, 10, 1)
    assert not progress.workshop_worthwhile()
