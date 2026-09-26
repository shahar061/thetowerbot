"""What the bot is doing right now, accumulated from the event stream.

The TUI panel and the dashboard's /api/status answer the same question, so
they share one accumulator instead of each deriving it and drifting apart.
No terminal, no web server, no database - just state, so it tests directly.

Everything is guarded by a lock. The events arrive on a sink's consumer
thread while the web layer reads snapshots from request threads, and copying
a Counter that is being mutated raises "dictionary changed size during
iteration". The lock is held for microseconds and never while doing I/O.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from typing import Any

import events
from sinks.base import QueueSink
from sinks.log import render


class BotState:
    def __init__(self, tail: int = 12) -> None:
        self._lock = threading.Lock()
        self.screen = "UNKNOWN"
        self.scans = 0
        self.taps: Counter[str] = Counter()
        self.skips: Counter[str] = Counter()
        self.last_error: str | None = None
        self.stalled: str | None = None
        self.started = time.monotonic()
        self.tail: deque[str] = deque(maxlen=tail)
        self.run_id: int | None = None
        self.run_started: float | None = None
        self.runs_completed = 0
        self.wallet: int | None = None
        self.run_taps: Counter[str] = Counter()

    def reset(self) -> None:
        """Forget the previous bot. Called by BotRunner on every start.

        BotState now outlives any individual bot, because the dashboard does.
        Without this, uptime, scans and the tap tallies accumulate across bot
        lifetimes and the status bar shows a stopped bot's scan count beside
        a fresh bot's uptime.
        """
        with self._lock:
            self.screen = "UNKNOWN"
            self.scans = 0
            self.started = time.monotonic()
            self.runs_completed = 0
            self.run_id = None
            self.run_started = None
            self.run_taps = Counter()
            self.wallet = None
            self.last_error = None
            self.stalled = None
            self.taps = Counter()
            self.skips = Counter()
            self.tail.clear()

    def apply(self, event: events.Event) -> None:
        with self._lock:
            match event:
                case events.ScreenChanged():
                    self.screen = event.curr
                    self.tail.append(render(event))
                case events.ScanCompleted():
                    self.scans += 1
                    self.screen = event.screen
                    self.wallet = event.wallet
                    # ScanCompleted only fires after a scan finishes without
                    # raising, so seeing one means the device recovered -
                    # otherwise a single transient EmulatorError pins a red
                    # line in the header for the rest of the session, long
                    # after the thing it was warning about is over.
                    self.last_error = None
                case events.Tapped():
                    self.taps[event.action] += 1
                    self.run_taps[event.action] += 1
                    self.tail.append(render(event))
                case events.Skipped():
                    self.skips[event.reason] += 1
                case events.RunStarted():
                    self.run_id = event.run_id
                    self.run_started = event.ts
                    self.run_taps = Counter()
                    self.tail.append(render(event))
                case events.RunEnded():
                    self.run_id = None
                    self.run_started = None
                    self.runs_completed += 1
                    self.tail.append(render(event))
                case events.BotError():
                    self.last_error = event.message
                    self.tail.append(render(event))
                case events.WorkerStalled() if event.stage == "paused":
                    # Unlike last_error it survives the scans that follow:
                    # a paused bot keeps scanning, and this is why it paused.
                    self.stalled = event.reason
                    self.tail.append(render(event))
                case events.ControlChanged() if event.changed.get("paused") is False:
                    self.stalled = None
                    self.tail.append(render(event))
                case _:
                    self.tail.append(render(event))

    @property
    def uptime(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self) -> dict[str, Any]:
        """A JSON-safe copy for /api/status.

        `run_started` is wall clock (the event's ts), so elapsed is measured
        against time.time(); uptime is monotonic and measured against its own
        clock. Mixing the two would be a bug the moment the host's clock moves.
        """
        with self._lock:
            run: dict[str, Any] | None = None
            if self.run_id is not None and self.run_started is not None:
                run = {
                    "id": self.run_id,
                    "started_at": self.run_started,
                    "elapsed": round(time.time() - self.run_started, 1),
                    "taps": dict(self.run_taps),
                }
            return {
                "screen": self.screen,
                "uptime": round(self.uptime, 1),
                "scans": self.scans,
                "taps": dict(self.taps),
                "skips": dict(self.skips),
                "runs_completed": self.runs_completed,
                "run": run,
                "wallet": self.wallet,
                "last_error": self.last_error,
                "stalled": self.stalled,
                "tail": list(self.tail),
            }


class StateSink(QueueSink):
    """Feeds a BotState off the bus when no TUI is doing it already."""

    def __init__(self, state: BotState, maxsize: int = 1000) -> None:
        super().__init__(maxsize=maxsize)
        self.state = state

    def handle(self, event: events.Event) -> None:
        self.state.apply(event)
