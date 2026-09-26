"""One battle's facts, each observed and aged separately, bound to one run.

Three rules hold this module together, and each of them is a failure the bot
would otherwise be unable to see.

* Every fact is its own observation. Wave, health, cash and game speed are
  read out of different parts of the HUD under different bounds, so one of
  them failing says nothing about the rest. Flattened into a single blob, one
  bad OCR frame either refuses every decision or - far worse - lets a
  decision be made from a value the frame never showed.
* A missing fact never becomes a number. `unknown`, `unreadable`,
  `unsupported` and `expired` are four different answers with four different
  remedies, and none of them is 0. Zero cash reads as "broke", zero health
  reads as "dead", and both would drive an action nobody chose.
* Every fact is stamped with the run and the build it was observed under.
  Cash from the previous run is not a small error in this one: the run it
  described no longer exists. The same holds across a build change - a
  Workshop purchase between two scans changes what the same wave means.

Freshness is per fact, not per context, for the same reason. `cash` is
spending evidence and must come from the very frame that is about to be
tapped; `wave` describes a battle that is still the same battle a second
later. One window for both would either make the bot spend on a stale wallet
or make it refuse every guide the moment a single frame dropped.
"""

from __future__ import annotations

import dataclasses
import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from perception import Observation

# The five answers this module can give about a fact. They never collapse
# into one another: `unknown` is "nobody has looked", `unreadable` is "a
# frame was examined and the value did not survive its bounds", `expired` is
# "it was read, under a run/build or at a time that no longer applies", and
# `unsupported` is "no recorded capture shows this at all, and here is who
# owns adding it".
KNOWN = "known"
UNKNOWN = "unknown"
UNREADABLE = "unreadable"
EXPIRED = "expired"
UNSUPPORTED = "unsupported"

# Battle facts the recorded in-run captures do not show, and the graph task
# that owns reading them. An honest gap with an owner is worth more than a
# reader invented from geometry no capture backs: the in-run HUD in
# tests/fixtures/in_run_*.png carries no tier, no perk row, no wall, no card
# or Ultimate Weapon state, and no enemy modifier beyond the damage figure
# perception already bounds. Tier appears only on the death modal, which
# describes the run that just ended rather than this one.
UNSUPPORTED_OWNERS: dict[str, str] = {
    "tier": "F05",
    "perks": "F06",
    "loadout": "F07",
    "wall": "A02",
    "cards": "C01",
    "ultimate_weapons": "U01",
    "enemy_modifiers": "B08",
}

# Facts perception reads off one battle frame. Named here so that a frame
# which yields some of them records the rest as *unreadable on that frame*
# rather than leaving them silently absent.
HUD_FACTS: tuple[str, ...] = (
    "wave", "health", "max_health", "health_regen", "enemy_damage",
    "game_speed", "paused", "cash",
)

# Facts about the run rather than the HUD: the tracker's run boundary supplies
# them, not OCR.
RUN_FACTS: tuple[str, ...] = ("elapsed", "purpose")

# Spending evidence has a window of zero on purpose: a wallet read one frame
# ago has already been invalidated by the purchase, the wave payout or the
# kill that happened in between, and "nearly current" cash is exactly how a
# reserve gets spent through. Everything else on the HUD keeps describing the
# same battle for a couple of seconds, which is what stops one dropped OCR
# frame from flipping a guide into "wait".
_SPENDING_WINDOW = 0.
_HUD_WINDOW = 2.
FRESHNESS: dict[str, float] = {
    "cash": _SPENDING_WINDOW,
    "wave": _HUD_WINDOW,
    "health": _HUD_WINDOW,
    "max_health": _HUD_WINDOW,
    "health_regen": _HUD_WINDOW,
    "enemy_damage": _HUD_WINDOW,
    "game_speed": _HUD_WINDOW,
    "paused": _HUD_WINDOW,
    "elapsed": _HUD_WINDOW,
    # The purpose is chosen when the run starts and cannot drift during it;
    # it expires with the run identity, not with time.
    "purpose": math.inf,
}


@dataclass(frozen=True)
class RunIdentity:
    """Which run, and which build, an observation belongs to.

    Fields left None are *not known on this scan*, which is deliberately not
    the same as being different: a scan that cannot name the build revision
    is no evidence that the build changed, and treating it as evidence would
    throw away the whole context every time the account database was busy.
    """

    run_id: int | None = None
    build_revision: int | None = None
    purpose: str | None = None

    @property
    def identified(self) -> bool:
        return any(value is not None for value in dataclasses.astuple(self))

    def differs_from(self, other: RunIdentity) -> bool:
        """True only where both sides name a field and the names disagree."""
        return any(
            mine is not None and theirs is not None and mine != theirs
            for mine, theirs in zip(dataclasses.astuple(self), dataclasses.astuple(other))
        )

    def describe(self) -> str:
        parts = [f"{field.name}={value}" for field, value
                 in zip(dataclasses.fields(self), dataclasses.astuple(self))
                 if value is not None]
        return ", ".join(parts) if parts else "an unidentified run"


@dataclass(frozen=True)
class Reading:
    """One battle fact, with the state that explains why it has no value."""

    name: str
    state: str
    value: float | bool | str | None = None
    observed_at: float | None = None
    identity: RunIdentity = RunIdentity()
    owner: str | None = None
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.state == KNOWN and self.value is not None

    def describe(self) -> str:
        if self.state == UNSUPPORTED:
            return f"{self.name} is unsupported (owner {self.owner})"
        if self.detail:
            return f"{self.name} is {self.state} ({self.detail})"
        return f"{self.name} is {self.state}"


