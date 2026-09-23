from __future__ import annotations

from pathlib import Path

import db
import events
from sinks.store import StoreSink, to_row


def stamped(event: events.Event, seq: int = 1, ts: float = 1000.0) -> events.Event:
    import dataclasses

    return dataclasses.replace(event, seq=seq, ts=ts)


def drain(tmp_path: Path, stream: list[events.Event]) -> Path:
    """Feed a sink and shut it down, so every event is written before we read."""
    path = tmp_path / "bot.db"
    sink = StoreSink(path)
    sink.start()
    bus = events.EventBus()
    bus.subscribe(sink)
    for event in stream:
        bus.publish(event)
    sink.close()
    return path


def test_a_tap_maps_its_typed_columns_and_blobs_the_rest() -> None:
    row = to_row(stamped(events.Tapped(action="Damage", x=840, y=1520, score=0.94,
                                       price=120, wallet=300)), run_id=2)

    assert (row["type"], row["action"], row["run_id"]) == ("Tapped", "Damage", 2)
    assert (row["score"], row["price"], row["wallet"]) == (0.94, 120, 300)
    assert '"x": 840' in row["detail"] and '"y": 1520' in row["detail"]


def test_a_screen_change_stores_the_new_screen_in_the_screen_column() -> None:
    """`curr` IS the screen; blobbing it would leave the column empty on the
    one event that defines it."""
    row = to_row(
        stamped(events.ScreenChanged(prev="MAIN_MENU", curr="IN_RUN",
                                     confidence=0.99, scores={"IN_RUN": 0.99})),
        run_id=None,
    )

    assert row["screen"] == "IN_RUN"
    assert '"prev": "MAIN_MENU"' in row["detail"]


def test_scan_completed_is_never_stored(tmp_path: Path) -> None:
    """43,000 near-identical rows a day is not history, it is noise."""
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.ScanCompleted(screen="IN_RUN", duration_ms=90.0, wallet=300),
        events.ScanCompleted(screen="IN_RUN", duration_ms=91.0, wallet=310),
    ])

    with db.reader(path) as conn:
        stored = db.run_events(conn, 1)

    assert [e["type"] for e in stored] == ["RunStarted"]


def test_a_screen_change_that_changed_nothing_is_not_stored(tmp_path: Path) -> None:
    """Booting onto an unmodelled screen emits UNKNOWN -> UNKNOWN once.

    A genuine change (UNKNOWN -> MAIN_MENU) sits right beside it and must
    still be stored - otherwise a `handle()` that dropped every
    ScreenChanged, not just the no-op ones, would pass this test too."""
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.ScreenChanged(prev="UNKNOWN", curr="UNKNOWN", confidence=0.4, scores={}),
        events.ScreenChanged(prev="UNKNOWN", curr="MAIN_MENU", confidence=0.9, scores={}),
    ])

    with db.reader(path) as conn:
        stored = db.run_events(conn, 1)

    assert [e["type"] for e in stored] == ["RunStarted", "ScreenChanged"]
    assert stored[1]["screen"] == "MAIN_MENU"


def test_events_are_correlated_to_the_open_run(tmp_path: Path) -> None:
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.RunEnded(run_id=1, duration=60.0, wave=12, coins=900, tier=2),
        events.Navigated(target="RETRY"),
    ])

    with db.reader(path) as conn:
        in_run = db.run_events(conn, 1)
        orphans = conn.execute(
            "SELECT type FROM events WHERE run_id IS NULL"
        ).fetchall()

    assert [e["type"] for e in in_run] == ["RunStarted", "Tapped", "RunEnded"]
    assert [row["type"] for row in orphans] == ["Navigated"]


def test_the_run_row_carries_its_stats_and_counters(tmp_path: Path) -> None:
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.ScanCompleted(screen="IN_RUN", duration_ms=90.0),
        events.ScanCompleted(screen="IN_RUN", duration_ms=90.0),
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.RunEnded(run_id=1, duration=60.0, wave=12, coins=900, tier=2),
    ])

    with db.reader(path) as conn:
        run = db.list_runs(conn)[0]

    assert (run["wave"], run["coins"], run["tier"]) == (12, 900, 2)
    assert (run["scan_count"], run["tap_count"]) == (2, 1)
    assert run["ended_at"] is not None


def test_counters_reset_between_runs(tmp_path: Path) -> None:
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.RunEnded(run_id=1, duration=10.0),
        events.RunStarted(run_id=2),
        events.RunEnded(run_id=2, duration=10.0),
    ])

    with db.reader(path) as conn:
        runs = {r["id"]: r for r in db.list_runs(conn)}

    assert runs[1]["tap_count"] == 1
    assert runs[2]["tap_count"] == 0


