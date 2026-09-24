"""SQLite persistence for the event stream.

Pure database mechanics: schema, writes, queries. It knows rows, not events -
turning an event into a row is `sinks/store.py`'s job. Keeping the two apart
lets the web layer read the database without importing a sink, and lets the
sink be tested without a web server.

Concurrency: WAL, short serialized writes (store sink and account repository), many readers
(the web layer). Readers open their own short-lived connection, because a
sqlite3 connection belongs to the thread that created it and FastAPI runs sync
routes on a threadpool.

Typed columns for what is filtered and aggregated, a JSON `detail` blob for
the long tail - so adding a field to an event needs no migration.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_identity (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    account_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, revision_id INTEGER NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_observations (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, observed_at REAL NOT NULL, detail TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    started_at REAL    NOT NULL,
    ended_at   REAL,
    wave       INTEGER,
    coins      INTEGER,
    tier       INTEGER,
    abandoned  INTEGER NOT NULL DEFAULT 0,
    scan_count INTEGER NOT NULL DEFAULT 0,
    tap_count  INTEGER NOT NULL DEFAULT 0,
    purpose    TEXT NOT NULL DEFAULT 'farm' CHECK(purpose IN ('farm', 'milestone'))
);

CREATE TABLE IF NOT EXISTS events (
    seq    INTEGER PRIMARY KEY,
    run_id INTEGER,
    ts     REAL NOT NULL,
    type   TEXT NOT NULL,
    screen TEXT,
    action TEXT,
    reason TEXT,
    score  REAL,
    price  INTEGER,
    wallet INTEGER,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS events_run_idx ON events(run_id, seq);
CREATE INDEX IF NOT EXISTS events_ts_idx  ON events(ts);

-- Never pruned, unlike `events`. This is the account's permanent history:
-- what it spent, on what, and what the balances were. `events` answers
-- "what was the bot doing" for 30 days; this answers "what happened to
-- this account" forever.
CREATE TABLE IF NOT EXISTS ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,
    item          TEXT,
    category      TEXT,
    currency      TEXT,
    delta         INTEGER,
    price         INTEGER,
    balance_after INTEGER,
    observed      INTEGER,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    run_id        INTEGER,
    visit         INTEGER,
    reason        TEXT,
    detail        TEXT
);

-- One line per currency per source event. It stops an event redelivered
-- across a restart from being counted twice.
--
-- Keyed on the currency as well as the seq, because one event can move
-- several: a mission claim pays coins AND gems, and classify() returns one
-- line for each, both carrying the event's seq. Keyed on seq alone - as it
-- was - INSERT OR IGNORE silently discarded the second line, so every
-- mission claim's gems vanished from the history. connect() drops that old
-- index from existing databases.
--
-- IFNULL, because SQLite treats NULLs as distinct inside a unique index: on
-- plain (seq, currency) a redelivered visit boundary or policy change, which
-- have no currency, would stop being deduplicated and double up.
--
-- Partial, because derived UNEXPLAINED lines have no source event and so no
-- seq. It deliberately cannot dedupe them, which is why ledger.backfill()
-- guards on the table being empty instead.
CREATE UNIQUE INDEX IF NOT EXISTS ledger_event_line_idx
    ON ledger(seq, IFNULL(currency, '')) WHERE seq IS NOT NULL;
CREATE INDEX IF NOT EXISTS ledger_ts_idx   ON ledger(ts);
CREATE INDEX IF NOT EXISTS ledger_kind_idx ON ledger(kind);

-- Written BEFORE the device action it describes, which is the whole point:
-- a row here is the only thing that outlives a process that dies between
-- the tap and its confirmation. `ledger` records what provably happened;
-- this records what was attempted, so a restart can tell the difference
-- between "never tapped" and "tapped, outcome unknown".
CREATE TABLE IF NOT EXISTS transactions (
    key           TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    stage         TEXT NOT NULL,
    item          TEXT NOT NULL,
    category      TEXT,
    currency      TEXT,
    price         INTEGER,
    wallet_before INTEGER,
    evidence      TEXT,
    acted_at      REAL,
    resolved_at   REAL,
    outcome       TEXT,
    spent         INTEGER,
    detail        TEXT
);

CREATE INDEX IF NOT EXISTS transactions_stage_idx ON transactions(stage);
CREATE INDEX IF NOT EXISTS transactions_ts_idx    ON transactions(ts);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the writable connection, creating the file and schema if needed."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    # WAL is what lets the web layer read while the sink writes.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    # The index this replaced keyed a ledger line on seq alone, which let only
    # the first line of a multi-currency event in. Idempotent: it exists on a
    # database written before the fix and on nothing created since.
    conn.execute("DROP INDEX IF EXISTS ledger_seq_idx")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "purpose" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN purpose TEXT NOT NULL DEFAULT 'farm' "
            "CHECK(purpose IN ('farm', 'milestone'))"
        )
    conn.commit()
    return conn


@contextmanager
def reader(path: Path | str) -> Iterator[sqlite3.Connection]:
    """A short-lived read-only connection, closed on exit.

    Read-only at the driver level rather than by convention: the web layer is
    the one component that should never be able to write, and a URI opened
    with mode=ro cannot, whatever a route does with it.
    """
    uri = f"{Path(path).absolute().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def connection_account(conn: sqlite3.Connection) -> str | None:
    """Read identity from the same connection used for account history."""
    table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='account_identity'").fetchone()
    if table is None:
        return None  # History recorded before account identity tracking.
    row = conn.execute("SELECT account_id FROM account_identity WHERE id = 1").fetchone()
    return row[0] if row is not None else None


def bound_account(path: Path | str) -> str | None:
    """Return the account explicitly assigned to a worker database."""
    try:
        with reader(path) as conn:
            return connection_account(conn)
    except sqlite3.OperationalError:
        return None  # An older database has no identity marker.


def bind_account(path: Path | str, account_id: str) -> None:
    """Assign an empty worker database once; reject reused or mixed history."""
    if not account_id:
        raise ValueError("worker account identity is required")
    conn = connect(path)
    try:
        with conn:
            row = conn.execute("SELECT account_id FROM account_identity WHERE id = 1").fetchone()
            if row is not None:
                if row[0] != account_id:
                    raise ValueError("worker database is bound to another account")
                return
            for table in ("runs", "events", "ledger", "transactions",
                          "account_revisions", "account_observations", "run_observations"):
                if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
                    raise ValueError("worker database has unattributed history")
            conn.execute("INSERT INTO account_identity(id, account_id) VALUES (1, ?)",
                         (account_id,))
    finally:
        conn.close()


def max_seq(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0])


def max_run_id(conn: sqlite3.Connection) -> int:
    return int(conn.execute("""SELECT MAX(
        (SELECT COALESCE(MAX(id), 0) FROM runs),
        (SELECT COALESCE(MAX(run_id), 0) FROM run_observations)
    )""").fetchone()[0])


def best_wave(conn: sqlite3.Connection) -> int | None:
    """The best wave ever reached, or None if no run has reported one.

    NULL, never 0, for an empty table or a table with only NULL waves -
    an unread best wave is not the same as a confirmed wave of zero.
    """
    row = conn.execute("SELECT MAX(wave) FROM runs WHERE wave IS NOT NULL").fetchone()
    value = row[0]
    return int(value) if value is not None else None


def insert_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO events
               (seq, run_id, ts, type, screen, action, reason, score, price,
                wallet, detail)
           VALUES (:seq, :run_id, :ts, :type, :screen, :action, :reason,
                   :score, :price, :wallet, :detail)""",
        row,
    )
    conn.commit()


