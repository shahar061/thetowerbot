from __future__ import annotations

import json
from pathlib import Path

import db
from account_state import AccountState
from fleet.reroll_progress import RerollProgress
from fleet.reroll_planner import RerollFacts, choose_next


def worker(root: Path) -> RerollProgress:
    db.bind_account(root / "tower_bot.db", "ACCOUNT-A")
    progress = RerollProgress(root, "ACCOUNT-A", AccountState())
    progress._utility_spent = lambda: None
    return progress


def test_observed_lab_discount_change_invalidates_price(tmp_path: Path, monkeypatch) -> None:
    progress = worker(tmp_path)
    levels = [{"concept_id": "labs.workshop-attack-discount", "status": "verified", "value": 1}]
    monkeypatch.setattr(progress.account_state, "snapshot", lambda: {"revision": {"lab_levels": levels}})
    progress.observe_prices({"damage": 30}, 100)
    assert progress._pricing({})[1]["damage"].level is None
    levels[0]["value"] = 2
    assert "damage" not in progress._pricing({})[1]


def test_delayed_store_events_already_in_observed_frame_are_not_counted_twice(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"damage": 55}, 70)
    observed = progress.price_memory.wallet["observed_at"]
    with db.connect(tmp_path / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                     "VALUES(?,'WORKSHOP_BUY','Damage','ATTACK','coins',-30,0,?)",
                     (observed - .1, json.dumps({"verdict": "bought"})))
        db.finish_run(conn, 1, started_at=observed - 30, ended_at=observed - 2,
                      wave=4, coins=20, tier=1, abandoned=False, scan_count=0, tap_count=0)
    wallet, quotes = progress._pricing({"damage": 1})
    assert wallet == 70
    assert quotes["damage"].price == 55
    with db.connect(tmp_path / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) "
                     "VALUES(?,'WORKSHOP_BUY','Damage','ATTACK','coins',-55,0,?)",
                     (observed + 1, json.dumps({"verdict": "bought"})))
    wallet, quotes = progress._pricing({"damage": 2})
    assert wallet == 15
    assert quotes["damage"].price == 88


def test_prices_for_all_seen_rows_survive_restart_and_earned_coins_update_plan(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"damage": 30, "attack_speed": 30, "critical_chance": 20}, 17)
    assert progress.decision().state == "save_coins"
    with db.connect(tmp_path / "tower_bot.db") as conn:
        observed = progress.price_memory.wallet["observed_at"]
        db.finish_run(conn, 1, started_at=observed + 1, ended_at=observed + 2, wave=4, coins=13, tier=1,
                      abandoned=False, scan_count=0, tap_count=0)
    restarted = worker(tmp_path)
    assert restarted.decision().price == 30
    assert restarted.decision().wallet_coins == 30
    assert restarted.workshop_worthwhile()
    assert restarted.price_memory.quotes({})["attack_speed"].price == 30


def test_no_forced_workshop_visit_after_ten_runs_when_still_unaffordable(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"damage": 1000}, 0)
    with db.connect(tmp_path / "tower_bot.db") as conn:
        observed = progress.price_memory.wallet["observed_at"]
        for i in range(1, 12):
            db.finish_run(conn, i, started_at=observed + i, ended_at=observed + i + .5, wave=4, coins=1,
                          tier=1, abandoned=False, scan_count=0, tap_count=0)
    assert not progress.workshop_worthwhile()
    assert progress.decision().wallet_coins == 11


def test_unproven_purchase_stops_level_projection_and_unknown_wallet_requests_reconciliation(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"damage": 30}, 60)
    with db.connect(tmp_path / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,dry_run,detail) VALUES(?, 'WORKSHOP_BUY','Damage','ATTACK','coins',NULL,0,?)",
                     (progress.price_memory.wallet["observed_at"] + 1, json.dumps({"verdict": "unproven"})))
    assert progress.decision().price is None
    assert progress.decision().wallet_coins is None


