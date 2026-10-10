"""Persist state-changing events to SQLite.

The consumer thread this sink inherits from QueueSink is the database's only
telemetry writer. WAL also permits the acknowledged account repository to write
short transactions while the web layer reads the same file concurrently. The
connection is opened on that thread rather than in start(), because a sqlite3
connection belongs to the thread that created it.

Two events are deliberately NOT stored:

* `ScanCompleted`. It fires every 2s - roughly 43,000 near-identical rows a
  day. It answers "is it alive right now", which is a question for the TUI and
  the live feed, not for history. The run row carries `scan_count` instead.
* `ScreenChanged` where prev == curr. Booting onto an unmodelled screen emits
  exactly one of these (the tracker's UNKNOWN placeholder confirming itself).
  It is a state change that changed no state, and the events table is meant to
  hold only rows that mean something.

`detail` names two different things depending on the row. For most events
it is the JSON blob of whatever `to_row` did not have a typed column for -
the long tail. `Skipped`, though, already has its own field called `detail`
(the human-readable reason, e.g. "screen is GAME_OVER"), and nothing pops it
out to a column before the rest gets blobbed - so it lands *inside* the blob
under its own name too. A `Skipped` row's `detail` column therefore decodes
to `{"detail": "..."}`, not a bare string, and `/api/runs/{id}/events`
passes that double-wrap straight through. The dashboard already knows to
unwrap it; the column is not renamed here to avoid a migration over a
collision that only one event type has.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

import config
import db
import events
import ledger
from sinks.base import QueueSink

logger = logging.getLogger("tower_bot.sinks.store")

# The columns the schema keeps typed. Everything else on an event goes to the
# JSON `detail` blob, so adding a field to an event needs no migration.
_TYPED = ("screen", "action", "reason", "score", "price", "wallet")


def to_row(event: events.Event, run_id: int | None) -> dict[str, Any]:
    """Flatten an event into a row for the events table."""
    data = dataclasses.asdict(event)
    seq = data.pop("seq")
    ts = data.pop("ts")
    # RunStarted / RunEnded name their own run; that belongs in the column,
    # not repeated inside the blob.
    own_run = data.pop("run_id", None)
    if "curr" in data:  # ScreenChanged: `curr` is the screen
        data["screen"] = data.pop("curr")

    row: dict[str, Any] = {
        "seq": seq,
        "ts": ts,
        "type": event.type,
        "run_id": own_run if own_run is not None else run_id,
    }
    for column in _TYPED:
        row[column] = data.pop(column, None)
    # default=str is a guard, not a plan: everything published today is
    # JSON-native, and a future field that is not degrades to a string here
    # rather than killing the writer thread.
    row["detail"] = json.dumps(data, default=str) if data else None
    return row


class StoreSink(QueueSink):
    def __init__(
        self, path: Path | str = config.DB_PATH, maxsize: int = 1000
    ) -> None:
        super().__init__(maxsize=maxsize)
        self.path = Path(path)
        self._conn: sqlite3.Connection | None = None
        self._ledger: ledger.LedgerWriter | None = None
        self._run_id: int | None = None
        self._scans = 0
        self._taps = 0

    def _consume(self) -> None:
        self._conn = db.connect(self.path)
        # After connect, because the writer seeds its running balances from
        # the ledger table - a restart that started from zero would read the
        # next real balance as an enormous unexplained gain.
        self._ledger = ledger.LedgerWriter(self._conn)
        try:
            super()._consume()
        finally:
            self._conn.close()
            self._conn = None
            self._ledger = None

    def handle(self, event: events.Event) -> None:
        conn = self._conn
        if conn is None:  # handle() called outside the consumer thread
            return

        match event:
            case events.ScanCompleted():
                self._scans += 1
                return
            case events.ScreenChanged() if event.prev == event.curr:
                return
            case events.RunStarted():
                self._run_id = event.run_id
                self._scans = 0
                self._taps = 0
                db.start_run(conn, event.run_id, event.ts, purpose=event.purpose, tournament=event.tournament, league=event.league)
            case events.Tapped():
                self._taps += 1
            case events.RunEnded():
                db.finish_run(
                    conn,
                    event.run_id,
                    started_at=event.ts - event.duration,
                    ended_at=event.ts,
                    wave=event.wave,
                    coins=event.coins,
                    tier=event.tier,
                    abandoned=event.abandoned,
                    killed_by=event.killed_by,
                    ad_coins=event.ad_coins,
                    scan_count=self._scans,
                    tap_count=self._taps,
                    tournament=event.tournament, league=event.league, rank=event.rank,
                )

        db.insert_event(conn, to_row(event, self._run_id))

        # Guarded separately from the event write above. QueueSink._consume
        # already catches everything, so a raise here could not kill the
        # thread - but it would skip the _run_id cleanup below, and every
        # orphaned event after a run ended would then be filed under the run
        # that already finished. The event history is the more important of
        # the two tables; the ledger must never cost it.
        if self._ledger is not None:
            try:
                for line in self._ledger.lines_for(event):
                    db.insert_ledger(conn, line.as_row())
            except Exception:  # noqa: BLE001
                logger.exception("could not write the ledger line for %s", event.type)

        if isinstance(event, events.RunEnded):
            # Orphaned events after a run ends belong to no run, not to the ended run.
            self._run_id = None
