"""One Events visit: Home -> Events -> claim each finished mission tier -> Home.

Claims medals only. It never taps the Event Shop, the Bots tab, the boost
offer or a relic: the one control it presses inside the page is a `Claim`
label on the Missions list, and a tap counts only once the next frame shows
the page changed (the label gone, or a tier counter moved).

Armed by the red dot on the Events icon, which the game lights while a
mission tier is claimable. The list outgrows the screen (two missions a day
for the first week) and reopens where it was last left, so a claim may be
above or below what the page opens on: the walk first scrolls up to the top,
then down to the end, claiming whatever passes. Measured live, a claimable
card twenty cards down was invisible on the opening frame.
"""
from __future__ import annotations

from dataclasses import asdict
from enum import Enum, auto
import time
from typing import Any

import events
import events_badge
import events_screen
from account_collection import CollectionAction, CollectionResult, ControlTaps, at_home
from device import Image

TARGET = 'events'
# Per direction. Measured: a full list of ~20 cards is three downward
# swipes; the bound leaves room for the second week's longer list.
MAX_SCROLLS = 12
MAX_VISIT_FRAMES = 120
# The info close and the Missions tab each change the page; one that does not
# after this many taps is not going to, so the visit leaves.
MAX_PAGE_TAPS = 3
# Measured list band on a 2400-tall capture: cards run from ~1260 down to the
# return band at 2270. A swipe from 2000 to 1300 stays inside it and never
# touches the relic strip above; kept as fractions for 1920-tall frames.
SCROLL_X, SCROLL_FROM, SCROLL_TO = 540, 2000 / 2400, 1300 / 2400
# A slow drag: a 0.35 s swipe flung the list ~1400 px for 700 px of travel,
# close to a whole screen. Consecutive frames must overlap or a card can pass
# unseen between them.
SCROLL_SECONDS = .8


class Step(Enum):
    IDLE = auto()
    OPEN = auto()
    READ = auto()
    VERIFY = auto()
    SCROLLED = auto()
    RETURN = auto()
    CONFIRM_HOME = auto()


