"""Which free claim is owed, as a pure rule over a state and a clock.

No device, no database and no clock of its own: `now` and the last-claim
times are parameters, so the whole policy is decidable from a synthetic state
and the loop's call site stays the only place that knows the real time.

`None` is used throughout for "nobody has read or done this", which is
deliberately not the same fact as a zero. An unread best wave owes nothing
rather than a guess.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

ClaimKind = Literal["missions", "milestones"]

SECONDS_PER_HOUR = 3600.0

# The waves at which a tier's MILESTONES ladder pays, the same rows on every
# tier (https://the-tower-idle-tower-defense.fandom.com/wiki/Milestones). A new
# best only owes a claim when it crosses one of these: under autopilot the best
# creeps up by a wave or two on nearly every run, and a menu walk for a best
# that crossed nothing is a trip spent for nothing. A superset is the safe
# error - an extra row costs one walk that finds no `Claim All`, a missing row
# leaves a reward unclaimed.
MILESTONE_WAVES: tuple[int, ...] = (
    10, 20, 30, 40, 50, 60, 70, 80, 90, 100,
    150, 200, 250, 300, 400, 500, 750,
    1000, 1250, 1500, 2000, 2500, 4500,
)

# The MILESTONES badge is the game's own count of claimable rewards, so seeing
# it is due on its own. But whether it also counts the premium rewards this
# walk can never take is unrecorded; a badge that a walk cannot clear would
# otherwise re-arm the walk on every main-menu frame. So a badge alone re-arms
# at most this often. A crossed threshold is not throttled - it fires once per
# crossing by construction.
MIN_MILESTONES_HOURS = 1.0


def crossed_threshold(best_wave: int | None, claimed_best_wave: int | None) -> bool:
    """True if `best_wave` reached a ladder row `claimed_best_wave` had not.

    None claimed means never claimed, which any read best owes - not coerced
    to 0, which would reach the same answer below wave 10 by accident only.
    """
    if best_wave is None:
        return False
    if claimed_best_wave is None:
        return True
    return any(claimed_best_wave < wave <= best_wave for wave in MILESTONE_WAVES)


@dataclass(frozen=True)
class ClaimState:
    """Everything the cadence needs, and nothing it does not.

    `claimed_best_wave` is the wave the ladder was last claimed at, not the
    last reward taken: the MILESTONES page has no per-claim counter, so
    "have we claimed since we got further" is the only question the evidence
    can answer. None means the ladder has never been claimed - the live
    account's actual state.

    Both waves are for ONE tier - the one last played - because every tier
    has its own ladder: a Tier 2 wave 50 owes Tier 2's rows even when Tier 1's
    best is far higher.

    `milestones_badge` is whether the main menu last showed the MILESTONES
    badge. `last_milestones` throttles how often that alone can re-arm the
    walk (see MIN_MILESTONES_HOURS): None means never claimed, which is
    immediately due rather than blocked.
    """

    last_missions: float | None
    last_milestones: float | None
    best_wave: int | None
    claimed_best_wave: int | None
    milestones_badge: bool = False


def due(
    state: ClaimState,
    *,
    now: float,
    missions_every_hours: float,
    milestones_on_new_best: bool,
) -> ClaimKind | None:
    """The one claim to offer next, or None.

    Returns at most ONE kind because at most one maintenance walk may be
    armed at a time - every BotRunner.request_* refuses against every other
    walk's .active. Returning a list would invite a caller to arm two and
    silently lose the second to a 409.

    Milestones outrank missions: the ladder pays 1,660 coins, 235 gems and 5
    stones once, missions pay 3 gems and come back in eight hours.
    """
    if not math.isfinite(now):
        return None

    if milestones_on_new_best:
        if crossed_threshold(state.best_wave, state.claimed_best_wave):
            return "milestones"
        # A non-finite or backwards-clock last_milestones is handled exactly
        # as defensively as last_missions is below: waiting is the safe
        # reading, so a badge on bad data falls through to the missions check.
        if state.milestones_badge:
            if state.last_milestones is None:
                return "milestones"
            if math.isfinite(state.last_milestones):
                elapsed = now - state.last_milestones
                if elapsed >= MIN_MILESTONES_HOURS * SECONDS_PER_HOUR:
                    return "milestones"

    if not math.isfinite(missions_every_hours) or missions_every_hours <= 0:
        # A zero or non-finite window would arm a walk on every frame.
        return None
    if state.last_missions is None:
        return "missions"
    if not math.isfinite(state.last_missions):
        return None
    elapsed = now - state.last_missions
    if elapsed < 0:
        # The clock went backwards, or a restored state is dated ahead. Not a
        # due claim; waiting is the only safe reading.
        return None
    if elapsed >= missions_every_hours * SECONDS_PER_HOUR:
        return "missions"
    return None
