from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest
import db


def test_attempt_intent_survives_restart_and_is_unique(tmp_path: Path) -> None:
    from tournament_store import TournamentStore, DuplicateTournamentAttempt
    path = tmp_path / "journal.sqlite"
    store = TournamentStore(path, "account")
    first = store.begin("utc:2026-10-10", "Copper", 1, now=100)
    store.close()
    recovered = TournamentStore(path, "account")
    assert recovered.pending().attempt_id == first.attempt_id
    with pytest.raises(DuplicateTournamentAttempt):
        recovered.begin("utc:2026-10-10", "Copper", 1, now=200)
    with pytest.raises(DuplicateTournamentAttempt):
        recovered.begin("utc:2026-10-14", "Copper", 1, now=300)
    recovered.close()


def test_result_is_idempotent_and_cursor_persisted(tmp_path: Path) -> None:
    from tournament_store import TournamentStore
    from tournament_policy import PurchaseCursor
    store = TournamentStore(tmp_path / "journal.sqlite", "account")
    attempt = store.begin("event", "Copper", 1, now=100)
    store.entered(7, now=110)
    store.save_cursor(PurchaseCursor(50, 2))
    assert store.pending().opening_spent == 50
    assert store.result(wave=8, rank=30, coins=103, ad_coins=0, killed_by="Basic", now=120)
    assert not store.result(wave=8, rank=30, coins=103, ad_coins=0, killed_by="Basic", now=121)
    store.complete(now=130)
    assert store.pending() is None
    assert store.seen("event")
    store.close()


def test_old_run_schema_migrates_and_tournaments_do_not_raise_farm_records(tmp_path: Path) -> None:
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as legacy:
        legacy.execute('CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL, wave INTEGER, coins INTEGER, tier INTEGER, abandoned INTEGER DEFAULT 0, scan_count INTEGER DEFAULT 0, tap_count INTEGER DEFAULT 0)')
        legacy.execute('INSERT INTO runs VALUES (9,0,10,20,2,1,0,0,0)')
    conn = db.connect(path)
    assert dict(conn.execute('SELECT * FROM runs WHERE id=9').fetchone())['tournament'] == 0
    db.finish_run(conn, 1, started_at=0, ended_at=10, wave=50, coins=10, tier=1,
                  abandoned=False, scan_count=0, tap_count=0)
    db.finish_run(conn, 2, started_at=20, ended_at=30, wave=999, coins=999, tier=1,
                  abandoned=False, scan_count=0, tap_count=0, tournament=True, league="Copper", rank=4)
    assert db.best_wave(conn) == 50
    assert db.tier_best_waves(conn) == {1: 50}
    row = next(row for row in db.list_runs(conn) if row["id"] == 2)
    assert row["tournament"] and row["league"] == "Copper" and row["tier"] is None
    assert not row["wave_record"]
    assert db.stats_progress(conn)['summary']['total_runs'] == 2
    conn.close()
