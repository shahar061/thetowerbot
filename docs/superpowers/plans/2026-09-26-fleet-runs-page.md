# Fleet Runs Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist Killed By and ad coins for every run, keep a durable per-run summary of the in-run upgrades bought, compute chronological high-score records, and show all of it on a new Reroll fleet **Runs** page with a per-run side panel.

**Architecture:** The bot reads Killed By and ad coins from the game-over popup with the existing passive `game_over.parse_frame` OCR parser. It puts them on `RunEnded`, and the store sink writes them to two new `runs` columns. `db.finish_run` also aggregates that run's `BattlePurchased` events into a new never-pruned `run_upgrades` table. `db.list_runs` returns record flags computed over the worker's full history. The UI fetches each reroll member's `/api/runs` and `/api/runs/{id}/upgrades` through the existing `workerRead` scope header, then merges the rows client-side.

**Tech Stack:** Python 3 / SQLite (`db.py`), FastAPI (`web/app.py`), RapidOCR (`ocr.py`), Next.js App Router + Tailwind + lucide-react (`web/ui`), pytest, vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-09-26-fleet-runs-page-design.md`
**Approved mock:** https://claude.ai/artifact/XyaEwd9BRa3MDYiqjFhVwZ

## Global Constraints

- Work in the worktree `/Users/shahar/BifrostProjects/thetowerbot-fleet-runs` (branch `feat/fleet-runs-page`). Never check out, restore or edit files in the main checkout `/Users/shahar/BifrostProjects/thetowerbot`; other sessions edit it.
- Python tests run from the worktree root with `../thetowerbot/.venv/bin/pytest <file> -q -p no:allure_pytest`. Run **only** the files named in the step, never `tests/` as a whole.
- Frontend: run `npm ci` once in `web/ui` of the worktree before the first vitest step. Then use `npx vitest run <path>`.
- Record scope is per worker DB (one emulator and one account). Wave records are per tier; coin records are not.
- A record strictly beats every earlier **finished, non-abandoned** run in its scope. A tie is not a record. The first qualifying run is a record. Only the latest record is `"standing"`; earlier ones are `"broken"`.
- Coins: `runs.coins` stays "coins earned". Total = `coins + COALESCE(ad_coins, 0)`, and it is NULL when `coins` is NULL.
- An unreadable or absent OCR field is stored as NULL, never guessed. A game-over OCR failure must never block or alter run end.
- Upgrade "levels" = the number of bot buys in the run. Copy says "levels bought".
- Max levels come from `workshop_levels.ladders()[id].max_level`. Categories and names come from `upgrades.CATALOG`, skipping entries with `unlock=True`.
- Scrollbars are hidden on the runs table and the side panel, and scrolling still works.
- Commit after each task with the repo's attribution trailer lines:
  ```
  Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JLwj7QzEp4pFsoQAJDm1gM
  ```

## Review Focus

1. **Open run.** A run with `ended_at IS NULL` (the live battle) must never get a badge, never count toward Killed-by, and must read "In progress". It is pinned in Task 2 (`list_runs`) and Task 6 (`killedLabel`, `killedBy`).
2. **Unread wave or coins.** A finished run with `wave` or `coins` NULL (glyph read failed) must not become or break a record. Pinned in Task 2.
3. **OCR on an unsupported frame.** `parse_frame` returns None on non-1080×2400 geometry, and `ocr.read` can raise. Run end must still publish with `killed_by=None`. Pinned in Task 4.
4. **Purchase rows without `upgrade_id`.** These must not crash the summary and must not create a NULL-keyed `run_upgrades` row. Pinned in Task 1.
5. **One worker unreachable, or its account unverified.** Other emulators still render, and that emulator shows an inline error. Pinned in Task 7.

---

### Task 1: Store Killed By, ad coins and a per-run upgrade summary

**Files:**
- Modify: `db.py` (SCHEMA string near `runs` at :35-46; `connect` migration at :145-157; `finish_run` at :262-299; add helpers after `run_purchases` ~:350)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.finish_run(conn, run_id, *, started_at, ended_at, wave, coins, tier, abandoned, scan_count, tap_count, killed_by: str | None = None, ad_coins: int | None = None) -> None`, which also writes `run_upgrades`.
  - Table `run_upgrades(run_id, upgrade_id, levels, spent, unpriced)`.
  - `runs.killed_by TEXT`, `runs.ad_coins INTEGER`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_db.py`; add `import events` and `from sinks.store import to_row` to the imports)

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_db.py -q -p no:allure_pytest -k "killed_by or summarizes or backfills"`
Expected: FAIL (`TypeError: finish_run() got an unexpected keyword argument 'killed_by'` / `no such table: run_upgrades`).

- [ ] **Step 3: Implement**

In `SCHEMA`, right after the `runs` CREATE TABLE statement, add:

```sql
-- Never pruned, unlike `events`: what each finished run bought, one row per
-- upgrade. Written by finish_run from that run's BattlePurchased events.
CREATE TABLE IF NOT EXISTS run_upgrades (
    run_id     INTEGER NOT NULL,
    upgrade_id TEXT    NOT NULL,
    levels     INTEGER NOT NULL,
    spent      INTEGER NOT NULL,
    unpriced   INTEGER NOT NULL,
    PRIMARY KEY (run_id, upgrade_id)
);
```

In `connect`, replace the block after `columns = {...}` so it reads:

```python
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "purpose" not in columns:
        conn.execute(
            "ALTER TABLE runs ADD COLUMN purpose TEXT NOT NULL DEFAULT 'farm' "
            "CHECK(purpose IN ('farm', 'milestone'))"
        )
    if "killed_by" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN killed_by TEXT")
    if "ad_coins" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN ad_coins INTEGER")
    _backfill_run_upgrades(conn)
    conn.commit()
    return conn
```

Replace `finish_run` with:

```python
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
    killed_by: str | None = None,
    ad_coins: int | None = None,
) -> None:
    """(keep the existing docstring) Also freezes the run's purchases into
    `run_upgrades`, because `events` is pruned and `runs` is not."""
    conn.execute(
        """INSERT INTO runs (id, started_at, ended_at, wave, coins, tier,
                             abandoned, scan_count, tap_count, killed_by, ad_coins)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               ended_at = excluded.ended_at, wave = excluded.wave,
               coins = excluded.coins, tier = excluded.tier,
               abandoned = excluded.abandoned,
               scan_count = excluded.scan_count, tap_count = excluded.tap_count,
               killed_by = excluded.killed_by, ad_coins = excluded.ad_coins""",
        (run_id, started_at, ended_at, wave, coins, tier, int(abandoned),
         scan_count, tap_count, killed_by, ad_coins),
    )
    _summarize_purchases(conn, run_id)
    conn.commit()
```

After `run_purchases`, add:

```python
def _summarize_purchases(conn: sqlite3.Connection, run_id: int) -> None:
    """Rewrite one run's `run_upgrades` rows from its BattlePurchased events.

    Delete-then-insert keeps a repeated finish idempotent. A purchase with no
    upgrade_id cannot be attributed to an upgrade, so it is left out rather
    than stored under a NULL key.
    """
    totals: dict[str, list[int]] = {}
    for row in run_purchases(conn, run_id):
        if not row["upgrade_id"]:
            continue
        entry = totals.setdefault(row["upgrade_id"], [0, 0, 0])
        entry[0] += 1
        if row["price"] is None:
            entry[2] += 1
        else:
            entry[1] += row["price"]
    conn.execute("DELETE FROM run_upgrades WHERE run_id = ?", (run_id,))
    conn.executemany(
        "INSERT INTO run_upgrades (run_id, upgrade_id, levels, spent, unpriced) VALUES (?, ?, ?, ?, ?)",
        [(run_id, upgrade_id, *values) for upgrade_id, values in totals.items()],
    )


def _backfill_run_upgrades(conn: sqlite3.Connection) -> None:
    """Summarize finished runs that predate `run_upgrades` while their events survive."""
    pending = conn.execute(
        "SELECT r.id FROM runs r WHERE r.ended_at IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM run_upgrades u WHERE u.run_id = r.id) "
        "AND EXISTS (SELECT 1 FROM events e WHERE e.run_id = r.id AND e.type = 'BattlePurchased')"
    ).fetchall()
    for row in pending:
        _summarize_purchases(conn, row["id"])
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_db.py -q -p no:allure_pytest`
Expected: all pass. If any test in that file fails, run the same file on `origin/main` in a scratch worktree to show it failed before this change; otherwise fix it.

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "Store killed_by, ad_coins and a durable per-run upgrade summary" -m "<trailer lines>"
```

---

### Task 2: Chronological records and buys on `list_runs`; `run_upgrade_levels`

**Files:**
- Modify: `db.py` (`list_runs` at :302-306; add `_records`, `_NO_RECORD`, `run_upgrade_levels`)
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: the Task 1 columns and table.
- Produces:
  - `db.list_runs(conn, limit=50) -> list[dict]`. Each row = the `runs.*` columns plus `buys: int | None`, `total_coins: int | None`, `wave_record` / `coin_record: "standing" | "broken" | None`, `wave_prev` / `coin_prev: {"run_id": int, "value": int} | None`, `wave_broken_by` / `coin_broken_by: int | None`.
  - `db.run_upgrade_levels(conn, run_id) -> list[dict] | None`. Rows are `{upgrade_id, levels, spent, unpriced}`; the result is None when the run has no rows.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_db.py`; reuses `_finish` and `_buy` from Task 1)

```python
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_db.py -q -p no:allure_pytest -k "records or buys_and_upgrade"`
Expected: FAIL with `KeyError: 'wave_record'` / `AttributeError: ... run_upgrade_levels`.

- [ ] **Step 3: Implement** (replace `list_runs`; add the rest next to it)

```python
_NO_RECORD: dict[str, Any] = {
    "wave_record": None, "wave_prev": None, "wave_broken_by": None,
    "coin_record": None, "coin_prev": None, "coin_broken_by": None,
}


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT runs.*, (SELECT SUM(levels) FROM run_upgrades u WHERE u.run_id = runs.id) AS buys "
        "FROM runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    records = _records(conn)
    listed = []
    for row in rows:
        run = dict(row)
        run["total_coins"] = None if run["coins"] is None else run["coins"] + (run["ad_coins"] or 0)
        run.update(records.get(run["id"], _NO_RECORD))
        listed.append(run)
    return listed


def _records(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    """High scores in the order they were set, over this account's whole history.

    A run is a record when it strictly beats every earlier finished,
    non-abandoned run in its scope: per tier for waves, account-wide for total
    coins. A later record does not erase an earlier one - it marks it broken.
    Runs with the value unread (NULL) neither set nor break a record.
    """
    rows = conn.execute(
        "SELECT id, tier, wave, coins + COALESCE(ad_coins, 0) AS total FROM runs "
        "WHERE ended_at IS NOT NULL AND abandoned = 0 ORDER BY id"
    ).fetchall()
    found: dict[int, dict[str, Any]] = {}
    for kind, column, per_tier in (("wave", "wave", True), ("coin", "total", False)):
        best: dict[int | None, tuple[int, int]] = {}
        for row in rows:
            value = row[column]
            if value is None or (per_tier and row["tier"] is None):
                continue
            scope = row["tier"] if per_tier else None
            previous = best.get(scope)
            if previous is not None and value <= previous[1]:
                continue
            entry = found.setdefault(row["id"], dict(_NO_RECORD))
            entry[f"{kind}_record"] = "standing"
            if previous is not None:
                entry[f"{kind}_prev"] = {"run_id": previous[0], "value": previous[1]}
                found[previous[0]][f"{kind}_record"] = "broken"
                found[previous[0]][f"{kind}_broken_by"] = row["id"]
            best[scope] = (row["id"], value)
    return found


def run_upgrade_levels(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]] | None:
    """One run's frozen purchase summary, or None when none was recorded
    (a run older than the events retention, or one that bought nothing)."""
    rows = conn.execute(
        "SELECT upgrade_id, levels, spent, unpriced FROM run_upgrades WHERE run_id = ? ORDER BY upgrade_id",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows] or None
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_db.py tests/test_stats_api.py -q -p no:allure_pytest`
Expected: all pass (`test_stats_api.py` guards the other `list_runs` consumers).

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "Compute chronological wave and coin records on list_runs" -m "<trailer lines>"
```

---

### Task 3: Carry Killed By and ad coins on `RunEnded` into the store

**Files:**
- Modify: `events.py:112-119` (`RunEnded`), `sinks/store.py` (the `case events.RunEnded()` block ~:122-129)
- Test: `tests/test_store_sink.py`

**Interfaces:**
- Consumes: `db.finish_run(..., killed_by=, ad_coins=)` from Task 1.
- Produces: `events.RunEnded.killed_by: str | None = None` and `events.RunEnded.ad_coins: int | None = None`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_store_sink.py`)

```python
def test_the_run_row_carries_killed_by_and_ad_coins(tmp_path: Path) -> None:
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.RunEnded(run_id=1, duration=60.0, wave=11, coins=80, tier=1, killed_by="Tank", ad_coins=0),
    ])
    with db.reader(path) as conn:
        run = db.list_runs(conn)[0]
    assert (run["killed_by"], run["ad_coins"], run["total_coins"]) == ("Tank", 0, 80)
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `../thetowerbot/.venv/bin/pytest tests/test_store_sink.py -q -p no:allure_pytest -k killed_by`
Expected: FAIL with `TypeError: RunEnded.__init__() got an unexpected keyword argument 'killed_by'`.

- [ ] **Step 3: Implement**

`events.py`, in `RunEnded` after `abandoned: bool = False`:

```python
    killed_by: str | None = None
    ad_coins: int | None = None