def test_real_unconfirmed_skip_invalidates_price_and_wallet(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"damage": 30}, 60)
    with db.connect(tmp_path / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,item,reason,currency,delta,dry_run,detail) "
                     "VALUES(?,'BUY_SKIPPED','Damage','unconfirmed',NULL,0,0,'{}')",
                     (progress.price_memory.wallet["observed_at"] + 1,))
    wallet, quotes = progress._pricing({})
    assert wallet is None
    assert "damage" not in quotes


def test_cheap_filler_never_opens_workshop_just_to_discover_unknown_price() -> None:
    owned = {"unlock_defense_upgrades": 1, "unlock_thorns": 1,
             "unlock_cash_bonuses": 1, "unlock_coin_bonuses": 1}
    base = RerollFacts("a", best_tier_1_wave=20, purchases=owned, utility_spent_coins=350,
                       wallet_coins=10, prices={"defense_absolute": 1000, "thorns": 1000})
    result = choose_next(base)
    assert not result.filler
    assert result.state == "save_coins"


def test_survival_starter_precedes_utility_and_skips_expensive_starter_levels() -> None:
    facts = RerollFacts("a", utility_spent_coins=0, wallet_coins=60,
                        prices={"damage": 30, "attack_speed": 30, "health": 30})
    first = choose_next(facts)
    assert first.starter and first.upgrade_id == "damage"
    from dataclasses import replace
    expensive = choose_next(replace(facts, prices={"damage": 383, "attack_speed": 30, "health": 30}))
    assert expensive.starter and expensive.upgrade_id == "attack_speed"
    done = choose_next(replace(facts, purchases={"damage": 1, "attack_speed": 1, "health": 1,
                                                "unlock_defense_upgrades": 1, "defense_absolute": 1}))
    assert not done.starter and done.upgrade_id == "unlock_cash_bonuses"


def _unexplained(root: Path, ts: float, delta: int, balance: int) -> None:
    with db.connect(root / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,currency,delta,balance_after,observed,dry_run) "
                     "VALUES(?,'UNEXPLAINED','coins',?,?,?,0)", (ts, delta, balance, balance))


def test_a_rounding_sized_unexplained_debit_keeps_workshop_prices(tmp_path: Path) -> None:
    # A lab visit read the wallet one coin under the ledger's balance. No
    # Workshop purchase costs one coin, so it cannot have moved any price;
    # dropping every quote here stalled a strategy worker for good.
    progress = worker(tmp_path)
    progress.observe_prices({"thorns": 206, "defense_absolute": 254}, 29)
    observed = progress.price_memory.wallet["observed_at"]
    _unexplained(tmp_path, observed + 5, -1, 450)
    assert {uid: quote.price for uid, quote in progress._pricing({})[1].items()
            if uid in {"thorns", "defense_absolute"}} == {"thorns": 206, "defense_absolute": 254}


def test_an_unexplained_debit_that_could_be_a_purchase_still_drops_prices(tmp_path: Path) -> None:
    progress = worker(tmp_path)
    progress.observe_prices({"thorns": 206}, 400)
    observed = progress.price_memory.wallet["observed_at"]
    _unexplained(tmp_path, observed + 5, -30, 370)
    assert "thorns" not in progress._pricing({})[1]


def test_a_rounding_line_never_invalidates_workshop_prices(tmp_path: Path) -> None:
    # An abbreviated "12.3K" header hides up to 100 coins; the ledger books
    # that gap as ROUNDING, and only UNEXPLAINED may drop observed prices.
    progress = worker(tmp_path)
    progress.observe_prices({"thorns": 206}, 12_360)
    observed = progress.price_memory.wallet["observed_at"]
    with db.connect(tmp_path / "tower_bot.db") as conn:
        conn.execute("INSERT INTO ledger(ts,kind,currency,delta,balance_after,observed,dry_run) "
                     "VALUES(?,'ROUNDING','coins',-60,12300,12300,0)", (observed + 5,))
    wallet, quotes = progress._pricing({})
    assert (wallet, quotes["thorns"].price) == (12_300, 206)
