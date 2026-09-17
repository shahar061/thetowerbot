from __future__ import annotations

from pathlib import Path

import events
from fleet.reroll_journal import RerollJournal
from sinks.reroll_journal import RerollJournalSink


def test_worker_event_is_written_with_instance_identity(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    sink = RerollJournalSink(journal, "Tiramisu64_20")
    sink.handle(events.RunEnded(seq=2, ts=100.0, run_id=1,
                                duration=60, wave=75, coins=150, tier=1))
    entry = journal.list_entries()[0]
    assert entry["instance"] == "Tiramisu64_20"
    assert entry["kind"] == "RunEnded"
    assert "wave=75" in entry["message"]
    assert entry["color"].startswith("#")


def test_scan_events_are_sampled_to_limit_background_io(tmp_path: Path) -> None:
    journal = RerollJournal(tmp_path)
    sink = RerollJournalSink(journal, "Tiramisu64_20")
    for ts in (100.0, 101.0, 120.0, 130.0):
        sink.handle(events.ScanCompleted(seq=int(ts), ts=ts,
                                         screen="BATTLE", duration_ms=20))
    assert len(journal.list_entries()) == 2