```

`sinks/store.py`, in the `case events.RunEnded():` call to `db.finish_run`, add after `abandoned=event.abandoned,`:

```python
                    killed_by=event.killed_by, ad_coins=event.ad_coins,
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_store_sink.py tests/test_runs.py -q -p no:allure_pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add events.py sinks/store.py tests/test_store_sink.py
git commit -m "Carry killed_by and ad_coins from RunEnded into the runs row" -m "<trailer lines>"
```

---

### Task 4: Read Killed By and ad coins at a confirmed game over

**Files:**
- Modify: `game_over.py` (add `run_extras` after `parse_frame`); `tower_bot.py` (add `import game_over` beside `import ocr` at :70; `_read_modal_stats` at :503-531; add `_read_modal_extras`)
- Test: create `tests/test_game_over_extras.py`

**Interfaces:**
- Consumes: `game_over.parse_frame(screen, boxes, *, now=None) -> GameOverReading | None`, `ocr.read(screen) -> tuple[TextBox, ...]`, `ocr.parse_number(text) -> int | None`, and the `RunEnded` fields from Task 3.
- Produces:
  - `game_over.run_extras(reading: GameOverReading | None) -> tuple[str | None, int | None]`
  - `TowerBot._read_modal_extras(self) -> tuple[str | None, int | None]`

- [ ] **Step 1: Write the failing tests** (`tests/test_game_over_extras.py`)

```python
"""Killed By and ad coins from the game-over popup, persisted per run."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import cv2
import pytest

import config
import game_over
import ocr
import tower_bot

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b["text"], b["confidence"], config.Rect(*b["rect"]))
                 for b in json.loads((FIXTURES / "ocr" / f"{name}.json").read_text()))


def parsed(name: str) -> game_over.GameOverReading | None:
    return game_over.parse_frame(cv2.imread(str(FIXTURES / f"{name}.png")), recorded(name), now=1_700_000_000.0)


@pytest.mark.parametrize(("name", "expected"), [
    ("game_over", ("Basic", None)),          # ad coins column not drawn
    ("game_over_newhigh", ("Basic", 0)),
])
def test_run_extras_from_recorded_popups(name: str, expected: tuple[str | None, int | None]) -> None:
    assert game_over.run_extras(parsed(name)) == expected


def test_run_extras_is_empty_without_a_reading() -> None:
    assert game_over.run_extras(None) == (None, None)


def test_run_extras_never_reports_an_unreadable_field() -> None:
    field = game_over.ResultField("killed_by", "Killed By", None, "unreadable", 0.0, None)
    reading = game_over.GameOverReading("game_over.result", 0.0, 1080, 2400, "d", (field,))
    assert game_over.run_extras(reading) == (None, None)


def test_modal_extras_swallow_ocr_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> tuple[ocr.TextBox, ...]:
        raise RuntimeError("onnx went away")
    monkeypatch.setattr(ocr, "read", boom)
    assert tower_bot.TowerBot._read_modal_extras(SimpleNamespace(screen=None)) == (None, None)
```

Before running, confirm the bot class name with `grep -n "^class .*:" tower_bot.py | head`. If it isn't `TowerBot`, use the class that defines `_read_modal_stats` in both the test and this plan's code.

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_game_over_extras.py -q -p no:allure_pytest`
Expected: FAIL with `AttributeError: module 'game_over' has no attribute 'run_extras'`.

- [ ] **Step 3: Implement**

`game_over.py`, after `parse_frame`:

```python
def run_extras(reading: GameOverReading | None) -> tuple[str | None, int | None]:
    """Killed By and ad coins for the run record, or None for each field that
    was not positively observed. Absent and unreadable are both None: the
    runs table stores what was read, never a guess."""
    if reading is None:
        return None, None
    observed = {f.key: f.raw_value for f in reading.fields if f.status == "observed" and f.raw_value}
    killed = observed.get("killed_by")
    ad = observed.get("ad_coins_earned")
    return (killed.strip() or None) if killed else None, ocr.parse_number(ad) if ad else None
```

`tower_bot.py`: add `import game_over` next to `import ocr`. In `_read_modal_stats`, compute the extras first and pass them to `dataclasses.replace`:

```python
        killed_by, ad_coins = self._read_modal_extras()
        return dataclasses.replace(
            ended,
            wave=...,            # unchanged
            coins=...,           # unchanged
            tier=...,            # unchanged
            killed_by=killed_by,
            ad_coins=ad_coins,
        )

    def _read_modal_extras(self) -> tuple[str | None, int | None]:
        """Killed By and ad coins by OCR on the confirmed game-over frame.

        Best effort by design: the glyph-read wave/coins/tier stay the run's
        authority, and nothing here may stop the run from ending.
        """
        try:
            return game_over.run_extras(game_over.parse_frame(self.screen, ocr.read(self.screen)))
        except Exception:
            logger.exception("Game-over OCR failed; killed_by and ad_coins left empty")
            return None, None
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_game_over_extras.py tests/test_screen_discovery.py -q -p no:allure_pytest -k "game_over or extras"`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add game_over.py tower_bot.py tests/test_game_over_extras.py
git commit -m "Read Killed By and ad coins from the game-over popup at run end" -m "<trailer lines>"
```

---

### Task 5: `/api/runs/{run_id}/upgrades`

**Files:**
- Modify: `web/app.py` (add `import workshop_levels` with the other top-level imports; add the route directly after `run_purchases` ~:930)
- Test: `tests/test_web_api.py`

**Interfaces:**
- Consumes: `db.run_upgrade_levels` (Task 2), `upgrades.CATALOG` (`Upgrade.id/name/category/unlock`), `workshop_levels.ladders() -> dict[str, Ladder]` (`Ladder.max_level`), and `_history_path(request)`.
- Produces: `GET /api/runs/{run_id}/upgrades` returns `null` or `{"categories": [{"name": "ATTACK"|"DEFENSE"|"UTILITY", "items": [{"upgrade_id", "name", "levels", "max_level", "spent", "unpriced"}]}], "totals": {"levels", "spent", "unpriced"}}`. `/api/runs` rows now also carry the Task 2 fields, with no route change.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_web_api.py`)

```python
def test_run_upgrades_group_levels_by_workshop_tab_with_max_levels(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.start_run(conn, 1, started_at=0.0)
    db.insert_event(conn, a_battle_purchase(1))
    db.insert_event(conn, a_battle_purchase(2, price=None))
    db.finish_run(conn, 1, started_at=0.0, ended_at=60.0, wave=11, coins=80, tier=1,
                  abandoned=False, scan_count=0, tap_count=0, killed_by="Tank", ad_coins=0)
    conn.close()
    body = client.get("/api/runs/1/upgrades").json()
    attack = next(c for c in body["categories"] if c["name"] == "ATTACK")
    damage = next(i for i in attack["items"] if i["upgrade_id"] == "damage")
    assert (damage["levels"], damage["spent"], damage["unpriced"], damage["max_level"]) == (2, 120, 1, 6000)
    assert [c["name"] for c in body["categories"]] == ["ATTACK", "DEFENSE", "UTILITY"]
    assert all(not i["upgrade_id"].startswith("unlock_") for c in body["categories"] for i in c["items"])
    assert body["totals"] == {"levels": 2, "spent": 120, "unpriced": 1}
    run = client.get("/api/runs").json()[0]
    assert (run["killed_by"], run["buys"], run["wave_record"]) == ("Tank", 2, "standing")


def test_run_upgrades_is_null_without_a_record(harness) -> None:
    client, _, _, _, db_path, _ = harness
    conn = db.connect(db_path)
    db.start_run(conn, 1, started_at=0.0)
    conn.close()
    assert client.get("/api/runs/1/upgrades").json() is None
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `../thetowerbot/.venv/bin/pytest tests/test_web_api.py -q -p no:allure_pytest -k run_upgrades`
Expected: FAIL (404 on the route).

- [ ] **Step 3: Implement** (in `create_app`, after the `run_purchases` route)

```python
    @app.get("/api/runs/{run_id}/upgrades")
    def run_upgrades(run_id: int, request: Request) -> dict | None:
        """End-of-run in-run upgrade levels, grouped like the Workshop tabs.

        Every catalog upgrade that has a level ladder is listed so the panel
        can show what was left alone; levels are the bot's buys this run.
        """
        path = _history_path(request)
        if path is None:
            return None
        with db.reader(path) as conn:
            rows = db.run_upgrade_levels(conn, run_id)
        if rows is None:
            return None
        bought = {row["upgrade_id"]: row for row in rows}
        ladders = workshop_levels.ladders()
        categories = []
        for category in ("ATTACK", "DEFENSE", "UTILITY"):
            items = []
            for upgrade in upgrades.CATALOG:
                if upgrade.category != category or upgrade.unlock or upgrade.id not in ladders:
                    continue
                row = bought.get(upgrade.id, {})
                items.append({
                    "upgrade_id": upgrade.id, "name": upgrade.name,
                    "levels": row.get("levels", 0), "max_level": ladders[upgrade.id].max_level,
                    "spent": row.get("spent", 0), "unpriced": row.get("unpriced", 0),
                })
            categories.append({"name": category, "items": items})
        totals = {key: sum(row[key] for row in rows) for key in ("levels", "spent", "unpriced")}
        return {"categories": categories, "totals": totals}
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `../thetowerbot/.venv/bin/pytest tests/test_web_api.py tests/test_account_catalog_api.py -q -p no:allure_pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add web/app.py tests/test_web_api.py
git commit -m "Serve end-of-run upgrade levels per Workshop tab" -m "<trailer lines>"
```

---

### Task 6: Frontend types, fetchers and pure view helpers

**Files:**
- Modify: `web/ui/lib/types.ts` (`RunRow` at :143; add the new types below it), `web/ui/lib/api.ts` (next to `fetchAccountRuns` ~:191)
- Create: `web/ui/app/fleet/reroll/runs/runsView.ts`
- Test: create `web/ui/app/fleet/reroll/runs/runsView.test.ts`

**Interfaces:**
- Produces:
  - types: `RecordState`, `RecordRef`, `RunUpgradeItem`, `RunUpgradesPayload`, and the extended `RunRow`
  - `fetchAccountRunHistory(accountKey: string, expectedAccountId?: string, limit?: number): Promise<RunRow[]>`
  - `fetchAccountRunUpgrades(accountKey: string, runId: number, expectedAccountId?: string): Promise<RunUpgradesPayload | null>`
  - from `runsView.ts`: `FleetRun`, `mergeRuns`, `killedLabel`, `killedBy`, `recordTip`, `totalCoins`

- [ ] **Step 1: Add the types and fetchers** (no behavior yet, so there's nothing to test in isolation)

`lib/types.ts`, extend `RunRow` with optional fields (older workers may omit them):

```ts
  killed_by?: string | null;
  ad_coins?: number | null;
  total_coins?: number | null;
  buys?: number | null;
  wave_record?: RecordState;
  wave_prev?: RecordRef | null;
  wave_broken_by?: number | null;
  coin_record?: RecordState;
  coin_prev?: RecordRef | null;
  coin_broken_by?: number | null;
```

and add:

```ts
export type RecordState = "standing" | "broken" | null;
export interface RecordRef { run_id: number; value: number }
export interface RunUpgradeItem {
  upgrade_id: string; name: string; levels: number; max_level: number; spent: number; unpriced: number;
}
export interface RunUpgradesPayload {
  categories: { name: string; items: RunUpgradeItem[] }[];
  totals: { levels: number; spent: number; unpriced: number };
}
```

`lib/api.ts` (add `RunUpgradesPayload` to the type import):

```ts
export const fetchAccountRunHistory = (accountKey: string, expectedAccountId?: string, limit = 200) =>
  workerRead<RunRow[]>(`/api/runs?limit=${limit}`, accountKey, expectedAccountId);
export const fetchAccountRunUpgrades = (accountKey: string, runId: number, expectedAccountId?: string) =>
  workerRead<RunUpgradesPayload | null>(`/api/runs/${runId}/upgrades`, accountKey, expectedAccountId);
```

- [ ] **Step 2: Write the failing tests** (`runsView.test.ts`)

```ts
import { describe, expect, it } from "vitest";
import type { RunRow } from "@/lib/types";
import { killedBy, killedLabel, mergeRuns, recordTip, totalCoins } from "./runsView";

const row = (over: Partial<RunRow>): RunRow => ({
  id: 1, started_at: 0, ended_at: 100, wave: 10, coins: 50, tier: 1, abandoned: 0, scan_count: 0, tap_count: 0, ...over,
});
const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `acc-${name}` });

describe("mergeRuns", () => {
  it("orders all emulators' runs newest first, live runs by their start", () => {
    const runs = mergeRuns([
      { member: member("A"), runs: [row({ id: 1, ended_at: 100 }), row({ id: 2, ended_at: null, started_at: 500 })] },
      { member: member("B"), runs: [row({ id: 7, ended_at: 300 })] },
    ]);
    expect(runs.map(r => `${r.emulator}#${r.id}`)).toEqual(["A#2", "B#7", "A#1"]);
    expect(runs[1].accountKey).toBe("worker:B");
  });
});

describe("killedLabel", () => {
  it.each([
    [row({ ended_at: null }), "In progress"],
    [row({ abandoned: 1 }), "Abandoned"],
    [row({ killed_by: null }), "Unreadable"],
    [row({ killed_by: "Tank" }), "Tank"],
  ])("%#", (run, label) => expect(killedLabel(run)).toBe(label));
});

describe("killedBy", () => {
  it("counts only finished, read, non-abandoned runs and reports the rest as excluded", () => {
    const summary = killedBy([
      row({ killed_by: "Tank" }), row({ killed_by: "Tank" }), row({ killed_by: "Boss" }),
      row({ abandoned: 1 }), row({ killed_by: null }), row({ ended_at: null, killed_by: "Fast" }),
    ]);
    expect(summary.counts).toEqual([
      { name: "Tank", count: 2, share: 67 }, { name: "Boss", count: 1, share: 33 },
    ]);
    expect(summary.excluded).toBe(2);
  });
});

describe("recordTip and totalCoins", () => {
  it("names the beaten run and the breaker", () => {
    expect(recordTip("wave", row({ tier: 1, wave_record: "broken", wave_prev: { run_id: 3, value: 9 }, wave_broken_by: 8 })))
      .toBe("Wave record at T1 · beat #3 (9) · broken by #8");
    expect(recordTip("coin", row({ coin_record: "standing", coin_prev: null }))).toBe("Coin record · first run · still standing");
    expect(recordTip("wave", row({}))).toBeNull();
  });
  it("prefers the server total and falls back to earned + ad", () => {
    expect(totalCoins(row({ total_coins: 90 }))).toBe(90);
    expect(totalCoins(row({ coins: 50, ad_coins: 10 }))).toBe(60);
    expect(totalCoins(row({ coins: null }))).toBeNull();
  });
});
```

- [ ] **Step 3: Run it and confirm it fails**

Run (in `web/ui`, after `npm ci`): `npx vitest run app/fleet/reroll/runs/runsView.test.ts`
Expected: FAIL (module `./runsView` not found).

- [ ] **Step 4: Implement `runsView.ts`**

```ts
import type { RunRow } from "@/lib/types";

/** A run tagged with the emulator (worker DB) it came from. */
export interface FleetRun extends RunRow { emulator: string; accountKey: string; accountId: string }

type Source = { member: { name: string; account_key: string; account_id: string }; runs: RunRow[] };

const endedOrStarted = (run: RunRow): number => run.ended_at ?? run.started_at;

export function mergeRuns(sources: Source[]): FleetRun[] {
  return sources
    .flatMap(({ member, runs }) => runs.map(run => ({
      ...run, emulator: member.name, accountKey: member.account_key, accountId: member.account_id,
    })))
    .sort((a, b) => endedOrStarted(b) - endedOrStarted(a));
}

const finished = (run: RunRow): boolean => run.ended_at !== null;

/** Never a guess: a live run, an abandoned run and a failed read each say so. */
export function killedLabel(run: RunRow): string {
  if (!finished(run)) return "In progress";
  if (run.abandoned) return "Abandoned";
  return run.killed_by ?? "Unreadable";
}

export function killedBy(runs: RunRow[]): { counts: { name: string; count: number; share: number }[]; excluded: number } {
  const done = runs.filter(finished);
  const known = done.filter(run => !run.abandoned && run.killed_by);
  const tally = new Map<string, number>();
  for (const run of known) tally.set(run.killed_by!, (tally.get(run.killed_by!) ?? 0) + 1);
  const counts = [...tally].map(([name, count]) => ({ name, count, share: Math.round(count / known.length * 100) }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  return { counts, excluded: done.length - known.length };
}

export function totalCoins(run: RunRow): number | null {
  if (run.total_coins !== undefined && run.total_coins !== null) return run.total_coins;
  return run.coins === null ? null : run.coins + (run.ad_coins ?? 0);
}

export function recordTip(kind: "wave" | "coin", run: RunRow): string | null {
  const state = kind === "wave" ? run.wave_record : run.coin_record;
  if (!state) return null;
  const prev = kind === "wave" ? run.wave_prev : run.coin_prev;
  const breaker = kind === "wave" ? run.wave_broken_by : run.coin_broken_by;
  const head = kind === "wave" ? `Wave record at T${run.tier}` : "Coin record";
  const beat = prev ? `beat #${prev.run_id} (${prev.value})` : "first run";
  const tail = state === "broken" && breaker ? `broken by #${breaker}` : "still standing";
  return `${head} · ${beat} · ${tail}`;
}
```

- [ ] **Step 5: Run it and confirm it passes**

Run: `npx vitest run app/fleet/reroll/runs/runsView.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/ui/lib/types.ts web/ui/lib/api.ts web/ui/app/fleet/reroll/runs/runsView.ts web/ui/app/fleet/reroll/runs/runsView.test.ts
git commit -m "Add fleet run fetchers and view helpers" -m "<trailer lines>"
```

---

### Task 7: Runs page, table, killed-by strip, side panel, sidebar tab

**Files:**
- Create: `web/ui/app/fleet/reroll/runs/page.tsx`, `web/ui/app/fleet/reroll/runs/RunsTable.tsx`, `web/ui/app/fleet/reroll/runs/RunDetailPanel.tsx`, `web/ui/app/fleet/reroll/runs/KilledByStrip.tsx`
- Modify: `web/ui/components/Sidebar.tsx` (add `Swords` to the lucide import; add the Runs item after Labs & Gems), `web/ui/app/globals.css` (append the `.no-scrollbar` utility)
- Test: create `web/ui/app/fleet/reroll/runs/page.test.tsx`; update `web/ui/components/Sidebar.test.tsx` (add `"/fleet/reroll/runs/"` to the `test.each` path list)

**Interfaces:**
- Consumes: `useRerollWorkspace()` from `../RerollWorkspace` (`{ pool, loading, error }`, `pool.members: RerollMember[]`), `deviceColor(name)` from `@/lib/rerollState`, `duration(seconds)` from `@/lib/format`, `CATEGORY_COLOR` from `../workshop/workshopFormat`, and everything from Task 6.
- Produces: route `/fleet/reroll/runs/`.

- [ ] **Step 1: Write the failing page test** (`page.test.tsx`)

```tsx
import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import FleetRunsPage from "./page";

const { fetchAccountRunHistory, fetchAccountRunUpgrades, workspace } = vi.hoisted(() => ({
  fetchAccountRunHistory: vi.fn(),
  fetchAccountRunUpgrades: vi.fn(),
  workspace: { pool: { members: [] as { name: string; account_key: string; account_id: string; lease_id: string }[] }, loading: false, error: null },
}));
vi.mock("@/lib/api", () => ({ fetchAccountRunHistory, fetchAccountRunUpgrades }));
vi.mock("../RerollWorkspace", () => ({ useRerollWorkspace: () => workspace }));

const member = (name: string) => ({ name, account_key: `worker:${name}`, account_id: `acc-${name}`, lease_id: name });
const run = (id: number, over: object = {}) => ({
  id, started_at: 0, ended_at: 1000 + id, wave: 10, coins: 50, tier: 1, abandoned: 0, scan_count: 0, tap_count: 0,
  killed_by: "Tank", ad_coins: 0, total_coins: 50, buys: 3, wave_record: null, coin_record: null, ...over,
});

beforeEach(() => {
  fetchAccountRunHistory.mockReset();
  fetchAccountRunUpgrades.mockReset();
  workspace.pool.members = [member("Air_1"), member("Air_2")];
});

describe("FleetRunsPage", () => {
  it("lists runs newest first with standing and broken HS badges", async () => {
    fetchAccountRunHistory.mockImplementation((key: string) => Promise.resolve(key === "worker:Air_1"
      ? [run(2, { wave_record: "standing", wave_prev: { run_id: 1, value: 9 } }), run(1, { wave_record: "broken", wave_broken_by: 2 })]
      : [run(5, { ended_at: 2000, killed_by: null })]));
    render(<FleetRunsPage />);
    const rows = await screen.findAllByRole("button", { name: /^Run #/ });
    expect(rows.map(r => r.getAttribute("aria-label"))).toEqual(["Run #5 on Air_2", "Run #2 on Air_1", "Run #1 on Air_1"]);
    expect(within(rows[0]).getByText("Unreadable")).toBeInTheDocument();
    expect(within(rows[1]).getByLabelText(/Wave record at T1 · beat #1 \(9\) · still standing/)).toHaveAttribute("data-record", "standing");
    expect(within(rows[2]).getByLabelText(/broken by #2/)).toHaveAttribute("data-record", "broken");
    expect(fetchAccountRunHistory).toHaveBeenCalledWith("worker:Air_1", "acc-Air_1");
  });

  it("opens the side panel with levels over max for the clicked run", async () => {
    fetchAccountRunHistory.mockResolvedValue([run(2)]);
    fetchAccountRunUpgrades.mockResolvedValue({
      categories: [{ name: "DEFENSE", items: [{ upgrade_id: "health", name: "Health", levels: 5, max_level: 6000, spent: 40, unpriced: 0 }] }],
      totals: { levels: 5, spent: 40, unpriced: 0 },
    });
    workspace.pool.members = [member("Air_1")];
    render(<FleetRunsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Run #2 on Air_1" }));
    const panel = await screen.findByRole("complementary", { name: "Run details" });
    expect(await within(panel).findByText("5")).toBeInTheDocument();
    expect(within(panel).getByText("/6,000")).toBeInTheDocument();
    expect(fetchAccountRunUpgrades).toHaveBeenCalledWith("worker:Air_1", 2, "acc-Air_1");
    fireEvent.click(within(panel).getByRole("button", { name: "Close run details" }));
    expect(screen.queryByRole("complementary", { name: "Run details" })).toBeNull();
  });

  it("keeps other emulators when one fails and says no purchase record when there is none", async () => {
    fetchAccountRunHistory.mockImplementation((key: string) => key === "worker:Air_1"
      ? Promise.reject(new Error("worker offline")) : Promise.resolve([run(9)]));
    fetchAccountRunUpgrades.mockResolvedValue(null);
    render(<FleetRunsPage />);
    expect(await screen.findByText(/Air_1: worker offline/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Run #9 on Air_2" }));
    expect(await screen.findByText("No purchase record for this run.")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `npx vitest run app/fleet/reroll/runs/page.test.tsx`
Expected: FAIL (module `./page` not found).

- [ ] **Step 3: Implement the components**

`globals.css`, appended at the end:

```css
/* Scrolls, but draws no bar (fleet Runs table and panel). */
.no-scrollbar { scrollbar-width: none; }
.no-scrollbar::-webkit-scrollbar { display: none; }
```

`KilledByStrip.tsx`:

```tsx
import type { RunRow } from "@/lib/types";
import { killedBy } from "./runsView";

const PALETTE = ["var(--ws-defense)", "var(--ws-attack)", "var(--ws-utility)", "oklch(0.75 0.13 85)", "var(--muted-foreground)"];

export function KilledByStrip({ runs }: { runs: RunRow[] }): React.JSX.Element {
  const { counts, excluded } = killedBy(runs);
  return <section aria-label="Killed by" className="space-y-2.5 rounded-xl border bg-card p-4">
    <div className="flex items-baseline justify-between">
      <h2 className="text-sm font-semibold">Killed by</h2>
      <span className="text-xs text-muted-foreground">{runs.length} runs · {excluded} abandoned or unreadable</span>
    </div>
    {counts.length === 0 ? <p className="text-xs text-muted-foreground">No finished run has a Killed By reading yet.</p> : <>
      <div className="flex h-3.5 gap-[2px] overflow-hidden rounded">
        {counts.map((c, i) => <div key={c.name} title={`${c.name}: ${c.count} runs`} className="h-full"
          style={{ flexGrow: c.count, backgroundColor: PALETTE[i % PALETTE.length] }} />)}
      </div>
      <ul className="flex flex-wrap gap-4 text-xs text-muted-foreground">
        {counts.map((c, i) => <li key={c.name} className="inline-flex items-center gap-1.5">
          <i className="size-2.5 rounded-[3px]" style={{ backgroundColor: PALETTE[i % PALETTE.length] }} />{c.name}
          <b className="font-mono text-foreground">{c.count}</b><span>{c.share}%</span>
        </li>)}
      </ul>
    </>}
  </section>;
}
```

`RunsTable.tsx`:

```tsx
import { Trophy } from "lucide-react";
import { duration } from "@/lib/format";
import { deviceColor } from "@/lib/rerollState";
import type { RecordState } from "@/lib/types";
import { killedLabel, recordTip, totalCoins, type FleetRun } from "./runsView";

export const runKey = (run: FleetRun): string => `${run.accountKey}#${run.id}`;

function Badge({ state, tip }: { state: RecordState | undefined; tip: string | null }): React.JSX.Element | null {
  if (!state || !tip) return null;
  const standing = state === "standing";
  return <span aria-label={tip} title={tip} data-record={state}
    className={`inline-flex items-center gap-0.5 rounded-full px-1.5 font-sans text-[10px] font-bold ${standing
      ? "bg-[oklch(0.82_0.14_85)] text-[oklch(0.22_0.03_85)]" : "border border-[oklch(0.82_0.14_85)] text-[oklch(0.82_0.14_85)]"}`}>
    <Trophy aria-hidden="true" className="size-2.5" />HS
  </span>;
}

const COLUMNS = "grid grid-cols-[4rem_8rem_5.5rem_2.75rem_6rem_7rem_5rem_minmax(0,1fr)_4rem] items-center gap-2.5";

export function RunsTable({ runs, selected, onSelect, now }: {
  runs: FleetRun[]; selected: string | null; onSelect: (run: FleetRun) => void; now: number;
}): React.JSX.Element {
  return <div className="flex min-w-0 flex-1 flex-col overflow-hidden rounded-xl border bg-card">
    <div className={`${COLUMNS} border-b px-4 py-2.5 text-[11px] font-semibold tracking-wide text-muted-foreground`}>
      <span>RUN</span><span>EMULATOR</span><span>ENDED</span><span>TIER</span><span>WAVE</span><span>COINS</span>
      <span>DURATION</span><span>KILLED BY</span><span className="text-right">BUYS</span>
    </div>
    <div className="no-scrollbar flex-1 overflow-auto">
      {runs.map(run => {
        const active = selected === runKey(run);
        const coins = totalCoins(run);
        const plain = run.ended_at === null || run.abandoned || !run.killed_by;
        return <button key={runKey(run)} type="button" aria-label={`Run #${run.id} on ${run.emulator}`} aria-pressed={active}
          onClick={() => onSelect(run)}
          className={`${COLUMNS} min-h-12 w-full border-b px-4 text-left text-[13px] ${active ? "bg-primary/15 shadow-[inset_3px_0_0_var(--primary)]" : "hover:bg-muted/50"}`}>
          <span className="font-mono text-muted-foreground">#{run.id}</span>
          <span className="inline-flex items-center gap-1.5 truncate"><i className="size-[7px] shrink-0 rounded-full" style={{ backgroundColor: deviceColor(run.emulator) }} />{run.emulator}</span>
          <span className="text-muted-foreground">{run.ended_at === null ? "live" : `${duration(Math.max(0, now - run.ended_at))} ago`}</span>
          <span className="font-mono">{run.tier === null ? "—" : `T${run.tier}`}</span>
          <span className="inline-flex items-center gap-1.5 font-mono">{run.wave ?? "—"}<Badge state={run.wave_record} tip={recordTip("wave", run)} /></span>
          <span className="inline-flex items-center gap-1.5 font-mono">{coins === null ? "—" : coins.toLocaleString()}<Badge state={run.coin_record} tip={recordTip("coin", run)} /></span>
          <span className="font-mono">{run.ended_at === null ? "—" : duration(run.ended_at - run.started_at)}</span>
          <span className={`truncate ${plain ? "italic text-muted-foreground" : ""}`}>{killedLabel(run)}</span>
          <span className="text-right font-mono text-muted-foreground">{run.buys ?? "—"}</span>
        </button>;
      })}
    </div>
  </div>;
}
```

`RunDetailPanel.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import { fetchAccountRunUpgrades } from "@/lib/api";
import { duration } from "@/lib/format";
import { deviceColor } from "@/lib/rerollState";
import type { RunUpgradesPayload } from "@/lib/types";
import { CATEGORY_COLOR } from "../workshop/workshopFormat";
import { killedLabel, totalCoins, type FleetRun } from "./runsView";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "done"; data: RunUpgradesPayload | null };

const title = (category: string): string => category[0] + category.slice(1).toLowerCase();

export function RunDetailPanel({ run, onClose }: { run: FleetRun; onClose: () => void }): React.JSX.Element {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  useEffect(() => {
    let live = true;
    setLoad({ state: "loading" });
    fetchAccountRunUpgrades(run.accountKey, run.id, run.accountId)
      .then(data => { if (live) setLoad({ state: "done", data }); })
      .catch((failure: unknown) => { if (live) setLoad({ state: "error", message: failure instanceof Error ? failure.message : "Upgrades unavailable" }); });
    return () => { live = false; };
  }, [run.accountKey, run.id, run.accountId]);

  const coins = totalCoins(run);
  const seconds = run.ended_at === null ? null : run.ended_at - run.started_at;
  return <aside aria-label="Run details" className="flex w-[32rem] shrink-0 flex-col overflow-hidden rounded-xl border border-border-strong bg-card">
    <header className="flex items-start justify-between gap-3 border-b px-4 pb-3 pt-4">
      <div className="space-y-1">
        <h2 className="text-base font-bold">Run #{run.id}</h2>
        <p className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <i className="size-[7px] rounded-full" style={{ backgroundColor: deviceColor(run.emulator) }} />{run.emulator} · {run.purpose === "milestone" ? "Milestone run" : "Farm run"}
        </p>
      </div>
      <button type="button" aria-label="Close run details" onClick={onClose} className="inline-flex size-8 items-center justify-center rounded-lg border"><X className="size-3.5" /></button>
    </header>
    <dl className="grid grid-cols-3 gap-2 p-4 text-xs">
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Wave · Tier {run.tier ?? "—"}</dt><dd className="font-mono text-lg font-bold">{run.wave ?? "—"}</dd></div>
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Coins</dt><dd className="font-mono text-lg font-bold">{coins === null ? "—" : coins.toLocaleString()}</dd>
        <dd className="text-muted-foreground">{run.coins ?? "—"} earned + {run.ad_coins ?? "—"} ad</dd></div>
      <div className="rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Duration</dt><dd className="font-mono text-lg font-bold">{seconds === null ? "—" : duration(seconds)}</dd>
        {seconds !== null && run.wave ? <dd className="text-muted-foreground">{Math.round(seconds / run.wave)}s per wave</dd> : null}</div>
      <div className="col-span-3 flex items-center justify-between rounded-lg bg-muted p-2.5"><dt className="text-muted-foreground">Killed by</dt><dd className="text-sm font-semibold">{killedLabel(run)}</dd></div>
    </dl>
    <div className="flex items-baseline justify-between px-4 pb-2">
      <h3 className="text-sm font-semibold">In-run upgrades at game over</h3>
      {load.state === "done" && load.data && <span className="text-[11px] text-muted-foreground">
        {load.data.totals.levels} levels bought · ${load.data.totals.spent.toLocaleString()} spent{load.data.totals.unpriced ? ` (+${load.data.totals.unpriced} unpriced)` : ""}</span>}
    </div>
    <div className="no-scrollbar flex-1 space-y-3 overflow-auto px-4 pb-4">
      {load.state === "loading" && <p role="status" className="text-sm text-muted-foreground">Loading upgrades…</p>}
      {load.state === "error" && <p role="alert" className="text-sm text-danger">{load.message}</p>}
      {load.state === "done" && !load.data && <p className="text-sm text-muted-foreground">No purchase record for this run.</p>}
      {load.state === "done" && load.data?.categories.map(category => {
        const color = CATEGORY_COLOR[category.name];
        const touched = category.items.filter(item => item.levels > 0).length;
        return <section key={category.name} aria-label={`${title(category.name)} upgrades`} className="space-y-2.5 rounded-xl border p-3"
          style={{ borderColor: `color-mix(in oklch, ${color}, transparent 55%)`, backgroundColor: `color-mix(in oklch, ${color}, transparent 93%)` }}>
          <header className="flex items-center justify-between text-xs font-semibold tracking-wide">
            <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-[2px]" style={{ backgroundColor: color }} />{category.name}</span>
            <span className="font-normal text-muted-foreground">{touched}/{category.items.length} bought</span>
          </header>
          <div className="grid grid-cols-2 gap-2">
            {category.items.map(item => <article key={item.upgrade_id} aria-label={item.name}
              className={`space-y-1.5 rounded-[10px] border bg-card p-2.5 ${item.levels === 0 ? "opacity-50" : ""}`}>
              <div className="flex items-baseline justify-between gap-1.5">
                <h4 className="truncate text-xs font-semibold">{item.name}</h4>
                <span className="font-mono text-[11px]"><b>{item.levels}</b><span className="text-muted-foreground">/{item.max_level.toLocaleString()}</span></span>
              </div>
              <div aria-hidden="true" className="h-1.5 overflow-hidden rounded" style={{ backgroundColor: `color-mix(in oklch, ${color}, transparent 82%)` }}>
                <div className="h-full rounded" style={{ width: `${item.levels === 0 ? 0 : Math.max(item.levels / item.max_level * 100, 2)}%`, backgroundColor: color }} />
              </div>
              <p className="text-[11px] text-muted-foreground">{item.levels === 0 ? "Not bought this run" : `+${item.levels} this run · $${item.spent.toLocaleString()}`}</p>
            </article>)}
          </div>
        </section>;
      })}
    </div>
  </aside>;
}
```

`page.tsx`:

```tsx
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { fetchAccountRunHistory } from "@/lib/api";
import type { RunRow } from "@/lib/types";
import { useRerollWorkspace } from "../RerollWorkspace";
import { KilledByStrip } from "./KilledByStrip";
import { RunDetailPanel } from "./RunDetailPanel";
import { RunsTable, runKey } from "./RunsTable";
import { mergeRuns } from "./runsView";

type Source = { runs: RunRow[]; error: string | null };

export default function FleetRunsPage(): React.JSX.Element {
  const { pool, loading, error } = useRerollWorkspace();
  const members = useMemo(() => [...(pool?.members ?? [])].filter(m => !m.hidden)
    .sort((a, b) => a.name.localeCompare(b.name)), [pool]);
  const identity = JSON.stringify(members.map(({ name, account_key, account_id, lease_id }) => [name, account_key, account_id, lease_id]));
  const [sources, setSources] = useState<Record<string, Source>>({});
  const [off, setOff] = useState<Set<string>>(new Set());
  const [recordsOnly, setRecordsOnly] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const epoch = useRef(0);

  useEffect(() => {
    const generation = ++epoch.current;
    setSources({});
    for (const member of members) {
      (async () => {
        let source: Source;
        try {
          if (!member.account_key || !member.account_id) throw new Error("Waiting for a verified account.");
          source = { runs: await fetchAccountRunHistory(member.account_key, member.account_id), error: null };
        } catch (failure) {
          source = { runs: [], error: failure instanceof Error ? failure.message : "Runs unavailable" };
        }
        if (epoch.current === generation) setSources(current => ({ ...current, [member.name]: source }));
      })();
    }
  }, [identity]); // eslint-disable-line react-hooks/exhaustive-deps -- identity captures members

  const all = mergeRuns(members.filter(m => sources[m.name] && m.account_key && m.account_id).map(m => ({
    member: { name: m.name, account_key: m.account_key!, account_id: m.account_id! }, runs: sources[m.name].runs,
  })));
  const shown = all.filter(run => !off.has(run.emulator))
    .filter(run => !recordsOnly || run.wave_record || run.coin_record);
  const active = all.find(run => runKey(run) === selected) ?? null;
  const now = Date.now() / 1000;

  return <main className="flex h-full flex-col gap-4 p-6">
    <header className="flex flex-wrap items-end justify-between gap-4">
      <div><h1 className="text-xl font-bold">Runs</h1>
        <p className="text-sm text-muted-foreground">Every battle across the fleet, newest first. Select a run to see what it bought.</p></div>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        {members.map(member => <button key={member.name} type="button" aria-pressed={!off.has(member.name)}
          onClick={() => setOff(current => { const next = new Set(current); if (next.has(member.name)) next.delete(member.name); else next.add(member.name); return next; })}
          className={`h-8 rounded-full px-3 ${off.has(member.name) ? "border border-dashed text-muted-foreground" : "border bg-muted"}`}>{member.name}</button>)}
        <button type="button" aria-pressed={recordsOnly} onClick={() => setRecordsOnly(value => !value)}
          className={`h-8 rounded-full border px-3 ${recordsOnly ? "border-[oklch(0.82_0.14_85)] text-[oklch(0.82_0.14_85)]" : ""}`}>Records only</button>
      </div>
    </header>
    {error && <p role="alert" className="text-sm text-danger">Fleet unavailable: {error}</p>}
    {members.map(member => sources[member.name]?.error
      ? <p key={member.name} role="alert" className="text-sm text-danger">{member.name}: {sources[member.name].error}</p> : null)}
    {loading && !pool ? <p role="status" className="text-sm text-muted-foreground">Loading fleet…</p>
      : !members.length ? <p className="text-sm text-muted-foreground">No visible emulators in this reroll.</p>
      : <>
        <KilledByStrip runs={shown} />
        <div className="flex min-h-0 flex-1 gap-4">
          <RunsTable runs={shown} selected={selected} onSelect={run => setSelected(runKey(run))} now={now} />
          {active && <RunDetailPanel run={active} onClose={() => setSelected(null)} />}
        </div>
      </>}
  </main>;
}
```

`Sidebar.tsx`: add `Swords` to the lucide import and, after the Labs & Gems item:

```ts
      { href: "/fleet/reroll/runs/", label: "Runs", icon: Swords },
```

`Sidebar.test.tsx`: add `"/fleet/reroll/runs/"` to the `test.each([...])` path list at :12.

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `npx vitest run app/fleet/reroll/runs components/Sidebar.test.tsx`
Expected: PASS. Then run `npx tsc --noEmit -p .` and `npx eslint app/fleet/reroll/runs components/Sidebar.tsx`; both should report nothing new.

- [ ] **Step 5: Commit**

```bash
git add web/ui/app/fleet/reroll/runs web/ui/components/Sidebar.tsx web/ui/components/Sidebar.test.tsx web/ui/app/globals.css
git commit -m "Add the fleet Runs page with HS badges and a per-run upgrade panel" -m "<trailer lines>"
```

---

## Self-review notes

- Spec coverage:
  - DB columns, the table, the backfill and `finish_run` writing the summary are Task 1.
  - Records over full history are Task 2.
  - `RunEnded` → store is Task 3; popup OCR capture is Task 4.
  - `/api/runs` fields and the upgrades route are Tasks 2 and 5.
  - The page, badges, strip, panel, filters, per-worker errors, hidden scrollbars and the sidebar tab are Tasks 6 and 7.
- Field names are refined from the spec: the spec's `record_prev` and `broken_by` became per-kind `wave_prev` / `coin_prev` and `wave_broken_by` / `coin_broken_by`, because one run can hold both records. They are used consistently in Tasks 2, 5, 6 and 7.
- The `/api/runs/{id}/upgrades` payload also returns `null` for a finished run that bought nothing. The panel's copy, "No purchase record for this run.", stays true in that case.
