"""Reroll progress only trusts a bound database and confirmed purchases."""

from __future__ import annotations

import json
from pathlib import Path

import db
import pytest
from account_state import AccountState
from fleet.reroll_progress import RerollProgress
from policy import AutopilotPolicy
from strategy import Strategy


def worker(tmp_path: Path, account_id: str = "ACCOUNT-A") -> RerollProgress:
    root = tmp_path / "workers" / "Tiramisu64_20"
    root.mkdir(parents=True)
    db.bind_account(root / "tower_bot.db", account_id)
    return RerollProgress(root, account_id, AccountState())


def test_single_planned_row_uses_existing_shopping_executor(tmp_path: Path) -> None:
    """One row, whichever row the planner picked.

    An untouched account leads with Damage - see
    tests/test_reroll_planner.py.
    """
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
                "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                "VALUES (1,'WORKSHOP_BUY','Damage','ATTACK','coins',0,?)",
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
    progress = worker(tmp_path)
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


def test_stale_price_cannot_authorize_purchase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    progress = worker(tmp_path)
    progress.observe_price("damage", 1000, 50)
    assert progress.decision().state == "buy"
    import fleet.reroll_progress as module
    now = module.time.time()
    monkeypatch.setattr(module.time, "time", lambda: now + 31)
    assert progress.decision().state == "observe_price"


def test_battle_policy_follows_verified_account_stage(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    base = AutopilotPolicy(enabled=True, preset="turtle")
    opening = progress.battle_policy(base)
    assert opening.enabled and opening.preset == "manual"
    assert [rule.upgrade_id for rule in opening.rules] == ["damage", "attack_speed"]
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave) VALUES(1,1,2,1,20)")
    progress.decision()
    later = progress.battle_policy(base)
    assert later.preset == "manual"
    assert [rule.upgrade_id for rule in later.rules] == ["health", "damage", "attack_speed"]


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


def test_published_worker_plan_contains_ten_account_bound_buys(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.shopping_policy(Strategy.from_config().shopping)
    payload = json.loads((progress.root / "reroll-plan.json").read_text())
    assert len(payload["next_purchases"]) == 10
    assert payload["next_purchases"][0]["account_id"] == "ACCOUNT-A"
