"""Turn confirmed screen transitions into run boundaries.

The FSM already knows when the game moves between the menu, a live run, and
the death modal - which is exactly enough to bracket a run without the bot
having to understand the game.

UNKNOWN deliberately does not close a run: a stray popup mid-battle must not
fabricate a boundary. The run stays open and resumes.

The tracker keys every decision off `curr` and whether a run is currently
open - never off the previous screen. Two reasons, both of which were live
bugs when the previous state was consulted:

* The screen tracker starts at UNKNOWN as a *value*, not an observation. A
  bot launched while a run is already live sees its first confirmed state go
  straight to IN_RUN, and that must open a run.
* An unmodelled screen (an ad, a daily reward) lands exactly between runs.
  IN_RUN -> UNKNOWN -> GAME_OVER must still close the run, or the id leaks
  into the next one and a later death reports the wrong run over the wrong
  duration.

Run ids are handed out from `start_id`, which the caller seeds from the
database so a restart continues the numbering instead of colliding with it.
"""

from __future__ import annotations

import events
from screens import ScreenState


class RunTracker:
    def __init__(self, start_id: int = 1) -> None:
        # Seeded from the database at startup. Run ids are the runs table's
        # primary key: restarting at 1 would overwrite the previous session's
        # runs one by one, exactly as an unseeded seq would collide on the
        # events table.
        self._next_id = start_id
        self.current_id: int | None = None
        self.completed = 0
        self._started_at: float | None = None

    @property
    def next_id(self) -> int:
        """The id the next run will take.

        Public so a restarting BotRunner can seed the next tracker from the
        last one. prepare_store() seeds this from the database at launch, but
        the database is only re-read at launch - and the dashboard can now
        start several bots between two launches.
        """
        return self._next_id

    def elapsed(self, now: float) -> float | None:
        """Seconds since the open run began, or None while none is open.

        None rather than 0.0, and the distinction is not pedantic: a planner
        reading 0.0 between runs would take every menu frame for the opening
        seconds of a battle. `now` is monotonic, as in transition().
        """
        if self._started_at is None:
            return None
        return max(0., now - self._started_at)

    def restore(self, run_id: int, now: float) -> None:
        """Resume a journal-owned run; measure only this observed session."""
        if self.current_id is not None:
            raise ValueError("cannot replace an open run")
        self.current_id = run_id
        self._next_id = max(self._next_id, run_id+1)
        self._started_at = now

    def transition(self, curr: ScreenState, now: float) -> events.Event | None:
        """Return an unstamped RunStarted / RunEnded, or None.

        `curr` is the newly *confirmed* screen state, not a raw reading.
        """
        if curr is ScreenState.UNKNOWN:
            return None  # hold: never open or close a run on an unknown screen

        if curr is ScreenState.IN_RUN and self.current_id is None:
            self.current_id = self._next_id
            self._next_id += 1
            self._started_at = now
            return events.RunStarted(run_id=self.current_id)

        if self.current_id is not None and curr is not ScreenState.IN_RUN:
            run_id = self.current_id
            started = self._started_at
            if started is None:
                # An open run always has a start time. If that ever breaks,
                # say so - do not quietly report duration=0.0 forever.
                raise RuntimeError(f"run {run_id} is open with no start time")
            self.current_id = None
            self._started_at = None
            self.completed += 1
            return events.RunEnded(
                run_id=run_id,
                duration=now - started,
                abandoned=curr is not ScreenState.GAME_OVER,
            )

        return None
