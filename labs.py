"""Evidence-bounded model of Lab ownership, research levels and running jobs.

The slot-one home and Game Speed research picker now have recorded readers in
`lab_screen`. This module remains the state model rather than a source of tap
targets. Research and acceleration actions have separate safety gates.

Identity comes from the versioned catalog's `labs` concepts and from nothing
else. Several of them research an Ultimate Weapon; the weapon itself is a
different domain and a different task, so an `ultimate-weapons.*` id is
refused here exactly like an invented one.

The distinction this module exists to protect: a lab nobody has looked at is
`unknown`, which is a claim about our EVIDENCE. A lab observed at level 0 is
`available` at 0, which is a claim about the WORLD. Nothing turns the first
into the second, and no number we failed to read is written down as zero.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import threading
from typing import Any

import screen_discovery
from account_state import AccountState, Evidence, Fact
from concepts import REGISTRY

# The identities this module may name, read from the registry rather than
# listed here, so a lab the catalog gains or loses is not a code change.
LAB_CONCEPT_IDS: frozenset[str] = frozenset(
    c.concept_id for c in REGISTRY.concepts if c.domain == 'labs')

UNKNOWN = 'unknown'
# The five things a frame can say about a lab, kept apart on purpose.
# available: seen, readable, more levels remain. maxed: seen at its cap, so
# there is nothing left to research. locked: seen, and the game itself says
# it is not open yet. unavailable: seen and open, but not startable now - no
# free slot, or the cost is not covered. unreadable: the row was on screen
# and its name or its numbers could not be trusted. None of the five is
# `unknown`, and none of them may be derived from another.
ENTRY_STATUSES = ('available', 'maxed', 'locked', 'unavailable', 'unreadable')
# Only these two carry a level worth keeping. The other three describe a row
# we could not price, and a level read off one of them would be a guess.
CLAIMABLE_STATUSES = ('available', 'maxed')

JOB_STATUSES = ('researching', 'idle', 'unreadable')
SLOT_STATUSES = ('observed', 'unreadable', UNKNOWN)
ACCELERATION_STATES = ('none', 'active', UNKNOWN)

_MIN_CONFIDENCE = .9
# The window inside which a second reading still describes the same screen.
# The same bar account_state holds a Workshop stat to, for the same reason:
# one frame plus a hopeful default is how a missing lab becomes a level 0.
_CONFIRM_WINDOW = 30.
# A research timer is drawn as a countdown, so two readings a moment apart
# agree on the absolute finish time rather than on the digits. Two seconds
# covers the redraw; a wider gap is a different job, not the same one ticking.
_COMPLETION_TOLERANCE = 2.


@dataclass(frozen=True)
class LabEntry:
    """One lab row on one frame, addressed by its catalog identity.

    `concept_id` is None when the row's name could not be resolved: an
    unnamed row has no identity rather than a guessed one. `level`, `cost`
    and `duration_s` stay None whenever they were not read, including on a
    row whose status is perfectly clear.
    """

    concept_id: str | None
    raw_name: str
    level: int | None
    max_level: int | None
    cost: float | None
    duration_s: float | None
    status: str
    confidence: float
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class LabJob:
    """One lab slot on one frame, occupied or not.

    `completes_at` is an ABSOLUTE second, not the remaining time the screen
    actually shows: a deadline is comparable across frames and a countdown is
    not. `remaining_s` keeps what was read, so the conversion stays auditable.

    `speed_multiplier` and `acceleration` are carried, never inferred. A slot
    whose speed nobody read has None and `unknown`, which is not 1x and not
    'none'. Spending to change either belongs to the acceleration task.
    """

    slot: int
    concept_id: str | None
    raw_name: str
    completes_at: float | None
    remaining_s: float | None
    speed_multiplier: float | None
    acceleration: str
    status: str
    confidence: float
    rect: tuple[int, int, int, int]


@dataclass(frozen=True)
class LabsReading:
    """One frame of Labs evidence, however it was produced.

    `slots_status` separates a strip that was read from one that was not:
    `slots_owned` is None under both `unreadable` and `unknown`, and neither
    means the account owns no slots.
    """

    observed_at: float
    frame_width: int
    frame_height: int
    frame_digest: str
    slots_owned: int | None
    slots_status: str
    entries: tuple[LabEntry, ...]
    jobs: tuple[LabJob, ...]

    def status_for(self, concept_id: str) -> str:
        """What this frame says about one lab's row, keeping doubt from absence.

        A lab that is not on the frame is `unknown` and never `unseen`. The
        Labs page scrolls, so nothing here can establish that every lab was
        drawn - the completeness argument that lets the missions reader rule
        a mission out has no counterpart until a capture exists.

        A running job is not an answer to this question either: it says what
        the account is paying for, not what level the lab has reached.
        """
        matches = [e for e in self.entries if e.concept_id == concept_id]
        if len(matches) > 1:
            return 'ambiguous'
        return matches[0].status if matches else UNKNOWN

    def strip_read(self) -> bool:
        """Whether every owned slot on this frame was seen and understood.

        Only then may stored jobs be replaced wholesale, because only then
        does an absent job mean a slot that is genuinely idle.
        """
        return (self.slots_status == 'observed' and self.slots_owned is not None
                and len(self.jobs) == self.slots_owned
                and all(j.status in ('researching', 'idle') for j in self.jobs))


def unlock_milestone(concept_id: str) -> tuple[str, tuple[str, ...] | None]:
    """What the catalog says gates this lab: ('unknown', None) for all of them.

    Read from the registry rather than hard-coded, so this starts telling a
    different truth the moment the catalog records a prerequisite. Today
    every lab carries `prerequisites: null`, which means the milestone was
    never inventoried - not that the lab has none.
    """
    concept = REGISTRY.by_id(concept_id) if concept_id in LAB_CONCEPT_IDS else None
    if concept is None or concept.prerequisites is None:
        return (UNKNOWN, None)
    return ('none', ()) if not concept.prerequisites else ('known', concept.prerequisites)


def capabilities() -> dict[str, Any]:
    """A detached support matrix. A modelled state is not a readable screen."""
    return {
        'schema_version': 1,
        'complete': False,
        'concepts': len(LAB_CONCEPT_IDS),
        'reader': 'lab_screen',
        'recorded_capture': 'tests/fixtures/menu_labs_slot1_idle.png',
        # The narrow reroll Lab 1 Game Speed walk lives in lab_visit. Generic
        # research rows, queueing and acceleration still have no executor.
        'actions': ('reroll_game_speed_slot_1',),
        'entry_statuses': ENTRY_STATUSES,
        'job_statuses': JOB_STATUSES,
        'acceleration_states': ACCELERATION_STATES,
        'unlock_milestones': UNKNOWN,
        # Taken from screen_discovery so the gap is declared in exactly one
        # place and this matrix cannot drift away from it.
        'unsupported_owners': {name: owner for name, owner
                               in screen_discovery.capabilities()['unsupported_owners'].items()
                               if name.startswith('labs_')},
    }


def _trusted(confidence: float) -> bool:
    return math.isfinite(confidence) and _MIN_CONFIDENCE <= confidence <= 1


def _claimable_entries(reading: LabsReading) -> dict[str, LabEntry]:
    """The rows whose level may be written down, by identity.

    A duplicated identity is dropped entirely, even when the two rows agree:
    two candidates for one lab is not evidence about that lab.
    """
    rows = [e for e in reading.entries
            if e.concept_id in LAB_CONCEPT_IDS and e.status in CLAIMABLE_STATUSES
            and type(e.level) is int and e.level >= 0
            and (e.max_level is None or type(e.max_level) is int and e.level <= e.max_level)
            and _trusted(e.confidence)]
    return {e.concept_id: e for e in rows
            if sum(other.concept_id == e.concept_id for other in reading.entries) == 1}


def _claimable_jobs(reading: LabsReading) -> dict[str, LabJob]:
    rows = [j for j in reading.jobs
            if j.concept_id in LAB_CONCEPT_IDS and j.status == 'researching'
            and j.completes_at is not None and math.isfinite(j.completes_at)
            and type(j.slot) is int and j.slot >= 0 and _trusted(j.confidence)]
    return {j.concept_id: j for j in rows
            if sum(other.concept_id == j.concept_id for other in reading.jobs) == 1}


def _evidence(reading: LabsReading, raw_name: str, raw_value: str | None,
              confidence: float, rect: tuple[int, int, int, int]) -> Evidence:
    return Evidence(reading.observed_at, confidence, raw_name, raw_value, rect,
                    reading.frame_width, reading.frame_height, reading.frame_digest)


class LabsState:
    """Confirms lab evidence across two fresh readings before it is persisted.

    Holds no device and issues no tap. Nothing in this class can act on the
    game; the worst a bad reading does is fail to become a fact.
    """

    def __init__(self, account: AccountState | None = None) -> None:
        self.account = account
        self._lock = threading.RLock()
        self._reading: LabsReading | None = None
        self._pending: LabsReading | None = None

    def reset_confirmation(self) -> None:
        """Discard the candidate frame when the screen or bot lifetime changes."""
        with self._lock:
            self._pending = None

    def current(self) -> LabsReading | None:
        """The last frame folded in, which is evidence and not yet a fact."""
        with self._lock:
            return self._reading

    def observe(self, reading: LabsReading) -> bool:
        """Fold one frame in; return whether it advanced the persisted account.

        A single frame never advances it. Writing requires two readings that
        agree, taken more than zero and at most `_CONFIRM_WINDOW` seconds
        apart - the bar account_state already holds Workshop stats to.
        """
        with self._lock:
            previous, self._pending, self._reading = self._pending, reading, reading
            if self.account is None or previous is None:
                return False
            if not 0 < reading.observed_at - previous.observed_at <= _CONFIRM_WINDOW:
                return False
            before, now = _claimable_entries(previous), _claimable_entries(reading)
            levels = tuple(Fact(cid, entry.level, entry.status,
                                _evidence(reading, entry.raw_name, str(entry.level),
                                          entry.confidence, entry.rect))
                           for cid, entry in sorted(now.items())
                           if cid in before and before[cid].level == entry.level
                           and before[cid].status == entry.status)
            running = {cid: job for cid, job in _claimable_jobs(reading).items()
                       if cid in _claimable_jobs(previous)
                       and abs(_claimable_jobs(previous)[cid].completes_at
                               - job.completes_at) <= _COMPLETION_TOLERANCE}
            # Jobs may only replace the stored set when BOTH frames read the
            # whole strip and every running slot on it was confirmed. Short
            # of that we know something about some slots and nothing about
            # the rest, which is not a set that may overwrite anything.
            complete = (reading.strip_read() and previous.strip_read()
                        and previous.slots_owned == reading.slots_owned
                        and len(running) == sum(j.status == 'researching' for j in reading.jobs))
            jobs = tuple(Fact(cid, job.completes_at, job.status,
                              _evidence(reading, job.raw_name, str(job.remaining_s),
                                        job.confidence, job.rect))
                         for cid, job in sorted(running.items())) if complete else None
            slots = reading.slots_owned if complete else None
            if not levels and jobs is None and slots is None:
                return False
            return self.account.record_labs(levels, jobs, slots, observed_at=reading.observed_at)

    def levels(self) -> dict[str, dict[str, Any]]:
        """Every catalog lab, `unknown` unless a verified revision names it.

        Built from the catalog rather than from what was observed, so a lab
        that has never been on screen is present and explicitly unknown -
        instead of being absent and read as a zero by whoever asks next.
        """
        stored: dict[str, dict[str, Any]] = {}
        if self.account is not None:
            revision = self.account.snapshot()['revision'] or {}
            for fact in revision.get('lab_levels') or ():
                stored[fact['concept_id']] = {'level': fact['value'], 'status': fact['status'],
                                              'observed_at': fact['evidence']['observed_at']}
        return {cid: stored.get(cid, {'level': None, 'status': UNKNOWN, 'observed_at': None})
                for cid in sorted(LAB_CONCEPT_IDS)}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reading = self._reading
        return {'persistence_available': self.account is not None,
                'capabilities': capabilities(),
                'current': asdict(reading) if reading is not None else None,
                'levels': self.levels()}
