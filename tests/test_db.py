from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import db
import events
from sinks.store import to_row


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


def test_tier_best_waves_are_the_highest_finished_wave_per_tier(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    for run_id, wave, tier in ((1, 80, 1), (2, 126, 1), (3, 40, 2), (4, 999, None)):
        db.finish_run(
            conn, run_id, started_at=0.0, ended_at=10.0, wave=wave, coins=1,
            tier=tier, abandoned=False, scan_count=1, tap_count=0,
        )
    db.start_run(conn, 5, started_at=20.0)

    assert db.tier_best_waves(conn) == {1: 126, 2: 40}


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


def test_close_abandoned_runs_summarizes_purchases_for_each_run_it_closes(tmp_path: Path) -> None:
    """A killed process never reaches finish_run, so without this the run's
    purchases would sit unsummarized in `run_upgrades` until the next
    restart's `db.connect` backfill - see `_backfill_run_upgrades`."""
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=100.0)
    db.insert_event(conn, to_row(events.BattlePurchased(
        seq=1, ts=101.0, item="Damage", upgrade_id="damage", price=10, value=None), 1))

    db.close_abandoned_runs(conn)

    rows = {r["upgrade_id"]: dict(r) for r in conn.execute("SELECT * FROM run_upgrades WHERE run_id = 1")}
    assert rows["damage"]["levels"] == 1


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


def test_one_event_may_write_a_line_in_each_currency(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=501, currency="coins", delta=120))
    db.insert_ledger(conn, a_line(seq=501, currency="gems", delta=5))

    assert {line["currency"] for line in db.ledger_page(conn)} == {"coins", "gems"}


def test_a_redelivered_event_is_still_counted_once_in_every_currency(
    tmp_path: Path,
) -> None:
    """What the unique index is FOR. Widening it to let a second currency in
    must not also let the same event in twice after a restart."""
    conn = make_db(tmp_path)
    for _ in range(2):
        db.insert_ledger(conn, a_line(seq=501, currency="coins", delta=120))
        db.insert_ledger(conn, a_line(seq=501, currency="gems", delta=5))

    assert len(db.ledger_page(conn)) == 2


def test_a_redelivered_event_with_no_currency_is_still_deduplicated(
    tmp_path: Path,
) -> None:
    """The trap in a plain (seq, currency) key: SQLite treats NULLs as
    distinct inside a unique index, so a visit boundary - which has no
    currency - would be let in twice. Hence IFNULL in the index."""
    conn = make_db(tmp_path)
    for _ in range(2):
        db.insert_ledger(conn, a_line(seq=7, kind="VISIT_START", currency=None, delta=None))

    assert len(db.ledger_page(conn)) == 1


def test_a_database_written_before_the_fix_gets_the_new_index(tmp_path: Path) -> None:
    """Existing databases carry the old seq-only index, and CREATE ... IF NOT
    EXISTS never replaces it. connect() has to drop it, or every account that
    already has a ledger keeps losing gems after upgrading."""
    path = tmp_path / "bot.db"
    old = db.connect(path)
    old.execute("DROP INDEX ledger_event_line_idx")
    old.execute(
        "CREATE UNIQUE INDEX ledger_seq_idx ON ledger(seq) WHERE seq IS NOT NULL"
    )
    old.commit()
    old.close()

    conn = db.connect(path)
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'ledger'"
        )
    }
    db.insert_ledger(conn, a_line(seq=501, currency="coins"))
    db.insert_ledger(conn, a_line(seq=501, currency="gems"))

    assert "ledger_seq_idx" not in indexes
    assert "ledger_event_line_idx" in indexes
    assert len(db.ledger_page(conn)) == 2


