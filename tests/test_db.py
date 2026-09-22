from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import db


def make_db(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(tmp_path / "bot.db")


def a_row(seq: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "seq": seq,
        "run_id": 1,
        "ts": 1000.0 + seq,
        "type": "Tapped",
        "screen": "IN_RUN",
        "action": "Damage",
        "reason": None,
        "score": 0.94,
        "price": 120,
        "wallet": 300,
        "detail": '{"x": 840, "y": 1520}',
    }
    row.update(overrides)
    return row


def a_line(**overrides: object) -> dict[str, object]:
    line: dict[str, object] = {
        "seq": 1, "ts": 1000.0, "kind": "WORKSHOP_BUY", "item": "Damage",
        "category": "ATTACK", "currency": "coins", "delta": -120,
        "price": 120, "balance_after": 1650, "observed": 1770,
        "dry_run": 0, "run_id": None, "visit": 3, "reason": None,
        "detail": None,
    }
    line.update(overrides)
    return line


def test_an_inserted_event_reads_back_with_its_detail_decoded(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=999.0)
    db.insert_event(conn, a_row(1))

    stored = db.run_events(conn, 1)

    assert len(stored) == 1
    assert stored[0]["action"] == "Damage"
    assert stored[0]["detail"] == {"x": 840, "y": 1520}


def test_seq_seeds_from_the_stored_maximum(tmp_path: Path) -> None:
    """A restart must not reset the counter onto rows that already exist."""
    conn = make_db(tmp_path)
    assert db.max_seq(conn) == 0

    db.insert_event(conn, a_row(7))
    assert db.max_seq(conn) == 7


def test_run_ids_seed_from_the_stored_maximum(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    assert db.max_run_id(conn) == 0

    db.start_run(conn, 4, started_at=1.0)
    assert db.max_run_id(conn) == 4


def test_best_wave_is_none_on_an_empty_table(tmp_path: Path) -> None:
    """An unread best wave is None, never 0 - the schedule must not guess."""
    conn = make_db(tmp_path)
    assert db.best_wave(conn) is None


def test_best_wave_ignores_rows_whose_wave_is_null(tmp_path: Path) -> None:
    """A started-but-unfinished run has a NULL wave and must not count as 0."""
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=1.0)

    assert db.best_wave(conn) is None


def test_best_wave_is_the_highest_wave_any_run_reached(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.finish_run(
        conn, 1, started_at=0.0, ended_at=10.0, wave=80, coins=100,
        tier=1, abandoned=False, scan_count=5, tap_count=5,
    )
    db.finish_run(
        conn, 2, started_at=10.0, ended_at=20.0, wave=137, coins=200,
        tier=2, abandoned=False, scan_count=5, tap_count=5,
    )
    db.finish_run(
        conn, 3, started_at=20.0, ended_at=30.0, wave=None, coins=None,
        tier=None, abandoned=True, scan_count=1, tap_count=0,
    )

    assert db.best_wave(conn) == 137


def test_finishing_a_run_keeps_the_start_it_was_opened_with(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=100.0)

    db.finish_run(
        conn, 1, started_at=0.0, ended_at=250.0, wave=137, coins=4200,
        tier=3, abandoned=False, scan_count=61, tap_count=12,
    )

    run = db.list_runs(conn)[0]
    assert run["started_at"] == 100.0
    assert (run["ended_at"], run["wave"], run["coins"], run["tier"]) == (250.0, 137, 4200, 3)
    assert (run["scan_count"], run["tap_count"], run["abandoned"]) == (61, 12, 0)


def test_finishing_an_unseen_run_still_records_it(tmp_path: Path) -> None:
    """A run opened before a crash must not vanish because its row is missing."""
    conn = make_db(tmp_path)

    db.finish_run(
        conn, 9, started_at=100.0, ended_at=160.0, wave=None, coins=None,
        tier=None, abandoned=True, scan_count=3, tap_count=0,
    )

    run = db.list_runs(conn)[0]
    assert run["id"] == 9
    assert run["started_at"] == 100.0
    assert run["abandoned"] == 1


def test_runs_come_back_newest_first_and_honour_the_limit(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    for run_id in (1, 2, 3):
        db.start_run(conn, run_id, started_at=float(run_id))

    assert [r["id"] for r in db.list_runs(conn, limit=2)] == [3, 2]


def test_pruning_deletes_old_events_and_keeps_recent_ones(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    now = time.time()
    db.insert_event(conn, a_row(1, ts=now - 40 * 86400))
    db.insert_event(conn, a_row(2, ts=now - 1 * 86400))

    removed = db.prune_events(conn, retention_days=30, now=now)

    assert removed == 1
    assert [e["seq"] for e in db.run_events(conn, 1)] == [2]


def test_close_abandoned_runs_backdates_ended_at_to_started_at(tmp_path: Path) -> None:
    """A killed (not stopped) process never fires RunEnded, so the row would
    otherwise keep ended_at IS NULL - the dashboard's "live" badge - forever."""
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=100.0)  # never finished
    db.finish_run(
        conn, 2, started_at=50.0, ended_at=90.0, wave=1, coins=1, tier=1,
        abandoned=False, scan_count=1, tap_count=1,
    )

    closed = db.close_abandoned_runs(conn)

    assert closed == 1
    runs = {r["id"]: r for r in db.list_runs(conn)}
    assert (runs[1]["ended_at"], runs[1]["abandoned"]) == (100.0, 1)
    # A run that already ended cleanly must be left alone.
    assert (runs[2]["ended_at"], runs[2]["abandoned"]) == (90.0, 0)


def test_close_abandoned_runs_is_a_no_op_when_nothing_is_open(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.finish_run(
        conn, 1, started_at=0.0, ended_at=10.0, wave=None, coins=None,
        tier=None, abandoned=False, scan_count=0, tap_count=0,
    )

    assert db.close_abandoned_runs(conn) == 0


def test_a_reader_connection_cannot_write(tmp_path: Path) -> None:
    """The web layer opens these; a bug there must not corrupt the log."""
    path = tmp_path / "bot.db"
    db.connect(path).close()

    with db.reader(path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM events")


def test_a_ledger_page_comes_back_newest_first(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    for seq in (1, 2, 3):
        db.insert_ledger(conn, a_line(seq=seq, ts=1000.0 + seq))

    page = db.ledger_page(conn)

    assert [line["seq"] for line in page] == [3, 2, 1]


def test_rehearsals_are_excluded_unless_asked_for(tmp_path: Path) -> None:
    """A dry-run line has delta 0 by construction, so showing it by default
    would put rows in a running-balance table that cannot move the balance."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, dry_run=0))
    db.insert_ledger(conn, a_line(seq=2, dry_run=1, delta=0))

    assert [line["seq"] for line in db.ledger_page(conn)] == [1]
    assert [line["seq"] for line in db.ledger_page(conn, include_rehearsals=True)] == [2, 1]
    assert db.count_rehearsals(conn) == 1


def test_the_last_balance_per_currency_ignores_lines_that_never_had_one(
    tmp_path: Path,
) -> None:
    """An unreadable price leaves balance_after NULL. That is a hole in the
    chain, not a balance of zero, and seeding a writer from it would invent
    a huge bogus UNEXPLAINED on the next real reading."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="coins", balance_after=1650))
    db.insert_ledger(conn, a_line(seq=2, currency="coins", balance_after=None))
    db.insert_ledger(conn, a_line(seq=3, currency="gems", balance_after=40))

    assert db.last_balances(conn) == {"coins": 1650, "gems": 40}


def test_an_empty_ledger_reports_both_balances_as_unknown(tmp_path: Path) -> None:
    assert db.last_balances(make_db(tmp_path)) == {"coins": None, "gems": None}


def test_the_ledger_lists_every_currency_it_holds_and_nothing_else(
    tmp_path: Path,
) -> None:
    """The page's currency filter is built from this. A currency the history
    holds must be reachable, and one it has never touched must not become a
    filter that can only return nothing."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="stones", balance_after=None))
    db.insert_ledger(conn, a_line(seq=2, currency="coins"))
    db.insert_ledger(conn, a_line(seq=3, currency=None, delta=None, kind="VISIT_START"))
    db.insert_ledger(conn, a_line(seq=4, currency="coins"))

    assert db.ledger_currencies(conn) == ["coins", "stones"]


def test_a_currency_with_no_balance_is_reported_as_unknown_not_dropped(
    tmp_path: Path,
) -> None:
    """Stones reach the ledger with balance_after NULL on every line. Asked
    for by name, the answer is None - present and unknown - never absent,
    which would let the page mistake it for a currency it cannot see."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="coins", balance_after=1650))
    db.insert_ledger(conn, a_line(seq=2, currency="stones", balance_after=None))

    assert db.last_balances(conn, ["coins", "gems", "stones"]) == {
        "coins": 1650, "gems": None, "stones": None,
    }


def test_the_writer_still_seeds_from_exactly_coins_and_gems(tmp_path: Path) -> None:
    """The default is what LedgerWriter seeds itself from. Widening it would
    hand the writer anchors for currencies it never balances."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="stones", balance_after=None))

    assert set(db.last_balances(conn)) == {"coins", "gems"}


def test_ledger_rows_survive_the_event_prune(tmp_path: Path) -> None:
    """The whole reason the ledger is its own table: events age out at 30
    days and an account history that forgets last month is not a history."""
    conn = make_db(tmp_path)
    db.insert_event(conn, a_row(1, ts=0.0))
    db.insert_ledger(conn, a_line(seq=1, ts=0.0))

    removed = db.prune_events(conn, retention_days=30, now=100 * 86400)

    assert removed == 1
    assert len(db.ledger_page(conn)) == 1


def a_purchase(seq: int, **overrides: object) -> dict[str, object]:
    """A stored BattlePurchased row, shaped the way store.to_row writes one:
    `price` in its own column, everything else in the JSON detail blob."""
    detail = {"item": "Damage", "upgrade_id": "damage", "value": 42.0}
    detail.update(overrides.pop("detail", {}))  # type: ignore[arg-type]
    row: dict[str, object] = {
        "seq": seq,
        "run_id": 1,
        "ts": 1000.0 + seq,
        "type": "BattlePurchased",
        "screen": "IN_RUN",
        "action": None,
        "reason": None,
        "score": None,
        "price": 120,
        "wallet": None,
        "detail": json.dumps(detail),
    }
    row.update(overrides)
    return row


def test_run_purchases_returns_only_this_runs_battle_purchases(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=999.0)
    db.insert_event(conn, a_row(1))  # a Tapped, not a purchase
    db.insert_event(conn, a_purchase(2))
    db.insert_event(conn, a_purchase(3, run_id=2))  # another run's purchase

    bought = db.run_purchases(conn, 1)

    assert [p["seq"] for p in bought] == [2]


def test_run_purchases_flattens_the_detail_blob_onto_the_row(tmp_path: Path) -> None:
    """item/upgrade_id/value live in the JSON blob; callers want them flat."""
    conn = make_db(tmp_path)
    db.insert_event(conn, a_purchase(1, price=980, detail={"item": "Health"}))

    purchase = db.run_purchases(conn, 1)[0]

    assert purchase["item"] == "Health"
    assert purchase["upgrade_id"] == "damage"
    assert purchase["price"] == 980
    assert purchase["value"] == 42.0
    assert purchase["ts"] == 1001.0


def test_run_purchases_keeps_an_unreadable_price_as_none(tmp_path: Path) -> None:
    """OCR that could not read the price writes NULL, which is not zero."""
    conn = make_db(tmp_path)
    db.insert_event(conn, a_purchase(1, price=None))

    assert db.run_purchases(conn, 1)[0]["price"] is None


def test_run_purchases_come_back_in_purchase_order(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    for seq in (3, 1, 2):
        db.insert_event(conn, a_purchase(seq))

    assert [p["seq"] for p in db.run_purchases(conn, 1)] == [1, 2, 3]