def test_orphan_events_between_runs_do_not_corrupt_counts(tmp_path: Path) -> None:
    """Events after a run ends but before the next starts belong to no run.

    Verifies that _run_id is cleared immediately after RunEnded, so orphan
    events do not get misattributed to the next run's counters.
    """
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.RunEnded(run_id=1, duration=10.0),
        # Orphan events after run 1 ends but before run 2 starts
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
        events.ScanCompleted(screen="IN_RUN", duration_ms=90.0),
        events.RunStarted(run_id=2),
        events.RunEnded(run_id=2, duration=10.0),
    ])

    with db.reader(path) as conn:
        runs = {r["id"]: r for r in db.list_runs(conn)}
        orphans = conn.execute(
            "SELECT type FROM events WHERE run_id IS NULL ORDER BY seq"
        ).fetchall()

    # Run 1 keeps only the tap it earned before it ended
    assert runs[1]["tap_count"] == 1
    # Run 2 has no counters (the orphan tap/scan do not land on it)
    assert runs[2]["tap_count"] == 0
    assert runs[2]["scan_count"] == 0
    # The orphan Tapped is stored with run_id=NULL
    # (ScanCompleted is not stored, so only one orphan row)
    assert len(orphans) == 1
    assert orphans[0]["type"] == "Tapped"


def test_a_purchase_lands_in_both_the_events_table_and_the_ledger(
    tmp_path: Path,
) -> None:
    path = drain(tmp_path, [
        events.Purchased(item="Health", category="DEFENSE", price=75,
                         coins_before=1770, dry_run=False),
    ])

    with db.reader(path) as conn:
        assert len(db.ledger_page(conn)) == 1
        assert db.ledger_page(conn)[0]["kind"] == "WORKSHOP_BUY"
        assert db.last_balances(conn)["coins"] == 1695


def test_a_tap_writes_an_event_row_and_no_ledger_line(tmp_path: Path) -> None:
    """In-run taps are not account history. They are bought with per-run
    cash, which resets."""
    path = drain(tmp_path, [events.Tapped(action="Damage", x=1, y=2, score=0.9)])

    with db.reader(path) as conn:
        assert db.ledger_page(conn) == []


def test_a_failing_ledger_write_still_records_the_event(
    tmp_path: Path, monkeypatch
) -> None:
    """The event history is the more important of the two. A ledger that
    cannot write must lose ledger lines, never event rows - and it must not
    skip the run bookkeeping that follows it either."""
    import ledger

    def boom(self, event):  # noqa: ANN001, ANN201
        raise RuntimeError("ledger is broken")

    monkeypatch.setattr(ledger.LedgerWriter, "lines_for", boom)
    path = drain(tmp_path, [
        events.RunStarted(run_id=1),
        events.RunEnded(run_id=1, duration=10.0, wave=5, coins=100, tier=1),
        events.Tapped(action="Damage", x=1, y=2, score=0.9),
    ])

    with db.reader(path) as conn:
        stored = conn.execute("SELECT type FROM events ORDER BY seq").fetchall()
        assert [row[0] for row in stored] == ["RunStarted", "RunEnded", "Tapped"]
        # _run_id was still cleared after RunEnded, so the trailing tap is
        # not misattributed to the run that already finished.
        orphan = conn.execute(
            "SELECT run_id FROM events WHERE type = 'Tapped'"
        ).fetchone()
        assert orphan[0] is None


def test_control_changes_are_persisted(tmp_path: Path) -> None:
    """A setting change must be as reconstructable as every other change.

    "Why did it stop tapping at 3am" is exactly the question the event log
    exists to answer, so the control surface cannot be the one thing that
    mutates the bot without leaving a row behind.
    """
    path = drain(tmp_path, [
        events.ControlChanged(changed={"paused": True}, source="web"),
    ])

    with db.reader(path) as conn:
        rows = [
            dict(row)
            for row in conn.execute("SELECT type, detail FROM events").fetchall()
        ]

    assert rows and rows[0]["type"] == "ControlChanged"
    # The events table has no column for `changed`; the JSON detail blob is
    # what absorbs a new event type with no schema migration.
    assert '"paused": true' in rows[0]["detail"]


def test_a_mission_claim_keeps_both_of_its_currencies(tmp_path: Path) -> None:
    """The regression. classify() returns a coins line AND a gems line for one
    claim, both carrying the claim's seq. With the ledger's unique index keyed
    on seq alone, INSERT OR IGNORE kept the coins and silently dropped the
    gems - on every mission claim, through exactly this live path."""
    path = drain(tmp_path, [
        events.MissionClaimed(mission="Kill 1,000 enemies", coins=120, gems=5),
    ])

    with db.reader(path) as conn:
        moved = {line["currency"]: line["delta"] for line in db.ledger_page(conn)}

    assert moved == {"coins": 120, "gems": 5}
