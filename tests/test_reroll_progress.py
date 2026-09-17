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
    progress = worker(tmp_path)
    base = Strategy.from_config().shopping
    policy = progress.shopping_policy(base)
    assert [(row.name, row.category) for row in policy.workshop] == [("Damage", "ATTACK")]
    assert policy.enabled == base.enabled and policy.armed == base.armed
    record = json.loads((progress.root / "reroll-plan.json").read_text())
    assert record["account_id"] == "ACCOUNT-A"
    assert record["state"] == "observe_price"


def test_only_confirmed_ledger_purchase_advances_plan(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    path = progress.root / "tower_bot.db"
    with db.connect(path) as connection:
        for verdict in ("unproven", "bought"):
            connection.execute(
                "INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) "
                "VALUES (1,'WORKSHOP_BUY','Damage','ATTACK','coins',0,?)",
                (json.dumps({"verdict": verdict}),))
    assert progress.decision().upgrade_id == "attack_speed"


def test_price_and_wallet_are_fresh_and_specific_to_planned_item(tmp_path: Path) -> None:
    progress = worker(tmp_path)
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
    assert [rule.upgrade_id for rule in opening.rules][-2:] == ["attack_speed", "coins_per_kill_bonus"]
    with db.connect(progress.root / "tower_bot.db") as connection:
        connection.execute("INSERT INTO runs(id,started_at,ended_at,tier,wave) VALUES(1,1,2,1,20)")
    progress.decision()
    assert progress.battle_policy(base).preset == "turtle"