def start_run(
    conn: sqlite3.Connection, run_id: int, started_at: float, *, purpose: str = "farm"
) -> None:
    conn.execute(
        """INSERT INTO runs (id, started_at, purpose) VALUES (?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET started_at = excluded.started_at,
                                        purpose = excluded.purpose""",
        (run_id, started_at, purpose),
    )
    conn.commit()


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    started_at: float,
    ended_at: float,
    wave: int | None,
    coins: int | None,
    tier: int | None,
    abandoned: bool,
    scan_count: int,
    tap_count: int,
) -> None:
    """Close a run out.

    An upsert rather than an update: if the process died mid-run and the
    opening row never landed, the run is still worth recording. `started_at`
    is only used in that case - an existing row keeps the start it was opened
    with, which is the real one.
    """
    conn.execute(
        """INSERT INTO runs (id, started_at, ended_at, wave, coins, tier,
                             abandoned, scan_count, tap_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               ended_at   = excluded.ended_at,
               wave       = excluded.wave,
               coins      = excluded.coins,
               tier       = excluded.tier,
               abandoned  = excluded.abandoned,
               scan_count = excluded.scan_count,
               tap_count  = excluded.tap_count""",
        (
            run_id, started_at, ended_at, wave, coins, tier,
            int(abandoned), scan_count, tap_count,
        ),
    )
    conn.commit()


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def run_events(
    conn: sqlite3.Connection, run_id: int, limit: int = 2000
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM events WHERE run_id = ? ORDER BY seq LIMIT ?",
        (run_id, limit),
    ).fetchall()
    return [_decode(row) for row in rows]


