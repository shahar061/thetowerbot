"""One Missions claim walk: Home -> Missions -> claim each -> Home.

A sibling of missions_visit, not a change to it: that walk is read-only and
its guarantee is worth keeping intact. What is shared is the part that touches
the device - account_collection.ControlTaps, the same one-action-per-step rule,
the same ambiguity-refusing locator and the same `at_missions_home` test - so
a second transaction cannot drift from the refusals the first is tested for.

Two properties make this walk safe in a way a general "tap the button" loop
would not be:

* A claimed card VANISHES and the list reflows up. Each pass therefore reads a
  different page, and the walk ends when no claimable card is left. Claiming
  the same mission twice is impossible because the card stops existing, not
  because a guard prevents it.
* The `completed N/35` counter moves the instant a reward is taken. That is a
  success test the walk can actually check, so a tap that changed nothing is
  REPORTED rather than repeated.

Both were measured, one tap apart, on menu_missions_claimable_no_status_bar ->
menu_missions_claimed: the card gone, `completed` 0/35 -> 1/35, the band 8/8 ->
7/8, coins 6.06K -> 6.08K and gems 60 -> 63.

The scan loop drives it and the runner owns the single instance, so a walk
outlives no bot: a restart cancels it, and so does pausing mid-walk.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum, auto
import threading
import time
from typing import TYPE_CHECKING, Any

import config
import events
from account_collection import (
    CollectionAction, CollectionResult, ControlTaps, MATCH_THRESHOLD,
    STEP_FRAME_BUDGET, at_missions_home,
)
from account_screens import ControlTarget
from device import Image
from missions_screen import ClaimTarget, MissionsReadings
from mail_claim import MailClaim

if TYPE_CHECKING:
    from strategy import Strategy

ClaimAction = CollectionAction
ClaimResult = CollectionResult

MISSIONS_TEMPLATE = config.NAV_TARGETS['MISSIONS']
RETURN_TEMPLATE = config.NAV_TARGETS['MISSIONS_RETURN']

MISSIONS_SCREEN = 'missions.daily'

# The page offers at most "8/8 Missions", so a walk that takes more than eight
# rewards is not reading reflow correctly. The bound turns that into a stop
# rather than a loop.
MAX_CLAIMS_PER_WALK = 8

TARGET = 'missions'


@dataclass(frozen=True)
class _PendingClaim:
    """What a tapped CLAIM button offered, kept only until the counter
    confirms or refutes it.

    Deliberately not the `ClaimTarget` it was tapped from: that type's own
    docstring says its `rect` "IS a tap target ... only ever valid for the
    frame it was read from," and `_pending` is carried one frame past that
    read. Holding only the fields this walk actually reports - never `rect` -
    means a coordinate cannot leak through here by construction, not by
    remembering not to read `.rect` off it.
    """

    raw_text: str
    mission_id: str | None
    coins: int | None
    gems: int | None


class Step(Enum):
    IDLE = auto()
    OPEN_MISSIONS = auto()
    CLAIM = auto()
    CONFIRM_HOME = auto()


class MissionsClaim(ControlTaps):
    """At most one Missions claim walk, driven one frame at a time."""

    def __init__(self, *, threshold: float = MATCH_THRESHOLD,
                 frame_budget: int = STEP_FRAME_BUDGET,
                 max_claims: int = MAX_CLAIMS_PER_WALK) -> None:
        self._lock = threading.RLock()
        self._threshold = threshold
        self._budget = frame_budget
        self._max_claims = max_claims
        self._step = Step.IDLE
        self._waited = 0
        self._requested_at: float | None = None
        self._result: ClaimResult | None = None
        self._trail: list[str] = []
        self._tuning: Strategy | None = None
        self._bus: Any = None
        self._claimed = 0
        self._announced = False
        # The claim awaiting proof: what was tapped, what it offered, and
        # what the counter read before it. Never a coordinate: `_PendingClaim`
        # has no `rect` field to hold one, so this is true by construction
        # rather than by remembering not to read one off it.
        self._pending: tuple[_PendingClaim, int] | None = None
        self._mail = MailClaim(frame_budget=frame_budget)
        self._mail_mode = False

    # -- reporting ---------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return self._step is not Step.IDLE or self._mail.active

    def snapshot(self) -> dict[str, Any]:
        """Detached. 'idle' is the absence of a walk, not a failed one.

        Carries no coordinate: a tap is located again on the frame it is made
        from, so a remembered point here could only ever be used wrongly.
        """
        with self._lock:
            if self._mail_mode:
                return self._mail.snapshot()
            status = 'running' if self._step is not Step.IDLE else (
                self._result.status if self._result is not None else 'idle')
            return {'status': status, 'step': self._step.name.lower(),
                    'requested_at': self._requested_at, 'claimed': self._claimed,
                    'trail': list(self._trail),
                    'result': asdict(self._result) if self._result is not None else None}

    # -- lifecycle ---------------------------------------------------------
    def request_mail(self, now: float | None = None) -> bool:
        """Mail shares this transaction's existing pause/recovery/navigation guards."""
        with self._lock:
            if self.active:
                return False
            self._mail_mode = True
            return self._mail.request(now)

    def request(self, now: float | None = None) -> bool:
        """Arm a walk. False when one is already running; never queues."""
        with self._lock:
            if self.active:
                return False
            self._mail_mode = False
            self._requested_at = time.time() if now is None else now
            self._result = None
            self._waited = 0
            self._claimed = 0
            self._announced = False
            self._pending = None
            self._trail = []
            self._enter(Step.OPEN_MISSIONS)
            return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        """End a walk the loop can no longer honour. Idempotent."""
        with self._lock:
            if self._mail_mode:
                self._mail.cancel(reason, detail, now)
                return
            if self._step is Step.IDLE:
                return
            self._finish('failed', reason, detail, time.time() if now is None else now)

    # -- one step ----------------------------------------------------------
    def advance(self, *, screen: Image, device: Any, templates: Any,
                readings: Any, missions: MissionsReadings, bus: Any,
                state: str, now: float | None = None,
                tuning: Strategy | None = None) -> ClaimAction | None:
        """One frame of the walk. Returns the tap it issued, if any."""
        with self._lock:
            if self._mail_mode:
                return self._mail.advance(screen=screen, device=device, templates=templates,
                                          readings=readings, state=state, bus=bus,
                                          now=now, tuning=tuning)
            self._tuning = tuning
            self._bus = bus
            if self._step is Step.IDLE:
                return None
            moment = time.time() if now is None else now
            if not self._announced:
                self._publish(events.ClaimStarted(target=TARGET))
                self._announced = True
            evidence = missions.claim_evidence()

            if self._step is Step.OPEN_MISSIONS:
                if not at_missions_home(state, readings.current_evidence(), evidence):
                    return self._wait('home_not_confirmed', 'The main menu was not confirmed '
                                      'on this frame, so no control was tapped.', moment)
                return self._tap(screen, device, templates, MISSIONS_TEMPLATE,
                                 'missions_control', Step.CLAIM, moment)

            if self._step is Step.CLAIM:
                return self._claim_step(screen, device, templates, evidence, moment)

            if not at_missions_home(state, readings.current_evidence(), evidence):
                return self._wait('home_not_restored', 'The missions page was walked, but the '
                                  'main menu was not confirmed again.', moment)
            return self._finish('completed', 'claimed', f'{self._claimed} mission reward(s) '
                                'were claimed and the game returned to the main menu.', moment)

    # -- the claiming itself -----------------------------------------------
    def _claim_step(self, screen: Image, device: Any, templates: Any,
                    evidence: dict[str, Any], moment: float) -> ClaimAction | None:
        if evidence['error'] is not None:
            return self._refuse('missions_unreadable', 'The missions page was reached but '
                                'could not be read; nothing was claimed.', moment)
        if evidence['screen_id'] != MISSIONS_SCREEN:
            return self._wait('missions_not_reached', 'The missions page was not observed '
                              'after the missions control was tapped.', moment)

        completed = evidence['completed']
        if completed is None:
            # The counter IS the success test. Without it a claim cannot be
            # verified, and an unverifiable claim is worse than none: nothing
            # afterwards could say whether the reward was taken.
            return self._refuse('counter_unreadable', 'The completed counter could not be '
                                'read, so no claim could be verified; nothing was tapped.',
                                moment)

        if self._pending is not None:
            claimed, before = self._pending
            if completed <= before:
                return self._wait_for_claim_confirmation(claimed, moment)
            self._pending = None
            self._waited = 0
            self._claimed += 1
            self._publish(events.MissionClaimed(
                mission=claimed.raw_text, mission_id=claimed.mission_id,
                coins=claimed.coins, gems=claimed.gems,
                completed_before=before, completed_after=completed))

        claims = evidence['claims']
        if not claims or self._claimed >= self._max_claims:
            return self._tap(screen, device, templates, RETURN_TEMPLATE,
                             'return_control', Step.CONFIRM_HOME, moment)

        target = claims[0]
        self._pending = (_PendingClaim(target.raw_text, target.mission_id,
                                       target.coins, target.gems), completed)
        return self._tap_point(target, device, moment)

    def _wait_for_claim_confirmation(self, claimed: _PendingClaim,
                                     moment: float) -> ClaimAction | None:
        """Wait, budget-bound, for the counter to confirm a tapped claim.

        Every other `_wait` caller in this walk is pre-tap: nothing was taken,
        so `_wait`'s bare ClaimEnded on exhaustion is the whole story. Here a
        CLAIM button was already tapped, and `claimed.raw_text` is the only
        record of which mission that was - if the tap landed and only the
        reflow or OCR lagged, a reward was taken and ClaimEnded alone names
        nothing. Route the timeout through `_uncertain` instead of `_wait`, so
        the one genuinely ambiguous outcome publishes a ClaimUncertain that
        names the mission before the walk ends, rather than the ClaimSkipped
        that would assert nothing moved.
        """
        self._waited += 1
        if self._waited > self._budget:
            return self._uncertain('claim_not_confirmed', f'"{claimed.raw_text}" was tapped '
                                   'but the completed counter did not move before the wait '
                                   'budget ran out; it may or may not have been claimed.', moment)
        return None

    def _tap_point(self, target: ClaimTarget, device: Any, moment: float) -> ClaimAction | None:
        """Tap a CLAIM button located on THIS frame.

        Not `_tap`: that locates a control by template match, and a CLAIM
        button is found by the reader instead - there is one per claimable
        card and a template would match all of them equally.
        """
        x, y = target.rect[0] + target.rect[2] // 2, target.rect[1] + target.rect[3] // 2
        return self._tap_target(
            ControlTarget(name='claim_button', point=(x, y), status='located',
                          score=1., rect=target.rect),
            device, 'claim_button', Step.CLAIM, moment)

    # -- internals ---------------------------------------------------------
    def _publish(self, event: events.Event) -> None:
        if self._bus is not None:
            self._bus.publish(event)

    def _refuse(self, reason: str, detail: str, moment: float) -> ClaimAction | None:
        self._publish(events.ClaimSkipped(target=TARGET, reason=reason, detail=detail))
        return self._finish('failed', reason, detail, moment)

    def _uncertain(self, reason: str, detail: str, moment: float) -> ClaimAction | None:
        """End a walk that TAPPED a claim it could not confirm.

        Not `_refuse`: ClaimSkipped means the walk declined to act and so
        provably moved nothing. A CLAIM was tapped here, and the reward may
        have been taken - a different fact, and one the ledger encodes as
        delta=None rather than delta=0.
        """
        self._publish(events.ClaimUncertain(target=TARGET, reason=reason, detail=detail))
        return self._finish('failed', reason, detail, moment)

    def _enter(self, step: Step) -> None:
        self._step = step
        self._waited = 0
        self._trail.append(step.name.lower())

    def _wait(self, reason: str, detail: str, moment: float) -> ClaimAction | None:
        self._waited += 1
        if self._waited > self._budget:
            return self._finish('failed', reason, detail, moment)
        return None

    def _finish(self, status: str, reason: str, detail: str,
                moment: float) -> ClaimAction | None:
        self._result = ClaimResult(status, reason, detail, MISSIONS_SCREEN, moment)
        self._publish(events.ClaimEnded(
            target=TARGET, claimed=self._claimed, reason=reason,
            aborted=status != 'completed'))
        self._step = Step.IDLE
        self._waited = 0
        self._pending = None
        return None