def _numeric(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def build_revision(account_state: Any | None) -> int | None:
    """The account revision id the current build is recorded under.

    Read through AccountState's public snapshot rather than its internals:
    observing the build is this module's job, owning how it is persisted is
    not.
    """
    if account_state is None:
        return None
    revision = account_state.snapshot().get("revision")
    if not isinstance(revision, Mapping):
        return None
    value = revision.get("revision_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def frame_combat(stored: Mapping[str, float], frame: Observation) -> dict[str, float]:
    """`stored` facts, overlaid with the HUD facts `frame` itself read.

    A battle scan decides before the autopilot stores its frame, so what the
    context holds is the previous scan's - older than the HUD window at the
    battle scan pace. The frame on screen is the current evidence.
    """
    current = {name: value for name, raw in frame.combat.items()
               if name in HUD_FACTS and (value := _numeric(raw)) is not None}
    return {**stored, **current}


class CombatContext:
    """Separate, run-bound observations of the battle currently on screen."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._identity = RunIdentity()
        self._known: dict[str, Reading] = {}
        self._failed: dict[str, float] = {}
        self._expired: dict[str, Reading] = {}

    @property
    def identity(self) -> RunIdentity:
        with self._lock:
            return self._identity

    def rebind(self, identity: RunIdentity) -> bool:
        """Adopt `identity`, expiring everything the old one carried.

        Returns whether anything was expired, so a caller can say why it
        suddenly knows nothing. Expiry replaces values with None rather than
        deleting the fact: "this was read under run 4" is a better answer to
        the operator than pretending the bot never looked.
        """
        with self._lock:
            if not identity.differs_from(self._identity):
                # Same run, or a scan that simply knows less about it. Fill in
                # the fields this scan does know without discarding evidence.
                self._identity = RunIdentity(*[
                    theirs if theirs is not None else mine
                    for mine, theirs in zip(dataclasses.astuple(self._identity),
                                            dataclasses.astuple(identity))
                ])
                return False
            previous = self._identity
            self._expire(f"observed under {previous.describe()}")
            self._identity = identity
            return True

    def clear(self) -> None:
        """Drop the run: the boundary itself is proof the context is over."""
        with self._lock:
            self._expire(f"the run ended ({self._identity.describe()})")
            self._identity = RunIdentity()

    def _expire(self, detail: str) -> None:
        for name, reading in self._known.items():
            self._expired[name] = Reading(name, EXPIRED, None, reading.observed_at,
                                          reading.identity, detail=detail)
        for name, failed_at in self._failed.items():
            self._expired.setdefault(name, Reading(name, EXPIRED, None, failed_at,
                                                   self._identity, detail=detail))
        self._known.clear()
        self._failed.clear()

    def observe(self, observation: Observation, *, identity: RunIdentity = RunIdentity(),
                cash: int | None = None, elapsed: float | None = None,
                now: float | None = None) -> None:
        """Record one battle frame as separate facts under `identity`."""
        now = observation.observed_at if now is None else now
        with self._lock:
            self.rebind(identity)
            if observation.context != "battle":
                # A Workshop or menu frame is no evidence about the HUD; it
                # must not mark battle facts unreadable.
                return
            values: dict[str, Any] = dict(observation.combat)
            values["cash"] = cash if cash is not None else observation.cash
            values["paused"] = observation.paused
            if elapsed is not None:
                values["elapsed"] = elapsed
            if self._identity.purpose is not None:
                values["purpose"] = self._identity.purpose
            for name in (*HUD_FACTS, *RUN_FACTS):
                if name in UNSUPPORTED_OWNERS:
                    continue
                value = values.get(name)
                if value is None:
                    # Only HUD facts are marked unreadable here: the frame was
                    # examined and this value did not survive its bounds. A run
                    # fact the caller did not supply was never looked at.
                    if name in HUD_FACTS:
                        self._failed[name] = now
                        self._known.pop(name, None)
                    continue
                self._failed.pop(name, None)
                self._expired.pop(name, None)
                self._known[name] = Reading(name, KNOWN, value, now, self._identity)

    def reading(self, name: str, now: float) -> Reading:
        """The current state of one fact - never a substituted number."""
        owner = UNSUPPORTED_OWNERS.get(name)
        if owner is not None:
            return Reading(name, UNSUPPORTED, None, None, self.identity, owner=owner)
        with self._lock:
            known = self._known.get(name)
            if known is not None:
                age = now - (known.observed_at if known.observed_at is not None else now)
                window = FRESHNESS.get(name, _HUD_WINDOW)
                if 0 <= age <= window:
                    return known
                return Reading(name, EXPIRED, None, known.observed_at, known.identity,
                               detail=f"last read {age:.1f}s ago")
            failed_at = self._failed.get(name)
            if failed_at is not None:
                return Reading(name, UNREADABLE, None, failed_at, self._identity,
                               detail="the frame was examined and the value did not read")
            expired = self._expired.get(name)
            if expired is not None:
                return expired
            return Reading(name, UNKNOWN, None, None, self._identity)

    def combat(self, now: float) -> dict[str, float]:
        """Fresh numeric facts only, for a policy that reads them by key.

        A fact that is unknown, unreadable, expired or unsupported is simply
        absent: the policies refuse to decide without the keys they need, and
        that refusal is the point.
        """
        result: dict[str, float] = {}
        for name in (*HUD_FACTS, *RUN_FACTS):
            reading = self.reading(name, now)
            value = _numeric(reading.value) if reading.known else None
            if value is not None:
                result[name] = value
        return result

    def refuse(self, action: str, names: Iterable[str], *, now: float) -> str | None:
        """Why `action` must not run, or None when every named fact is fresh."""
        for name in names:
            reading = self.reading(name, now)
            if not reading.known:
                return f"{action} needs {reading.describe()}"
        return None
