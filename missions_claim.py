"""One Missions claim walk: Home -> Missions -> claim each -> Home.

A sibling of missions_visit, not a change to it: that walk is read-only and
its guarantee is worth keeping intact. What is shared is the part that touches
the device - account_collection.ControlTaps, the same one-action-per-step rule,
the same ambiguity-refusing locator and the same `at_missions_home` test - so
a second transaction cannot drift from the refusals the first is tested for.

Two properties make this walk safe in a way a general "tap the button" loop
would not be:

* A claimed card VANISHES and the list reflows up. Each pass therefore reads a
  different page. Once visible cards have no CLAIM buttons, the walk scrolls
  through the remaining cards with a fixed bound and stops on an unchanged
  list. Claiming the same mission twice is impossible because the card stops
  existing, not because a guard prevents it.
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
from missions_screen import (ClaimTarget, MissionsReadings,
                             WEEKLY_REWARD_SCREEN, WeeklyRewardModal)
from events_claim import EventsClaim
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
MAX_MISSIONS_SCROLLS = 4
MAX_WEEKLY_CHESTS_PER_WALK = 7
MAX_WEEKLY_STRIP_SCROLLS = 2

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
    completed_target: int | None


@dataclass
class _PendingChest:
    """A tapped chest, with reward steps observed before final confirmation."""

    threshold: int
    rewards: list[tuple[str, int]]
    reward_texts: list[str]
    unreadable_rewards: int = 0
    index: int = 0
    total: int | None = None
    final_tapped: bool = False


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
        self._pending_chest: _PendingChest | None = None
        self._chests_claimed = 0
        self._chest_scrolls = 0
        self._chest_seek_failed = False
        self._mail = MailClaim(frame_budget=frame_budget)
        self._events = EventsClaim(frame_budget=frame_budget)
        # The side walk this transaction is standing in for, if any: mail and
        # events share every pause/recovery/navigation guard held on
        # `.active`, without each needing a guard of its own.
        self._side: MailClaim | EventsClaim | None = None
        self._scrolls = 0
        self._scroll_pending: tuple[Any, ...] | None = None
        self._scroll_seen: set[tuple[Any, ...]] = set()

    # -- reporting ---------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return (self._step is not Step.IDLE or self._mail.active
                    or self._events.active)

    @property
    def chest_pending(self) -> bool:
        with self._lock:
            return self._pending_chest is not None

    def snapshot(self) -> dict[str, Any]:
        """Detached. 'idle' is the absence of a walk, not a failed one.

        Carries no coordinate: a tap is located again on the frame it is made
        from, so a remembered point here could only ever be used wrongly.
        """
        with self._lock:
            if self._side is not None:
                return self._side.snapshot()
            status = 'running' if self._step is not Step.IDLE else (
                self._result.status if self._result is not None else 'idle')
            return {'status': status, 'step': self._step.name.lower(),
                    'requested_at': self._requested_at, 'claimed': self._claimed,
                    'chests_claimed': self._chests_claimed,
                    'trail': list(self._trail),
                    'result': asdict(self._result) if self._result is not None else None}

    # -- lifecycle ---------------------------------------------------------
    def request_mail(self, now: float | None = None) -> bool:
        """Mail shares this transaction's existing pause/recovery/navigation guards."""
        with self._lock:
            if self.active:
                return False
            self._side = self._mail
            return self._mail.request(now)

    def request_events(self, now: float | None = None) -> bool:
        """Events shares the same guards; see request_mail."""
        with self._lock:
            if self.active:
                return False
            self._side = self._events
            return self._events.request(now)

    def request(self, now: float | None = None) -> bool:
        """Arm a walk. False when one is already running; never queues."""
        with self._lock:
            if self.active:
                return False
            self._side = None
            self._requested_at = time.time() if now is None else now
            self._result = None
            self._waited = 0
            self._claimed = 0
            self._announced = False
            self._pending = None
            self._pending_chest = None
            self._chests_claimed = 0
            self._chest_scrolls = 0
            self._chest_seek_failed = False
            self._scrolls = 0
            self._scroll_pending = None
            self._scroll_seen.clear()
            self._trail = []
            self._enter(Step.OPEN_MISSIONS)
            return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        """End a walk the loop can no longer honour. Idempotent."""
        with self._lock:
            if self._side is not None:
                self._side.cancel(reason, detail, now)
                return
            if self._step is Step.IDLE:
                return
            moment = time.time() if now is None else now
            if self._pending is not None or self._pending_chest is not None:
                self._uncertain(reason, detail, moment)
            else:
                self._finish('failed', reason, detail, moment)

    # -- one step ----------------------------------------------------------
    def advance(self, *, screen: Image, device: Any, templates: Any,
                readings: Any, missions: MissionsReadings, bus: Any,
                state: str, now: float | None = None,
                tuning: Strategy | None = None) -> ClaimAction | None:
        """One frame of the walk. Returns the tap it issued, if any."""
        with self._lock:
            if self._side is not None:
                return self._side.advance(screen=screen, device=device, templates=templates,
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
            return self._finish(
                'completed', 'claimed',
                f'{self._claimed} mission reward(s) and {self._chests_claimed} '
                'weekly chest(s) were claimed; the game returned to the main menu.',
                moment)

    # -- the claiming itself -----------------------------------------------
    def _claim_step(self, screen: Image, device: Any, templates: Any,
                    evidence: dict[str, Any], moment: float) -> ClaimAction | None:
        if self._pending_chest is not None:
            return self._chest_step(device, evidence, moment)
        if evidence['error'] is not None:
            if self._pending is not None:
                return self._uncertain('missions_unreadable', 'The missions page became unreadable '
                                       'after a reward tap; its outcome is unresolved.', moment)
            return self._refuse('missions_unreadable', 'The missions page was reached but '
                                'could not be read; nothing was claimed.', moment)
        if evidence['screen_id'] != MISSIONS_SCREEN:
            if self._pending is not None and self._waited >= self._budget:
                return self._uncertain('missions_not_reached', 'The missions page was lost '
                                       'after a reward tap; its outcome is unresolved.', moment)
            return self._wait('missions_not_reached', 'The missions page was not observed '
                              'after the missions control was tapped.', moment)

        completed = evidence['completed']
        if completed is None:
            if self._pending is not None:
                return self._uncertain('counter_unreadable', 'The counter became unreadable '
                                       'after a reward tap; its outcome is unresolved.', moment)
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
            if (completed != before + 1
                    or claimed.completed_target is not None
                    and evidence.get('completed_target') != claimed.completed_target
                    or any(isinstance(item, (list, tuple)) and len(item) >= 2
                           and item[0] == claimed.mission_id and item[1] == claimed.raw_text
                           for item in evidence.get('visible', ()))):
                return self._uncertain('claim_evidence_mismatch', 'The counter or mission '
                                       'cards changed ambiguously after the reward tap.', moment)
            self._publish(events.MissionClaimed(
                mission=claimed.raw_text, mission_id=claimed.mission_id,
                coins=claimed.coins, gems=claimed.gems,
                completed_before=before, completed_after=completed))
            # Publication through MissionReceiptBus fsyncs the receipt. If
            # that fails, keep _pending and retry on the next scan; no second
            # reward button can be reached before this commit succeeds.
            self._pending = None
            self._waited = 0
            self._claimed += 1

        visible = tuple(evidence.get('visible', ()))
        if self._scroll_pending is not None:
            if visible == self._scroll_pending:
                self._waited += 1
                if self._waited <= self._budget:
                    return None
                # A dropped gesture and the end of the list look alike. In
                # either case, another blind swipe would only repeat it.
                return self._return_home(screen, device, templates, moment)
            self._scroll_pending = None
            self._waited = 0

        chests = evidence.get('chests', ())
        if self._chests_claimed < MAX_WEEKLY_CHESTS_PER_WALK and chests:
            target = chests[0]
            x = target.rect[0] + target.rect[2] // 2
            y = target.rect[1] + target.rect[3] // 2
            prepare_chest = getattr(self._bus, 'prepare_chest', None)
            if prepare_chest is not None:
                prepare_chest(threshold=target.threshold, completed_before=completed,
                              now=moment)
            action = self._tap_target(
                ControlTarget(name='weekly_chest', point=(x, y),
                              status='located', score=1., rect=target.rect),
                device, 'weekly_chest', Step.CLAIM, moment)
            if action is None:
                discard = getattr(self._bus, 'discard_prepared_claim', None)
                if discard is not None:
                    discard(moment)
                return self._refuse('chest_target_unusable', 'The weekly chest could not '
                                    'be tapped at its verified position.', moment)
            self._pending_chest = _PendingChest(target.threshold, [], [])
            return action

        claims = evidence['claims']
        if claims and self._claimed < self._max_claims:
            target = claims[0]
            prepare = getattr(self._bus, 'prepare_claim', None)
            if prepare is not None:
                prepare(mission=target.raw_text, mission_id=target.mission_id,
                        coins=target.coins, gems=target.gems,
                        completed_before=completed,
                        completed_target=evidence.get('completed_target'),
                        visible_before=list(evidence.get('visible', ())), now=moment)
            self._pending = (_PendingClaim(target.raw_text, target.mission_id,
                                           target.coins, target.gems,
                                           evidence.get('completed_target')), completed)
            action = self._tap_point(target, device, moment)
            if action is None:
                discard = getattr(self._bus, 'discard_prepared_claim', None)
                if discard is not None:
                    discard(moment)
                self._pending = None
            return action

        # The recorded strip initially shows 5..25; a horizontal gesture
        # reveals 30 and 35. Only seek it when an eligible threshold is
        # absent from this frame, and never tap a box inferred from a count.
        seen = {threshold for threshold, _ in evidence.get('milestones', ())}
        unseen_eligible = [threshold for threshold in (30, 35)
                           if completed >= threshold and threshold not in seen]
        if unseen_eligible:
            if self._chest_scrolls >= MAX_WEEKLY_STRIP_SCROLLS:
                if not self._chest_seek_failed:
                    self._publish(events.ClaimSkipped(
                        target='weekly_chest', reason='chest_strip_not_reached',
                        detail=f'The {unseen_eligible[0]}-mission chest was eligible '
                               'but its position was not visible after the weekly strip '
                               'was swiped.'))
                    self._chest_seek_failed = True
            else:
                height, width = screen.shape[:2]
                x, x2, y = int(width * .81), int(width * .17), int(height * .16)
                device.swipe(x, y, x2, y, .4)
                self._chest_scrolls += 1
                self._waited = 0
                return ClaimAction('claim', 'weekly_chest_scroll', x, y, 1.,
                                   (x2, y, 1, x - x2))

        if self._claimed >= self._max_claims:
            return self._return_home(screen, device, templates, moment)
        if not claims:
            if (visible and visible not in self._scroll_seen
                    and self._scrolls < MAX_MISSIONS_SCROLLS):
                height, width = screen.shape[:2]
                x = width // 2
                y, y2 = int(height * .78), int(height * .43)
                device.swipe(x, y, x, y2, .35)
                self._scrolls += 1
                self._scroll_pending = visible
                self._scroll_seen.add(visible)
                self._waited = 0
                return ClaimAction('claim', 'missions_scroll', x, y, 1.,
                                   (x, y2, 1, y - y2))
            return self._return_home(screen, device, templates, moment)

        return None

    def _chest_step(self, device: Any, evidence: dict[str, Any],
                    moment: float) -> ClaimAction | None:
        pending = self._pending_chest
        assert pending is not None
        if evidence.get('error') is not None:
            return self._uncertain('chest_unreadable', f'The {pending.threshold}-mission '
                                   'chest was tapped, but the next screen was unreadable.', moment)

        if evidence.get('screen_id') == WEEKLY_REWARD_SCREEN:
            modal: WeeklyRewardModal | None = evidence.get('reward_modal')
            if modal is None:
                return self._wait_for_chest(pending, moment)
            if modal.index == pending.index:
                return self._wait_for_chest(pending, moment)
            if (pending.final_tapped or modal.index != pending.index + 1
                    or pending.total is not None and modal.total != pending.total):
                return self._uncertain('chest_reward_sequence_changed',
                                       'The weekly chest reward pages changed order.', moment)
            rect = modal.control
            x, y = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
            observe_reward = getattr(self._bus, 'observe_chest_reward', None)
            if observe_reward is not None:
                observe_reward(index=modal.index, total=modal.total,
                               currency=modal.currency, amount=modal.amount,
                               reward_text=modal.reward_text,
                               final_tapped=modal.action == 'claim', now=moment)
            action = self._tap_target(
                ControlTarget(name=f'weekly_chest_{modal.action}', point=(x, y),
                              status='located', score=1., rect=rect),
                device, f'weekly_chest_{modal.action}', Step.CLAIM, moment)
            if action is None:
                return self._uncertain('chest_reward_control_unusable',
                                       'A weekly chest reward control could not be tapped.', moment)
            pending.index = modal.index
            pending.total = modal.total
            if modal.reward_text is not None:
                pending.reward_texts.append(modal.reward_text)
            if modal.currency is not None and modal.amount is not None:
                pending.rewards.append((modal.currency, modal.amount))
            else:
                pending.unreadable_rewards += 1
            pending.final_tapped = modal.action == 'claim'
            self._waited = 0
            return action

        if evidence.get('screen_id') == MISSIONS_SCREEN:
            states = dict(evidence.get('milestones', ()))
            if pending.final_tapped and states.get(pending.threshold) == 'claimed':
                self._publish(events.WeeklyChestClaimed(
                    threshold=pending.threshold, rewards=tuple(pending.rewards),
                    unreadable_rewards=pending.unreadable_rewards,
                    reward_text=', '.join(pending.reward_texts) or None,
                    confirmation='chest_marked_claimed'))
                self._pending_chest = None
                self._chests_claimed += 1
                self._waited = 0
                return None
            if states.get(pending.threshold) == 'claimed' and not pending.final_tapped:
                return self._uncertain('chest_claimed_early', 'The weekly chest changed '
                                       'before its reward ceremony was completed.', moment)
        return self._wait_for_chest(pending, moment)

    def _wait_for_chest(self, pending: _PendingChest,
                        moment: float) -> ClaimAction | None:
        self._waited += 1
        if self._waited > self._budget:
            return self._uncertain('chest_not_confirmed',
                                   f'The {pending.threshold}-mission chest was tapped '
                                   'but its reward and green check were not confirmed.', moment)
        return None

    def _return_home(self, screen: Image, device: Any, templates: Any,
                     moment: float) -> ClaimAction | None:
        return self._tap(screen, device, templates, RETURN_TEMPLATE,
                         'return_control', Step.CONFIRM_HOME, moment)

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
            target=TARGET, claimed=self._claimed + self._chests_claimed, reason=reason,
            aborted=status != 'completed'))
        self._step = Step.IDLE
        self._waited = 0
        self._pending = None
        self._pending_chest = None
        self._scroll_pending = None
        return None