def run_purchases(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    """The in-run upgrades bought during one run, in the order they happened.

    Reads the events table rather than a table of its own: `BattlePurchased`
    is already written there with the run's id, and the row for a live run
    lands as the purchase happens - so an open run and a finished one are the
    same query, not two code paths.

    The detail blob is flattened onto the row because `item`, `upgrade_id`
    and `value` are only in it by accident of the schema (store.to_row keeps
    columns for the six typed fields and JSON for the rest); a caller wants
    one flat purchase, not a column half and a blob half. `price` keeps None
    when OCR could not read it - that is not a free upgrade.
    """
    rows = conn.execute(
        "SELECT seq, ts, price, detail FROM events "
        "WHERE run_id = ? AND type = 'BattlePurchased' ORDER BY seq",
        (run_id,),
    ).fetchall()
    purchases = []
    for row in rows:
        data = _decode(row)
        detail = data.pop("detail")
        purchases.append({
            "seq": data["seq"],
            "ts": data["ts"],
            "price": data["price"],
            "item": detail.get("item"),
            "upgrade_id": detail.get("upgrade_id"),
            "value": detail.get("value"),
        })
    return purchases


def close_abandoned_runs(conn: sqlite3.Connection) -> int:
    """Close out runs a killed process never finished. Returns how many.

    `RunEnded` never fires for a kill (only for the tracked run/game-over
    lifecycle), so the row keeps `ended_at IS NULL` - which is exactly what
    the dashboard reads as "live" - forever, and `runs` is never pruned by
    age the way `events` is. `started_at` stands in for `ended_at` rather
    than "now": no real duration was ever observed, and backdating to the
    run's own start is honest about that instead of inventing one.
    """
    cursor = conn.execute(
        "UPDATE runs SET ended_at = started_at, abandoned = 1 "
        "WHERE ended_at IS NULL"
    )
    conn.commit()
    return cursor.rowcount


def prune_events(
    conn: sqlite3.Connection, retention_days: int, now: float | None = None
) -> int:
    """Delete events older than the retention window. Returns how many went."""
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    cursor = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.commit()
    return cursor.rowcount


def run_stats(conn: sqlite3.Connection, limit: int = 200) -> list[dict[str, Any]]:
    """Finished runs, oldest first, with their duration precomputed.

    Oldest first because every consumer is a time series; reversing a DESC
    result in the browser is work the database already knows how to avoid.
    """
    rows = conn.execute(
        """SELECT id, started_at, ended_at, wave, coins, tier, tap_count,
                  scan_count, ended_at - started_at AS duration
             FROM runs
            WHERE ended_at IS NOT NULL
            ORDER BY id DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(row) for row in reversed(rows)]


def workshop_purchase_summary(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Full-history counts of confirmed Workshop buys, grouped for comparison."""
    rows = conn.execute(
        """SELECT COALESCE(category, 'OTHER') AS category,
                  COALESCE(item, 'Unknown upgrade') AS item,
                  COUNT(*) AS count
             FROM ledger
            WHERE kind = 'WORKSHOP_BUY' AND dry_run = 0
              AND CASE WHEN json_valid(detail)
                       THEN json_extract(detail, '$.verdict') END IN ('bought', 'free')
            GROUP BY COALESCE(category, 'OTHER'), COALESCE(item, 'Unknown upgrade')
            ORDER BY category, item"""
    ).fetchall()
    return [dict(row) for row in rows]


def stats_progress(conn: sqlite3.Connection) -> dict[str, Any]:
    """Full-history Tier 1 benchmarks, confirmed at the end of a run.

    Recorded play counts completed runs on every tier. It excludes gaps
    between runs; elapsed time includes them. Neither claims the precise
    moment within a run at which a wave was crossed.
    """
    benchmarks = [dict(tier=1, wave=wave, run_id=None, reached_at=None,
                       play_seconds=None, elapsed_seconds=None)
                  for wave in (20, 30, 60, 100)]
    total = 0
    played = 0.0
    first_started: float | None = None
    best: int | None = None
    for row in conn.execute(
            "SELECT id, started_at, ended_at, tier, wave FROM runs "
            "WHERE ended_at IS NOT NULL ORDER BY started_at, id"):
        total += 1
        if first_started is None:
            first_started = row["started_at"]
        played += max(0.0, row["ended_at"] - row["started_at"])
        if row["tier"] != 1 or row["wave"] is None:
            continue
        best = row["wave"] if best is None else max(best, row["wave"])
        for milestone in benchmarks:
            if milestone["run_id"] is None and row["wave"] >= milestone["wave"]:
                milestone.update(run_id=row["id"], reached_at=row["ended_at"],
                                 play_seconds=played,
                                 elapsed_seconds=max(0.0, row["ended_at"] - first_started))
    return {"summary": {"total_runs": total, "best_tier_1_wave": best,
                        "play_seconds": played}, "benchmarks": benchmarks}


def taps_by_action(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Which upgrades actually get bought."""
    rows = conn.execute(
        """SELECT action, COUNT(*) AS count
             FROM events
            WHERE type = 'Tapped' AND action IS NOT NULL
            GROUP BY action
            ORDER BY count DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def screen_histogram(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Where the bot spends its scans.

    ScanCompleted is never stored (the run row carries scan_count instead), so
    this counts the screens on every event that names one - which is the same
    question and the only one this table can answer.
    """
    rows = conn.execute(
        """SELECT screen, COUNT(*) AS count
             FROM events
            WHERE screen IS NOT NULL
            GROUP BY screen
            ORDER BY count DESC"""
    ).fetchall()
    return [dict(row) for row in rows]


def error_log(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """BotError rows, newest first, with their tracebacks decoded."""
    rows = conn.execute(
        """SELECT * FROM events
            WHERE type = 'BotError'
            ORDER BY seq DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_decode(row) for row in rows]


def insert_ledger(conn: sqlite3.Connection, row: dict[str, Any], *, commit: bool = True) -> None:
    """Append one ledger line.

    OR IGNORE, not OR REPLACE: a line already written for this seq is the
    same line, and replacing it would rewrite a balance_after that later
    lines were already computed against.
    """
    conn.execute(
        """INSERT OR IGNORE INTO ledger
               (seq, ts, kind, item, category, currency, delta, price,
                balance_after, observed, dry_run, run_id, visit, reason,
                detail)
           VALUES (:seq, :ts, :kind, :item, :category, :currency, :delta,
                   :price, :balance_after, :observed, :dry_run, :run_id,
                   :visit, :reason, :detail)""",
        row,
    )
    if commit:
        conn.commit()


def last_balances(
    conn: sqlite3.Connection, currencies: Iterable[str] = ("coins", "gems"),
) -> dict[str, int | None]:
    """The most recent known balance for each currency, or None.

    Skips lines whose balance_after is NULL. That is a hole left by an
    unreadable price, not a balance of zero, and seeding from it would
    invent an enormous UNEXPLAINED line on the next real reading.

    `currencies` defaults to the two the writer balances, which is what
    LedgerWriter seeds itself from and must keep seeing. The ledger route
    passes every currency the history holds, so the page can show a stones
    balance the day one exists instead of assuming there never will be.
    """
    balances: dict[str, int | None] = {currency: None for currency in currencies}
    for currency in balances:
        row = conn.execute(
            """SELECT balance_after FROM ledger
                WHERE currency = ? AND balance_after IS NOT NULL
                ORDER BY id DESC LIMIT 1""",
            (currency,),
        ).fetchone()
        if row is not None:
            balances[currency] = int(row[0])
    return balances


def ledger_currencies(conn: sqlite3.Connection) -> list[str]:
    """Every currency any ledger line has moved, sorted.

    What the page offers as filters. Read from the history rather than from
    currencies.CURRENCIES: a filter for a currency this account has never
    touched is a control that can only ever return nothing, and a currency
    the history does hold must never be unreachable because a list somewhere
    forgot it - which is exactly how stones ended up stored and unfilterable.
    """
    rows = conn.execute(
        "SELECT DISTINCT currency FROM ledger WHERE currency IS NOT NULL ORDER BY currency"
    ).fetchall()
    return [str(row[0]) for row in rows]


def count_rehearsals(conn: sqlite3.Connection) -> int:
    """How many dry-run lines the ledger holds, for the page's toggle."""
    return int(conn.execute("SELECT COUNT(*) FROM ledger WHERE dry_run = 1").fetchone()[0])


def ledger_page(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    before: int | None = None,
    kind: str | None = None,
    currency: str | None = None,
    include_rehearsals: bool = False,
) -> list[dict[str, Any]]:
    """One page of ledger lines, newest first.

    Ordered and paged by `id`, not `ts`: a derived UNEXPLAINED line shares
    the ts of the event that revealed it, so ts alone cannot order the two,
    and the insertion order is the true one.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if before is not None:
        clauses.append("id < ?")
        params.append(before)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if currency is not None:
        clauses.append("currency = ?")
        params.append(currency)
    if not include_rehearsals:
        clauses.append("dry_run = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    rows = conn.execute(
        f"SELECT * FROM ledger {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    lines = [_decode(row) for row in rows]

    # Never end a page halfway through one event. A mission claim is two lines,
    # one per currency, and a boundary between them shows the gems on this
    # page and the coins on the next - a reward rendered as two half-rewards
    # until the reader happens to press "Load more". So a full page whose last
    # line belongs to an event pulls in that event's remaining lines, under the
    # same filters. The cursor is still the last line returned, so the next
    # page resumes after the whole event and never repeats one.
    if len(lines) == limit and lines[-1]["seq"] is not None:
        tail = conn.execute(
            f"SELECT * FROM ledger {where} {'AND' if clauses else 'WHERE'} "
            "seq = ? AND id < ? ORDER BY id DESC",
            [*params[:-1], lines[-1]["seq"], lines[-1]["id"]],
        ).fetchall()
        lines.extend(_decode(row) for row in tail)
    return lines


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    raw = data.get("detail")
    data["detail"] = json.loads(raw) if raw else {}
    return data
