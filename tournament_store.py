"""Durable account-scoped entry intents, independent of queued run analytics."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tournament_policy import PurchaseCursor

SCHEMA = """
CREATE TABLE IF NOT EXISTS tournament_backoff (account_key TEXT NOT NULL,event_key TEXT NOT NULL,reason TEXT NOT NULL,until_at REAL NOT NULL,PRIMARY KEY(account_key,event_key));
CREATE TABLE IF NOT EXISTS tournament_attempts (
 attempt_id TEXT PRIMARY KEY, account_key TEXT NOT NULL, event_key TEXT NOT NULL,
 stage TEXT NOT NULL, league TEXT, tickets_before INTEGER, run_id INTEGER,
 started_at REAL NOT NULL, entered_at REAL, finished_at REAL,
 opening_spent INTEGER NOT NULL DEFAULT 0, growth_index INTEGER NOT NULL DEFAULT 0, receipt_index INTEGER NOT NULL DEFAULT 0,
 result TEXT, lease_key TEXT, opening_closed INTEGER NOT NULL DEFAULT 0, UNIQUE(account_key,event_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS tournament_account_pending
 ON tournament_attempts(account_key) WHERE stage != 'completed';
"""


class DuplicateTournamentAttempt(ValueError):
    pass


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    account_key: str
    event_key: str
    stage: str
    league: str | None
    tickets_before: int | None
    run_id: int | None
    started_at: float
    entered_at: float | None
    finished_at: float | None
    opening_spent: int
    growth_index: int
    receipt_index: int
    result: str | None
    lease_key: str | None
    opening_closed: bool = False


class TournamentStore:
    def __init__(self, path: Path | str, account_key: str, lease_key: str | None = None) -> None:
        import db
        self.conn = db.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=FULL')
        self.conn.executescript(SCHEMA)
        if "receipt_index" not in {row[1] for row in self.conn.execute("PRAGMA table_info(tournament_attempts)")}:
            self.conn.execute("ALTER TABLE tournament_attempts ADD COLUMN receipt_index INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        if "opening_closed" not in {row[1] for row in self.conn.execute("PRAGMA table_info(tournament_attempts)")}:
            self.conn.execute("ALTER TABLE tournament_attempts ADD COLUMN opening_closed INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        self.account_key, self.lease_key = account_key, lease_key

    def close(self) -> None:
        self.conn.close()

    def pending(self) -> Attempt | None:
        row = self.conn.execute("SELECT * FROM tournament_attempts WHERE account_key=? AND stage!='completed'",
                                (self.account_key,)).fetchone()
        return Attempt(**dict(row)) if row else None

    def defer(self, event_key: str, reason: str, until_at: float) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO tournament_backoff VALUES(?,?,?,?) ON CONFLICT(account_key,event_key) DO UPDATE SET reason=excluded.reason,until_at=excluded.until_at",
                              (self.account_key,event_key,reason,until_at))

    def deferred(self, event_key: str, now: float) -> bool:
        return self.conn.execute("SELECT 1 FROM tournament_backoff WHERE account_key=? AND event_key=? AND until_at>?",
                                 (self.account_key,event_key,now)).fetchone() is not None

    def seen(self, event_key: str) -> bool:
        return self.conn.execute('SELECT 1 FROM tournament_attempts WHERE account_key=? AND event_key=?',
                                 (self.account_key,event_key)).fetchone() is not None

    def begin(self, event_key: str, league: str | None, tickets: int, *, now: float | None = None) -> Attempt:
        if type(tickets) is not int or tickets < 1:
            raise ValueError('entry requires a verified positive ticket count')
        try:
            with self.conn:
                self.conn.execute('INSERT INTO tournament_attempts '
                    '(attempt_id,account_key,event_key,stage,league,tickets_before,started_at,lease_key) '
                    'VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,self.account_key,event_key,
                    'entry_pending',league,tickets,time.time() if now is None else now,self.lease_key))
        except sqlite3.IntegrityError as exc:
            raise DuplicateTournamentAttempt('an entry for this event or an unresolved attempt already exists') from exc
        attempt = self.pending()
        assert attempt is not None
        return attempt

    def entered(self, run_id: int | None, *, now: float | None = None) -> bool:
        with self.conn:
            cursor = self.conn.execute("UPDATE tournament_attempts SET stage='playing',run_id=COALESCE(?,run_id),"
                "entered_at=COALESCE(entered_at,?) WHERE account_key=? AND stage='entry_pending'",
                (run_id,time.time() if now is None else now,self.account_key))
            if cursor.rowcount == 1:
                import db, ledger
                attempt = self.pending()
                line = ledger.LedgerLine(kind='TOURNAMENT_ENTRY',ts=time.time() if now is None else now,
                    delta=0,reason='free_ticket',run_id=run_id,
                    detail={'attempt_id':attempt.attempt_id,'tickets_consumed':1,'league':attempt.league})
                db.insert_ledger(self.conn,line.as_row(),commit=False)
        return cursor.rowcount == 1

    def bind_run(self, run_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE tournament_attempts SET run_id=? WHERE account_key=? AND stage='playing'",
                              (run_id,self.account_key))
            attempt = self.pending()
            if attempt is not None:
                self.conn.execute("INSERT OR IGNORE INTO runs(id,started_at,purpose,tournament,league) VALUES(?,?,'farm',1,?)",
                                  (run_id,attempt.entered_at or attempt.started_at,attempt.league))

    def close_opening(self) -> None:
        with self.conn:
            self.conn.execute("UPDATE tournament_attempts SET opening_closed=1 WHERE account_key=? AND stage='playing'", (self.account_key,))

    def save_cursor(self, cursor: PurchaseCursor) -> None:
        with self.conn:
            self.conn.execute("UPDATE tournament_attempts SET opening_spent=?,growth_index=?,receipt_index=? "
                              "WHERE account_key=? AND stage='playing'",
                              (cursor.opening_spent,cursor.growth_index,cursor.receipt_index,self.account_key))

    def result(self, *, wave: int | None, rank: int | None, coins: int | None,
               ad_coins: int | None, killed_by: str | None, now: float | None = None) -> bool:
        import db, events, ledger
        attempt = self.pending()
        payload = json.dumps(dict(wave=wave,rank=rank,coins=coins,ad_coins=ad_coins,killed_by=killed_by))
        with self.conn:
            cursor = self.conn.execute("UPDATE tournament_attempts SET stage='result',result=?,finished_at=? "
                                      "WHERE account_key=? AND stage IN ('playing','entry_pending') AND result IS NULL",
                                      (payload,time.time() if now is None else now,self.account_key))
            if cursor.rowcount == 1 and attempt is not None and attempt.run_id is not None:
                end = time.time() if now is None else now
                start = attempt.entered_at or attempt.started_at
                db.finish_run(self.conn, attempt.run_id, started_at=start, ended_at=end,
                    wave=wave, coins=coins, tier=None, abandoned=False, scan_count=0, tap_count=0,
                    ad_coins=ad_coins,killed_by=killed_by,tournament=True,league=attempt.league,rank=rank,commit=False)
                event = events.RunEnded(run_id=attempt.run_id,duration=max(0,end-start),ts=end,
                    tournament=True,league=attempt.league,rank=rank,wave=wave,coins=coins,
                    ad_coins=ad_coins,killed_by=killed_by)
                for line in ledger.LedgerWriter(self.conn).lines_for(event):
                    db.insert_ledger(self.conn,line.as_row(),commit=False)
        return cursor.rowcount == 1

    def complete(self, *, now: float | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE tournament_attempts SET stage='completed',finished_at=COALESCE(finished_at,?) "
                              "WHERE account_key=? AND stage='result'",(time.time() if now is None else now,self.account_key))

    def reconcile_exhausted(self, *, now: float | None = None) -> None:
        """Mark an unconfirmed consumed ticket without fabricating run/payout proof."""
        with self.conn:
            self.conn.execute("UPDATE tournament_attempts SET stage='result',finished_at=? "
                              "WHERE account_key=? AND stage='entry_pending'",
                              (time.time() if now is None else now,self.account_key))

    def snapshot(self) -> dict[str, Any]:
        pending = self.pending()
        return dict(pending.__dict__) if pending else {"stage":"idle"}
