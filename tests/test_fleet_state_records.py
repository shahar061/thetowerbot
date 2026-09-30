"""Read-only worker DB reads for the Fleet State page."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import db
from fleet.state_records import RECENT_LIMIT, ForeignDatabase, read_records

BOUGHT = json.dumps({"verdict": "bought"})


def _database(tmp_path: Path, account: str = "account-a") -> Path:
    path = tmp_path / "tower_bot.db"
    db.bind_account(path, account)
    return path


def _ledger(conn: sqlite3.Connection, ts: float, kind: str, item: str, category: str,
            delta: int | None, price: int | None, detail: str = BOUGHT, dry_run: int = 0) -> None:
    conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (ts, kind, item, category, "gems" if kind == "CARD_BUY" else "coins",
                  delta, price, dry_run, detail))


def _run(conn: sqlite3.Connection, run_id: int, tier: int, wave: int, abandoned: int = 0,
         ended: bool = True) -> None:
    conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier, abandoned) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (run_id, 100. * run_id, 100. * run_id + 50 if ended else None, wave, 1000,
                  tier, abandoned))


def test_a_missing_or_foreign_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_records(tmp_path / "absent.db", "account-a", None)
    path = _database(tmp_path, "account-b")
    with pytest.raises(ForeignDatabase):
        read_records(path, "account-a", None)


def test_best_wave_is_per_tier_over_finished_runs_that_were_not_abandoned(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 11, 5020)
        _run(conn, 2, 11, 9999, abandoned=1)
        _run(conn, 3, 7, 1420)
        _run(conn, 4, 11, 4000)
        _run(conn, 5, 11, 8000, ended=False)
    records = read_records(path, "account-a", None)
    assert records.best_waves == {11: 5020, 7: 1420}


def test_the_last_five_finished_runs_are_newest_first(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        for run_id in range(1, 8):
            _run(conn, run_id, 1, 10 * run_id)
        _run(conn, 8, 1, 999, ended=False)
    records = read_records(path, "account-a", None)
    assert [run["id"] for run in records.runs] == [7, 6, 5, 4, 3]


def test_current_run_upgrades_come_from_its_purchase_events(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 1, 10)
        conn.execute("INSERT INTO run_upgrades VALUES (1, 'health', 3, 90, 0)")
        _run(conn, 2, 1, 0, ended=False)
        for seq, upgrade_id in enumerate(["damage", "damage", "health", None], start=1):
            conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                         (seq, 2, 200. + seq, "BattlePurchased",
                          json.dumps({"item": "x", "upgrade_id": upgrade_id, "value": None})))
    current = read_records(path, "account-a", 2)
    assert current.run_upgrades_scope == "current"
    assert sorted((row["upgrade_id"], row["levels"]) for row in current.run_upgrades) == [
        ("damage", 2), ("health", 1)]
    last = read_records(path, "account-a", None)
    assert last.run_upgrades_scope == "last"
    assert [(row["upgrade_id"], row["levels"]) for row in last.run_upgrades] == [("health", 3)]


def test_a_new_account_has_no_runs_no_revision_and_zero_gems_spent(tmp_path: Path) -> None:
    records = read_records(_database(tmp_path), "account-a", None)
    assert (records.revision, records.runs, records.best_waves) == (None, [], {})
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], None)
    assert (records.card_gems, records.workshop_recent, records.last_seen) == (0, [], None)
    assert records.balances == {"coins": None, "gems": None}


def test_a_revision_stamped_with_another_account_is_ignored(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                     (json.dumps({"account_id": "account-old", "workshop_stats": []}),))
    assert read_records(path, "account-a", None).revision is None


def test_foreign_facts_inside_an_account_revision_do_not_supply_workshop_evidence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)", (json.dumps({
            "account_id": "account-a", "workshop_stats": [{
                "concept_id": "stats.thorns", "value": 6, "status": "verified",
                "scope": {"account_id": "account-old"}, "evidence": {"observed_at": 5., "raw_value": "6%"}}],
            "unlocks": [{"concept_id": "unlocks.thorns", "value": True, "status": "verified",
                         "scope": {"account_id": "account-old"}}]}),))
    records = read_records(path, "account-a", None)
    assert records.revision["workshop_stats"] == records.revision["unlocks"] == []
    assert records.workshop_evidence.actions.level_purchases == {}


def test_a_corrupt_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: a bad JSON blob in account_revisions must not blank the account."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('{bad')")
    assert read_records(path, "account-a", None).revision is None


def test_a_non_dict_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: valid JSON that is not an object (e.g. null) must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('null')")
    assert read_records(path, "account-a", None).revision is None


def test_a_list_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: a non-null non-dict JSON value (a list) must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('[1, 2]')")
    assert read_records(path, "account-a", None).revision is None


def test_a_corrupt_live_run_purchase_detail_leaves_run_upgrades_empty(tmp_path: Path) -> None:
    """Review Focus: a bad BattlePurchased detail must empty run_upgrades, not raise."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                     (1, 5, 200., "BattlePurchased", "{bad"))
    records = read_records(path, "account-a", 5)
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], "current")


def test_a_null_live_run_purchase_detail_leaves_run_upgrades_empty(tmp_path: Path) -> None:
    """Review Focus: a null (not object) BattlePurchased detail must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                     (1, 5, 200., "BattlePurchased", "null"))
    records = read_records(path, "account-a", 5)
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], "current")


def test_a_huge_ledger_is_summed_and_bounded_by_sql(tmp_path: Path) -> None:
    """Review Focus: years of ledger rows must not be loaded into Python."""
    path = _database(tmp_path)
    from fleet.workshop_prices import WorkshopPrices
    memory = WorkshopPrices(tmp_path, "account-a")
    memory.observe("damage", 30, 20_000, now=20_000.)
    memory.save()
    with db.connect(path) as conn:
        conn.executemany(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(float(i), "WORKSHOP_BUY", "Damage", "ATTACK", "coins", -10, 10, 0, BOUGHT)
             for i in range(50_000)])
        _ledger(conn, 60_000., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99, dry_run=1)
        _ledger(conn, 60_001., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99,
                detail=json.dumps({"verdict": "unconfirmed"}))
        _ledger(conn, 60_002., "CARD_BUY", "Card", "CARDS", -20, 20)
        _ledger(conn, 60_003., "CARD_BUY", "Card", "CARDS", None, 20)
    started = time.monotonic()
    records = read_records(path, "account-a", None)
    assert time.monotonic() - started < 2
    assert records.workshop_spent == [("Damage", "ATTACK", 500_000)]
    assert records.workshop_evidence.actions.price_purchases == {"damage": 29_999}
    assert records.workshop_evidence.actions.invalidated == {"damage": 60_001.}
    assert "damage" not in records.workshop_evidence.quotes
    assert len(records.workshop_recent) == RECENT_LIMIT
    assert records.workshop_recent[0] == {"ts": 49_999., "item": "Damage", "category": "ATTACK",
                                          "price": 10}
    assert records.card_gems == 20
    assert [row["price"] for row in records.card_recent] == [20, 20]


def test_a_database_locked_mid_write_fails_fast(tmp_path: Path) -> None:
    """Review Focus: a worker holding a write lock costs this read 0.1 s, not a hang."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")  # WAL would let the reader through
    locker = sqlite3.connect(path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            read_records(path, "account-a", None)
        assert time.monotonic() - started < 1
    finally:
        locker.rollback()
        locker.close()
