"""One MILESTONES claim walk: Home -> Milestones -> Claim All, repeated -> Home.

A sibling of missions_claim, not a change to it: two independent claim walks
share the device-touching machinery - account_collection.ControlTaps, the
same one-action-per-step rule, the same ambiguity-refusing locator - so
neither can drift from the refusals the other is tested for. What differs is
shaped by the page itself, not by choice:

* The missions page lists several claimable cards; a claimed card VANISHES
  and the list reflows, so the walk there loops over `claims` until none are
  left. The MILESTONES ladder offers ONE control - `Claim All` - and it is
  read from the frame by OCR text, not matched by any template: the reward
  slots' three states (padlock / glow / green check) are told apart by icon
  art this codebase has recorded no bounds for, and `Claim All`'s own exact
  text match does not need them.
* The missions page carries a `completed N/35` counter that moves the
  instant a reward is taken - a success test the walk can actually check.
  MILESTONES has no such counter. What proves a claim landed is the
  ceremony's own transition: tapping `Claim All` opens a full-screen reward
  modal naming exactly one reward in words ("25 COINS"), and tapping that
  modal's CLAIM closes it back onto the ladder. THE MODAL -> LADDER
  TRANSITION IS THE WHOLE SUCCESS TEST, and it is weaker than the missions
  counter: coins on this page are read abbreviated (6.08K -> 6.11K, useless
  as a moved-or-not signal) and gems move only for gem rewards. Stated
  plainly here rather than implied by a check this walk cannot make.
* The terminal condition is `Claim All` being ABSENT from the ladder - NEVER
  "no reward is still glowing". The premium `50 COINS` at wave 10 glows
  before and after a claim on both recorded ladder captures, because it sits
  behind Premium Pass 1 and this walk can never take it. A loop keyed on a
  visible reward would never end; a loop keyed on `Claim All` ends the
  instant the page has nothing left this account can actually take.

`Claim All` disappearing is the terminal condition, not the only stop: a
misread ladder that keeps re-offering it would otherwise cycle
ladder -> modal -> ladder forever, since a confirmed claim resets the wait
budget rather than exhausting it. `MAX_CLAIMS_PER_WALK` exists as a backstop
against exactly that failure mode - see its own comment for why it is a
safety number, not a measured one.

`nav/skip.png` also matches the reward modal at 1.0000, and this walk never
taps it: only CLAIM was ever exercised on a real device, and whether SKIP
claims the remaining rewards or discards them is unknown. Tapping an
unverified control on a page that spends real rewards is not a risk worth
taking to save one frame.

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
    STEP_FRAME_BUDGET, at_page_home,
)
from account_screens import ControlTarget
from device import Image
from milestones_screen import LADDER_SCREEN, MODAL_SCREEN, MilestonesReadings

if TYPE_CHECKING:
    from strategy import Strategy

ClaimAction = CollectionAction
ClaimResult = CollectionResult

MILESTONES_TEMPLATE = config.NAV_TARGETS['MILESTONES']
RETURN_TEMPLATE = config.NAV_TARGETS['MILESTONES_RETURN']

# The reward modal's own CLAIM button. Not a NAV_TARGETS entry: it is not a
# main-menu navigation control, it is the ceremony's own confirm button, the
# same way account_collection.CLOSE_TEMPLATE is a Settings-panel control kept
# local to the module that taps it. Measured on the reward modal capture at
# (540, 1866), score 1.0000. `nav/skip.png` matches the same modal equally
# well and is deliberately never used here - see this module's docstring.
CLAIM_TEMPLATE = 'nav/claim_reward.png'

# Imported, never re-declared: the reader that PRODUCES these ids owns them.
# Two copies would let the reader be renamed while this walk went on
# comparing against the old strings - every ladder frame silently becoming
# `ladder_not_reached` and every modal `modal_not_reached`, with nothing on
# the reader's side failing.

# A RUNAWAY BACKSTOP, not a measured capacity - unlike missions'
# MAX_CLAIMS_PER_WALK, which the page's own "8/8" caps for real. Nothing here
# measures a ceiling on how many tiers this ladder can offer at once: the
# recorded captures show TEN visible reward slots (five rows across two
# tracks) on menu_milestones_claimable.png, and this walk neither scrolls the
# ladder nor changes tier, so nothing it ever actually reads comes anywhere
# close to twenty. Twenty exists solely so a ladder that keeps re-offering
# `Claim All` - a misread of the reflow, confirming a claim resets the wait
# budget rather than exhausting it - cannot cycle this walk forever; it is
# not a claim that twenty rewards were ever seen on one ladder.
MAX_CLAIMS_PER_WALK = 20

TARGET = 'milestones'


@dataclass(frozen=True)
class _PendingClaim:
    """What the reward modal named, kept only until the ladder reappears to
    confirm it.

    `tier` is carried separately from the modal reading that produced the
    rest of this record: the modal's own `MilestonesReading.tier` is always
    None (the ceremony draws no tier caption), so the tier this claim belongs
    to has to be the one read from the LADDER just before `Claim All` was
    tapped. Nothing here is a coordinate - the modal's CLAIM button is a
    template tap (`CLAIM_TEMPLATE`), never a reader-provided rect, so unlike
    missions_claim._PendingClaim there was never a rect to carry across a
    frame in the first place. This type still omits one by construction
    rather than by convention, for the identical reason that type states.
    """

    reward_text: str | None
    currency: str | None
    amount: int | None
    tier: int | None
    modal_index: int | None = None
    modal_total: int | None = None


class Step(Enum):
    IDLE = auto()
    OPEN_MILESTONES = auto()
    LADDER = auto()
    MODAL = auto()
    CONFIRM_HOME = auto()


class MilestonesClaim(ControlTaps):
    """At most one MILESTONES claim walk, driven one frame at a time."""

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
        # Set only when the RETURN tap fires because the bound was reached
        # rather than because `Claim All` genuinely ran out - so the final
        # reason can tell the two apart. Reaching the bound is the walk doing
        # its job, not a failure: this never affects `status` or `aborted`.
        self._bound_reached = False
        # The tier read from the LADDER right before `Claim All` was tapped -
        # a plain int, held only until the modal names the reward it belongs
        # with. Not part of `_pending`: nothing is "pending" confirmation
        # until CLAIM is actually tapped in the modal.
        self._tier_at_claim: int | None = None
        # The claim awaiting proof: what the modal named, and which tier it
        # was for. Never a coordinate: `_PendingClaim` has no field that
        # could hold one, so this is true by construction rather than by
        # remembering not to read one off it.
        self._pending: _PendingClaim | None = None

    # -- reporting ---------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        """Detached. 'idle' is the absence of a walk, not a failed one.

        Carries no coordinate: a tap is located again on the frame it is made
        from, so a remembered point here could only ever be used wrongly.
        """
        with self._lock:
            status = 'running' if self._step is not Step.IDLE else (
                self._result.status if self._result is not None else 'idle')
            return {'status': status, 'step': self._step.name.lower(),
                    'requested_at': self._requested_at, 'claimed': self._claimed,
                    'trail': list(self._trail),
                    'result': asdict(self._result) if self._result is not None else None}

    # -- lifecycle ---------------------------------------------------------
    def request(self, now: float | None = None) -> bool:
        """Arm a walk. False when one is already running; never queues."""
        with self._lock:
            if self._step is not Step.IDLE:
                return False
            self._requested_at = time.time() if now is None else now
            self._result = None
            self._waited = 0
            self._claimed = 0
            self._announced = False
            self._bound_reached = False
            self._tier_at_claim = None
            self._pending = None
            self._trail = []
            self._enter(Step.OPEN_MILESTONES)
            return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        """End a walk the loop can no longer honour. Idempotent."""
        with self._lock:
            if self._step is Step.IDLE:
                return
            self._finish('failed', reason, detail, time.time() if now is None else now)

    # -- one step ----------------------------------------------------------
    def advance(self, *, screen: Image, device: Any, templates: Any,
                readings: Any, milestones: MilestonesReadings, bus: Any,
                state: str, now: float | None = None,
                tuning: Strategy | None = None) -> ClaimAction | None:
        """One frame of the walk. Returns the tap it issued, if any."""
        with self._lock:
            self._tuning = tuning
            self._bus = bus
            if self._step is Step.IDLE:
                return None
            moment = time.time() if now is None else now
            if not self._announced:
                self._publish(events.ClaimStarted(target=TARGET))
                self._announced = True
            evidence = milestones.claim_evidence()

            if self._step is Step.OPEN_MILESTONES:
                if not at_page_home(state, readings.current_evidence(), evidence):
                    return self._wait('home_not_confirmed', 'The main menu was not confirmed '
                                      'on this frame, so no control was tapped.', moment)
                return self._tap(screen, device, templates, MILESTONES_TEMPLATE,
                                 'milestones_control', Step.LADDER, moment)

            if self._step is Step.LADDER:
                return self._ladder_step(screen, device, templates, evidence, moment)

            if self._step is Step.MODAL:
                return self._modal_step(screen, device, templates, evidence, moment)

            if not at_page_home(state, readings.current_evidence(), evidence):
                return self._wait('home_not_restored', 'The milestones ladder was walked, but '
                                  'the main menu was not confirmed again.', moment)
            if self._bound_reached:
                # A backstop ending, not a failure: `status` is still
                # 'completed' and ClaimEnded still publishes `aborted=False`
                # below, the same as an ordinary end. Only `reason` differs,
                # so a caller can tell "the bound stopped a walk that would
                # otherwise still be claiming" apart from "Claim All ran out".
                return self._finish('completed', 'claim_bound_reached',
                                    f'{self._claimed} milestone reward(s) were claimed - the walk '
                                    f'stopped at its {self._max_claims}-claim safety backstop with '
                                    'Claim All still offering more - and the game returned to the '
                                    'main menu.', moment)
            return self._finish('completed', 'claimed', f'{self._claimed} milestone reward(s) '
                                'were claimed and the game returned to the main menu.', moment)

    # -- the claiming itself -----------------------------------------------
    def _ladder_step(self, screen: Image, device: Any, templates: Any,
                     evidence: dict[str, Any], moment: float) -> ClaimAction | None:
        """Confirm a claim tapped last step (if one is pending), then decide
        the ladder's next action.

        Two screens can appear here because CLAIM was tapped from the modal
        on the previous step: this frame's evidence may still show the modal
        (the panel takes a beat to animate closed) or the ladder underneath
        it (confirmed). Because a CLAIM was already tapped whenever `_pending`
        is set, a wait that runs out here must name the claim as uncertain -
        `_wait_for_claim_confirmation`, never the plain `_wait` a fresh,
        nothing-tapped-yet decision uses below.
        """
        if self._pending is not None:
            confirmed = evidence['error'] is None and evidence['screen_id'] == LADDER_SCREEN
            if not confirmed:
                return self._wait_for_claim_confirmation(self._pending, moment)
            pending = self._pending
            self._pending = None
            self._waited = 0
            self._claimed += 1
            self._publish(events.MilestoneClaimed(
                reward_text=pending.reward_text, currency=pending.currency,
                amount=pending.amount, tier=pending.tier))
        else:
            if evidence['error'] is not None:
                return self._refuse('ladder_unreadable', 'The milestones ladder was reached '
                                    'but could not be read; nothing was claimed.', moment)
            if evidence['screen_id'] != LADDER_SCREEN:
                return self._wait('ladder_not_reached', 'The milestones ladder was not observed '
                                  'after the milestones control was tapped.', moment)

        # Reached only with a clean ladder read: either a fresh one (the
        # `else` branch above returned otherwise) or the one that just
        # confirmed a claim, in which case THIS frame - not a stale one - is
        # what `claim_all` below is read from.
        claim_all = evidence['claim_all']
        if claim_all is None or self._claimed >= self._max_claims:
            # `claim_all is None` is the terminal condition proper - never
            # "no reward still glows", see this module's docstring for the
            # premium reward that always does. The claimed-count check is the
            # OTHER way home: a safety backstop, not a second terminal
            # condition this walk expects to hit for real - see
            # MAX_CLAIMS_PER_WALK's own comment. Recorded so the walk still
            # reports which one fired, since ending at the backstop is not a
            # failure but is a fact worth telling apart from a ladder that
            # genuinely ran out.
            if claim_all is not None:
                self._bound_reached = True
            return self._tap(screen, device, templates, RETURN_TEMPLATE,
                             'return_control', Step.CONFIRM_HOME, moment)

        # The tier this claim belongs to, read from the ladder BEFORE the tap
        # that opens the modal - the modal's own reading never carries one.
        self._tier_at_claim = evidence['tier']
        return self._tap_claim_all(claim_all, device, moment)

    def _modal_step(self, screen: Image, device: Any, templates: Any,
                    evidence: dict[str, Any], moment: float) -> ClaimAction | None:
        """Wait for the reward ceremony, then tap its CLAIM - never SKIP.

        `evidence['error']` is None even when the reward line names no
        currency ("Unlock Lab" is a real reward on the recorded ladder,
        parsed by milestones_screen.reward_of as (None, None)) - that is a
        successful, if uninformative, reading, not a failure. Only a genuine
        reader error refuses the tap here.
        """
        if self._pending is not None and (
                evidence['error'] is not None or evidence['screen_id'] != MODAL_SCREEN):
            return self._wait_for_claim_confirmation(self._pending, moment)
        if evidence['error'] is not None:
            return self._refuse('modal_unreadable', 'The reward ceremony was reached but '
                                'could not be read; nothing was tapped.', moment)
        if evidence['screen_id'] != MODAL_SCREEN:
            return self._wait('modal_not_reached', 'The reward ceremony was not observed after '
                              'Claim All was tapped.', moment)
        if self._pending is not None:
            pending = self._pending
            if (pending.modal_index is None
                    or evidence.get('modal_index') != pending.modal_index + 1
                    or evidence.get('modal_total') != pending.modal_total):
                return self._wait_for_claim_confirmation(pending, moment)
            self._pending = None
            self._waited = 0
            self._claimed += 1
            self._publish(events.MilestoneClaimed(
                reward_text=pending.reward_text, currency=pending.currency,
                amount=pending.amount, tier=pending.tier))

        pending = _PendingClaim(evidence['reward_text'], evidence['currency'],
                                evidence['amount'], self._tier_at_claim,
                                evidence.get('modal_index'), evidence.get('modal_total'))
        modal_action = evidence.get('modal_action')
        if modal_action == 'next':
            rect = evidence.get('modal_control')
            if rect is None or pending.modal_index is None or pending.modal_total is None:
                return self._refuse('next_unreadable',
                                    'The next reward control or progress was unreadable.', moment)
            x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
            action = self._tap_target(
                ControlTarget('next_button', (x, y), 'located', 1., rect),
                device, 'next_button', Step.MODAL, moment)
        elif modal_action in ('claim', None):
            # Legacy scripted evidence predates modal_action; the actual
            # reader always supplies it on a recognized reward ceremony.
            action = self._tap(screen, device, templates, CLAIM_TEMPLATE,
                               'claim_button', Step.LADDER, moment)
        else:
            return self._refuse('modal_action_unreadable',
                                'The reward ceremony action was not recognized.', moment)
        if action is not None:
            self._pending = pending
        return action

    def _wait_for_claim_confirmation(self, claimed: _PendingClaim,
                                     moment: float) -> ClaimAction | None:
        """Wait, budget-bound, for the ladder to reappear after CLAIM was tapped.

        Every other `_wait` caller in this walk is pre-tap: nothing was taken,
        so `_wait`'s bare ClaimEnded on exhaustion is the whole story. Here a
        CLAIM button was already tapped, and `claimed` is the only record of
        what that modal named - if the tap landed and only the transition
        lagged, a reward was taken and ClaimEnded alone names nothing. Route
        the timeout through `_uncertain` instead of `_wait`, so the one
        genuinely ambiguous outcome publishes a ClaimUncertain that names the
        reward before the walk ends, rather than the ClaimSkipped that would
        assert nothing moved.
        """
        self._waited += 1
        if self._waited > self._budget:
            label = claimed.reward_text if claimed.reward_text is not None else 'a milestone reward'
            return self._uncertain('claim_not_confirmed', f'"{label}" was tapped but the ladder '
                                   'did not reappear before the wait budget ran out; it may or '
                                   'may not have been claimed.', moment)
        return None

    def _tap_claim_all(self, rect: tuple[int, int, int, int], device: Any,
                       moment: float) -> ClaimAction | None:
        """Tap `Claim All`, located on THIS frame.

        Not `_tap`: that locates a control by template match, and `Claim All`
        is found by the reader instead - milestones_screen reads it from OCR
        text at an exact equality match, for the reason its own docstring
        gives (the reward slots' icon states are not bounded, but this
        button's text is exact and unique).
        """
        x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
        return self._tap_target(
            ControlTarget(name='claim_all_button', point=(x, y), status='located',
                          score=1., rect=rect),
            device, 'claim_all_button', Step.MODAL, moment)

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
        self._result = ClaimResult(status, reason, detail, LADDER_SCREEN, moment)
        self._publish(events.ClaimEnded(
            target=TARGET, claimed=self._claimed, reason=reason,
            aborted=status != 'completed'))
        self._step = Step.IDLE
        self._waited = 0
        self._pending = None
        self._tier_at_claim = None
        self._bound_reached = False
        return None