def test_a_page_never_ends_halfway_through_an_event(tmp_path: Path) -> None:
    """A page boundary between an event's two lines would show a mission's
    gems on one page and its coins on the next."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="coins"))  # id 1
    db.insert_ledger(conn, a_line(seq=2, currency="coins"))  # id 2
    db.insert_ledger(conn, a_line(seq=2, currency="gems"))   # id 3
    db.insert_ledger(conn, a_line(seq=3, currency="coins"))  # id 4

    first = db.ledger_page(conn, limit=2)
    second = db.ledger_page(conn, limit=2, before=first[-1]["id"])

    # The limit landed between seq 2's lines; the page finishes the event.
    assert [line["seq"] for line in first] == [3, 2, 2]
    # And resumes after it, repeating nothing.
    assert [line["seq"] for line in second] == [1]


def test_finishing_an_event_respects_the_page_filters(tmp_path: Path) -> None:
    """Filtered to coins, a claim's gem line must not be pulled back in."""
    conn = make_db(tmp_path)
    db.insert_ledger(conn, a_line(seq=1, currency="coins"))
    db.insert_ledger(conn, a_line(seq=2, currency="coins"))
    db.insert_ledger(conn, a_line(seq=2, currency="gems"))

    page = db.ledger_page(conn, limit=1, currency="coins")

    assert [line["currency"] for line in page] == ["coins"]


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


def _buy(conn: sqlite3.Connection, seq: int, run_id: int, upgrade_id: str | None, price: int | None) -> None:
    db.insert_event(conn, to_row(events.BattlePurchased(
        seq=seq, ts=100.0 + seq, item="x", upgrade_id=upgrade_id, price=price, value=None), run_id))


def _finish(conn: sqlite3.Connection, run_id: int, **overrides: object) -> None:
    fields: dict[str, object] = dict(started_at=0.0, ended_at=100.0 + run_id, wave=10, coins=50, tier=1,
                                     abandoned=False, scan_count=0, tap_count=0)
    fields.update(overrides)
    db.finish_run(conn, run_id, **fields)  # type: ignore[arg-type]


def test_connect_adds_killed_by_and_ad_coins_to_an_old_runs_table(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, "
                "wave INTEGER, coins INTEGER, tier INTEGER, abandoned INTEGER NOT NULL DEFAULT 0, "
                "scan_count INTEGER NOT NULL DEFAULT 0, tap_count INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO runs (id, started_at) VALUES (1, 5.0)")
    old.commit()
    old.close()
    conn = db.connect(path)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"killed_by", "ad_coins", "purpose"} <= columns
    assert db.list_runs(conn)[0]["killed_by"] is None


def test_finish_run_stores_killed_by_and_ad_coins(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, killed_by="Tank", ad_coins=40)
    run = db.list_runs(conn)[0]
    assert (run["killed_by"], run["ad_coins"]) == ("Tank", 40)


def test_finish_run_summarizes_purchases_once_per_upgrade(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=0.0)
    _buy(conn, 1, 1, "damage", 10)
    _buy(conn, 2, 1, "damage", None)
    _buy(conn, 3, 1, "health", 7)
    _buy(conn, 4, 1, None, 3)          # unattributable: skipped
    _finish(conn, 1)
    _finish(conn, 1)                   # repeated finish must not double count
    rows = {r["upgrade_id"]: dict(r) for r in conn.execute("SELECT * FROM run_upgrades WHERE run_id = 1")}
    assert set(rows) == {"damage", "health"}
    assert (rows["damage"]["levels"], rows["damage"]["spent"], rows["damage"]["unpriced"]) == (2, 10, 1)
    assert (rows["health"]["levels"], rows["health"]["spent"], rows["health"]["unpriced"]) == (1, 7, 0)


def test_connect_backfills_summaries_for_finished_runs_with_purchase_events(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    conn = db.connect(path)
    _finish(conn, 1)
    _buy(conn, 1, 1, "damage", 10)
    conn.execute("DELETE FROM run_upgrades")
    conn.commit()
    conn.close()
    conn = db.connect(path)
    assert conn.execute("SELECT levels FROM run_upgrades WHERE run_id = 1 AND upgrade_id = 'damage'").fetchone()[0] == 1


def _pre_migration_db(path: Path) -> None:
    """A worker DB as it looks before any writer `connect` has touched it:
    `purpose` exists (an older migration), but `killed_by`, `ad_coins` and
    `run_upgrades` do not - `db.reader` (mode=ro) never migrates, only
    `db.connect` does."""
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, "
        "wave INTEGER, coins INTEGER, tier INTEGER, abandoned INTEGER NOT NULL DEFAULT 0, "
        "scan_count INTEGER NOT NULL DEFAULT 0, tap_count INTEGER NOT NULL DEFAULT 0, "
        "purpose TEXT NOT NULL DEFAULT 'farm')"
    )
    old.execute(
        "INSERT INTO runs (id, started_at, ended_at, wave, coins, tier, abandoned) "
        "VALUES (1, 0.0, 60.0, 10, 50, 1, 0)"
    )
    old.commit()
    old.close()


