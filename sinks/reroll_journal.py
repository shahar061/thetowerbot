"""Nonblocking fan-out from a worker event bus to the shared Reroll journal."""

from __future__ import annotations

from datetime import datetime, timezone

import events
from fleet.reroll_journal import RerollJournal
from sinks.base import QueueSink
from sinks.log import render


class RerollJournalSink(QueueSink):
    def __init__(self, journal: RerollJournal, instance: str) -> None:
        super().__init__(maxsize=1000)
        self.journal = journal
        self.instance = instance
        self._last_diagnostic: dict[str, float] = {}

    def handle(self, event: events.Event) -> None:
        diagnostic = isinstance(event, (events.ScanCompleted, events.Skipped))
        if diagnostic:
            last = self._last_diagnostic.get(event.type, float("-inf"))
            if event.ts - last < 30:
                return
            self._last_diagnostic[event.type] = event.ts
        level = "error" if isinstance(event, (events.BotError, events.IdentityIncident)) else "info"
        timestamp = datetime.fromtimestamp(event.ts, timezone.utc)
        self.journal.append(instance=self.instance, level=level,
                            kind="diagnostic" if diagnostic else event.type,
                            message=render(event).split(" ", 1)[-1], at=timestamp)