class EventsClaim(ControlTaps):
    def __init__(self, *, frame_budget: int = 3, max_claims: int = 12) -> None:
        self._budget = frame_budget
        self._max_claims = max_claims
        self._threshold = .9
        self._tuning: Any = None
        self._step = Step.IDLE
        self._waited = 0
        self._frames = 0
        self._claimed = 0
        self._scrolls = 0
        self._page_taps = 0
        self._pending: events_screen.EventsReading | None = None
        self._scroll_from: tuple[str, ...] | None = None
        # Rewinding to the top before the downward pass; see the docstring.
        self._rewinding = True
        self._rewinds = 0
        self._result: CollectionResult | None = None
        self._bus: Any = None
        self._reason = 'claimed'
        self._requested_at: float | None = None
        self._announced = False

    @property
    def active(self) -> bool:
        return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        return {'status': 'running' if self.active else self._result.status if self._result else 'idle',
                'step': self._step.name.lower(), 'requested_at': self._requested_at,
                'claimed': self._claimed, 'target': TARGET, 'scrolls': self._scrolls,
                'result': asdict(self._result) if self._result else None}

    def request(self, now: float | None = None) -> bool:
        if self.active:
            return False
        self._requested_at = time.time() if now is None else now
        self._result = self._pending = self._scroll_from = None
        self._frames = self._claimed = self._scrolls = self._page_taps = self._rewinds = 0
        self._rewinding = True
        self._announced = False
        self._reason = 'claimed'
        self._enter(Step.OPEN)
        return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        if self.active:
            self._uncertain(reason)
            self._finish('failed', reason, detail, time.time() if now is None else now)

    def advance(self, *, screen: Image, device: Any, templates: Any, readings: Any,
                state: str, bus: Any, now: float | None = None,
                tuning: Any = None) -> CollectionAction | None:
        if not self.active:
            return None
        self._bus, self._tuning = bus, tuning
        moment = time.time() if now is None else now
        self._frames += 1
        if not self._announced:
            bus.publish(events.ClaimStarted(target=TARGET))
            self._announced = True
        if self._step is Step.OPEN:
            if not at_home(state, readings.current_evidence()):
                return self._wait('home_not_confirmed', 'Events opening requires confirmed home.', moment)
            if events_badge.badge_visible(screen, templates) is None:
                return self._finish('failed', 'events_control_unreadable',
                                    'No single Events icon on the current frame.', moment)
            return self._tap_target(events_badge.locate(screen, templates), device,
                                    'events_open', Step.READ, moment)
        page = events_screen.scan(screen)
        if self._step is Step.CONFIRM_HOME:
            if not page.visible and not page.error and at_home(state, readings.current_evidence()):
                return self._finish('completed', self._reason, 'Returned from Events to the main menu.', moment)
            return self._wait('home_not_restored', 'Waiting for Events to close.', moment)
        if self._frames > MAX_VISIT_FRAMES and self._step is not Step.RETURN:
            self._uncertain('events_frame_budget')
            self._reason = 'events_frame_budget'
            self._enter(Step.RETURN)
        if page.error or not page.visible:
            return self._wait('events_not_readable', 'No confirmed Events page; no control guessed.', moment)
        if self._step is Step.VERIFY:
            before = self._pending
            assert before is not None
            if page.info_close is not None or not page.missions:
                return self._wait('events_claim_unreadable',
                                  'Waiting for the Missions list after a claim.', moment)
            proof = ('claim_label_gone' if len(page.claims) < len(before.claims)
                     else 'tier_counter_moved' if page.counters != before.counters else None)
            if proof is None:
                self._waited += 1
                if self._waited <= self._budget:
                    return None
                self._uncertain('events_claim_not_confirmed')
                self._reason = 'events_claim_not_confirmed'
                self._enter(Step.RETURN)
            else:
                self._pending = None
                self._claimed += 1
                bus.publish(events.EventMissionClaimed(confirmation=proof))
                self._enter(Step.READ)
        if self._step is Step.SCROLLED:
            if page.fingerprint == self._scroll_from:
                self._waited += 1
                if self._waited <= self._budget:
                    return None
                # A dropped swipe and the end of the list look the same;
                # neither is worth a second blind swipe.
                if self._rewinding:
                    self._rewinding = False
                    self._enter(Step.READ)
                else:
                    self._end('events_list_end')
            else:
                self._enter(Step.READ)
        if self._step is Step.READ and (page.info_close is not None or not page.missions):
            if self._page_taps >= MAX_PAGE_TAPS:
                self._reason = 'events_missions_not_reached'
                self._enter(Step.RETURN)
            else:
                self._page_taps += 1
                if page.info_close is not None:
                    return self._tap_target(page.info_close, device, 'events_info_close',
                                            Step.READ, moment)
                return self._tap_target(page.missions_tab, device, 'events_missions_tab',
                                        Step.READ, moment)
        if self._step is Step.READ:
            if page.claims and self._claimed < self._max_claims:
                self._pending = page
                return self._tap_target(page.claims[0], device, 'events_claim', Step.VERIFY, moment)
            if page.claims:
                self._end('events_claim_budget')
                return self._return(page, device, moment)
            if self._rewinding and (page.at_top or self._rewinds >= MAX_SCROLLS):
                self._rewinding = False
            if self._rewinding:
                self._rewinds += 1
                return self._scroll(page, screen, device, up=True)
            if self._scrolls < MAX_SCROLLS:
                self._scrolls += 1
                return self._scroll(page, screen, device, up=False)
            self._end('events_scroll_budget')
        return self._return(page, device, moment)

    def _return(self, page: events_screen.EventsReading, device: Any,
                moment: float) -> CollectionAction | None:
        if self._step is Step.RETURN:
            return self._tap_target(page.back, device, 'events_return', Step.CONFIRM_HOME, moment)
        return None

    def _scroll(self, page: events_screen.EventsReading, screen: Image, device: Any,
                *, up: bool) -> CollectionAction:
        """One slow drag inside the list band; `up` reveals the cards above."""
        self._scroll_from = page.fingerprint
        height = screen.shape[0]
        start, end = int(height * SCROLL_FROM), int(height * SCROLL_TO)
        if up:
            start, end = end, start
        device.swipe(SCROLL_X, start, SCROLL_X, end, SCROLL_SECONDS)
        self._enter(Step.SCROLLED)
        return CollectionAction('read', 'events_rewind' if up else 'events_scroll',
                                SCROLL_X, start, 1., (SCROLL_X, min(start, end), 1, abs(start - end)))

    def _end(self, reason: str) -> None:
        if self._claimed == 0:
            reason = 'no_claimable_event_mission'
            if self._bus is not None:
                self._bus.publish(events.ClaimSkipped(
                    target=TARGET, reason=reason,
                    detail='The Events dot was lit but no mission tier was claimable.'))
        self._reason = reason
        self._enter(Step.RETURN)

    def _uncertain(self, reason: str) -> None:
        if self._pending is not None and self._bus is not None:
            self._bus.publish(events.ClaimUncertain(target=TARGET, reason=reason,
                                                   detail='An event mission claim was tapped but not verified.'))
        self._pending = None

    def _enter(self, step: Step) -> None:
        self._step, self._waited = step, 0

    def _wait(self, reason: str, detail: str, moment: float) -> None:
        self._waited += 1
        if self._waited > self._budget:
            self._uncertain(reason)
            if self._step in {Step.READ, Step.VERIFY, Step.SCROLLED}:
                self._reason = reason
                self._enter(Step.RETURN)
            else:
                self._finish('failed', reason, detail, moment)

    def _finish(self, status: str, reason: str, detail: str, moment: float) -> None:
        self._result = CollectionResult(status, reason, detail, TARGET, moment)
        if self._bus is not None:
            self._bus.publish(events.ClaimEnded(target=TARGET, claimed=self._claimed,
                                               reason=reason, aborted=status != 'completed'))
        self._step = Step.IDLE
        self._pending = None