def test_list_runs_and_run_upgrade_levels_tolerate_a_worker_db_opened_before_migration(tmp_path: Path) -> None:
    """The web layer reads worker DBs with `db.reader`, which never migrates.
    A worker whose writer has not connected since `run_upgrades`/`killed_by`/
    `ad_coins` were added must still answer, not raise OperationalError."""
    path = tmp_path / "old.db"
    _pre_migration_db(path)

    with db.reader(path) as conn:
        runs = db.list_runs(conn)
        levels = db.run_upgrade_levels(conn, 1)

    assert len(runs) == 1
    run = runs[0]
    assert (run["buys"], run["killed_by"], run["ad_coins"]) == (None, None, None)
    assert run["total_coins"] == 50
    assert levels is None


def _by_id(conn: sqlite3.Connection, limit: int = 50) -> dict[int, dict]:
    return {run["id"]: run for run in db.list_runs(conn, limit=limit)}


def test_records_are_chronological_and_keep_broken_badges(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, wave=10, coins=50)
    _finish(conn, 2, wave=10, coins=40)        # tie on wave: not a record
    _finish(conn, 3, wave=14, coins=90, ad_coins=10)
    runs = _by_id(conn)
    assert runs[1]["wave_record"] == "broken" and runs[1]["wave_broken_by"] == 3
    assert runs[1]["wave_prev"] is None
    assert runs[2]["wave_record"] is None and runs[2]["coin_record"] is None
    assert runs[3]["wave_record"] == "standing" and runs[3]["wave_prev"] == {"run_id": 1, "value": 10}
    assert runs[3]["total_coins"] == 100 and runs[3]["coin_record"] == "standing"


def test_wave_records_are_per_tier_and_coin_records_are_not(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, tier=1, wave=30, coins=100)
    _finish(conn, 2, tier=2, wave=12, coins=80)
    runs = _by_id(conn)
    assert runs[2]["wave_record"] == "standing"      # first T2 run
    assert runs[1]["wave_record"] == "standing"      # still best at T1
    assert runs[2]["coin_record"] is None            # 80 < 100 fleet-member-wide


def test_abandoned_open_and_unread_runs_never_hold_or_break_records(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, wave=10, coins=50)
    _finish(conn, 2, wave=99, coins=999, abandoned=True)
    _finish(conn, 3, wave=None, coins=None)
    db.start_run(conn, 4, started_at=500.0)          # live run, ended_at NULL
    conn.execute("UPDATE runs SET wave = 200, coins = 2000 WHERE id = 4")
    conn.commit()
    runs = _by_id(conn)
    assert runs[1]["wave_record"] == "standing" and runs[1]["coin_record"] == "standing"
    for run_id in (2, 3, 4):
        assert runs[run_id]["wave_record"] is None and runs[run_id]["coin_record"] is None
    assert runs[3]["total_coins"] is None


def test_records_use_full_history_beyond_the_page_limit(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    _finish(conn, 1, wave=10)
    _finish(conn, 2, wave=12)
    [latest] = db.list_runs(conn, limit=1)
    assert latest["id"] == 2 and latest["wave_prev"] == {"run_id": 1, "value": 10}


def test_buys_and_upgrade_levels(tmp_path: Path) -> None:
    conn = make_db(tmp_path)
    db.start_run(conn, 1, started_at=0.0)
    _buy(conn, 1, 1, "damage", 10)
    _buy(conn, 2, 1, "damage", 12)
    _finish(conn, 1)
    _finish(conn, 2)
    runs = _by_id(conn)
    assert runs[1]["buys"] == 2 and runs[2]["buys"] is None
    assert db.run_upgrade_levels(conn, 1) == [{"upgrade_id": "damage", "levels": 2, "spent": 22, "unpriced": 0}]
    assert db.run_upgrade_levels(conn, 2) is None
