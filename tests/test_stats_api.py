"""Aggregates, in SQL. Read the existing tests/test_db.py for its fixtures
and reuse whatever it already uses to build a populated database."""

from __future__ import annotations

import sqlite3

import pytest

import db


@pytest.fixture
def populated(tmp_path):
    path = tmp_path / "events.db"
    conn = db.connect(path)
    conn.executescript(
        """
        INSERT INTO runs (id, started_at, ended_at, wave, coins, tier, scan_count, tap_count)
        VALUES (1, 100, 200, 50, 1000, 3, 40, 6),
               (2, 300, 460, 70, 2000, 4, 70, 9);

        INSERT INTO events (seq, run_id, ts, type, screen, action, reason)
        VALUES (1, 1, 110, 'Tapped',   'IN_RUN', 'Damage',      NULL),
               (2, 1, 120, 'Tapped',   'IN_RUN', 'Damage',      NULL),
               (3, 2, 310, 'Tapped',   'IN_RUN', 'Attack Speed',NULL),
               (4, 2, 320, 'BotError', NULL,     NULL,          NULL),
               (5, 2, 330, 'ScanCompleted', 'IN_RUN', NULL,     NULL),
               (6, 2, 340, 'ScanCompleted', 'MENU',   NULL,     NULL);
        """
    )
    conn.commit()
    conn.close()
    return path


def test_run_stats_returns_finished_runs_with_their_duration(populated) -> None:
    with db.reader(populated) as conn:
        rows = db.run_stats(conn, limit=10)
    assert [row["id"] for row in rows] == [1, 2]
    assert rows[0]["duration"] == 100
    assert rows[1]["wave"] == 70


def test_taps_by_action_counts_only_taps(populated) -> None:
    with db.reader(populated) as conn:
        rows = db.taps_by_action(conn)
    counts = {row["action"]: row["count"] for row in rows}
    assert counts == {"Damage": 2, "Attack Speed": 1}


def test_screen_histogram_counts_every_event_that_names_a_screen(populated) -> None:
    # Four IN_RUN rows (seq 1, 2, 3, 5) and one MENU (seq 6); seq 4 names no
    # screen and must not be counted.
    with db.reader(populated) as conn:
        rows = db.screen_histogram(conn)
    counts = {row["screen"]: row["count"] for row in rows}
    assert counts == {"IN_RUN": 4, "MENU": 1}


def test_error_log_returns_newest_first(populated) -> None:
    with db.reader(populated) as conn:
        rows = db.error_log(conn, limit=10)
    assert len(rows) == 1
    assert rows[0]["type"] == "BotError"


def test_the_reader_still_cannot_write(populated) -> None:
    """The one guarantee none of this may weaken."""
    with db.reader(populated) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM runs")


def test_progress_uses_all_history_and_first_reaching_run(tmp_path) -> None:
    path = tmp_path / "history.db"
    with db.connect(path) as conn:
        conn.executemany(
            "INSERT INTO runs(id,started_at,ended_at,tier,wave) VALUES(?,?,?,?,?)",
            [(1, 100, 110, 2, 100), (2, 200, 230, 1, 65),
             *[(i, i * 200, i * 200 + 5, 1, 10) for i in range(3, 204)],
             (204, 50000, None, 1, 150)])
    with db.reader(path) as conn:
        assert max(row["wave"] for row in db.run_stats(conn)) == 10
        progress = db.stats_progress(conn)
    assert progress["summary"] == {
        "total_runs": 203, "best_tier_1_wave": 65, "play_seconds": 1045.0}
    for row in progress["benchmarks"][:3]:
        assert row["run_id"] == 2
        assert row["play_seconds"] == 40
        assert row["elapsed_seconds"] == 130
        assert row["reached_at"] == 230
    assert progress["benchmarks"][3]["play_seconds"] is None

    with db.reader(path) as conn:
        custom = db.stats_progress(conn, targets=((1, 30), (2, 90)))
    assert [(row["tier"], row["wave"], row["run_id"], row["play_seconds"])
            for row in custom["benchmarks"]] == [
                (1, 30, 2, 40.0), (2, 90, 1, 10.0)]


def test_progress_distinguishes_unknown_wave_and_unreached_milestones(tmp_path) -> None:
    path = tmp_path / "history.db"
    with db.connect(path) as conn:
        conn.executemany(
            "INSERT INTO runs(id,started_at,ended_at,tier,wave) VALUES(?,?,?,?,?)",
            [(1, 10, 20, None, 100), (2, 30, 40, 1, None),
             (3, 50, 49, 1, 10), (4, 60, 70, 1, 20)])
    with db.reader(path) as conn:
        progress = db.stats_progress(conn)
    assert progress["summary"]["best_tier_1_wave"] == 20
    assert progress["benchmarks"][0]["play_seconds"] == 30
    assert all(row["run_id"] is None for row in progress["benchmarks"][1:])
