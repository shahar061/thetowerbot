"""Bounded free-mail claims, with frame-local buttons and post-tap proof."""
from __future__ import annotations

from dataclasses import asdict
from enum import Enum, auto
import time
from typing import Any

import events
import mail_screen
import menu_badges
from account_collection import CollectionAction, CollectionResult, ControlTaps, at_home
from device import Image

MAX_NEWS_SCROLLS = 8
MAX_VISIT_FRAMES = 160
MAX_INBOX_ENTRIES = 32


class Step(Enum):
    IDLE = auto()
    OPEN = auto()
    READ = auto()
    VERIFY = auto()
    RETURN = auto()
    CONFIRM_HOME = auto()
    NEWS_LIST = auto()
    NEWS_SCROLL = auto()
    NEWS_DETAIL = auto()
    MAIL_DETAIL = auto()


class MailClaim(ControlTaps):
    """No selecting unknown rows, deleting, links, ads or purchases.

    Supports a list's explicit Claim All and a displayed detail's Claim.
    A page without a safe control is left through its explicit return button.
    """
    def __init__(self, *, frame_budget: int = 3, max_claims: int = 4) -> None:
        self._budget = frame_budget
        self._max_claims = max_claims
        self._threshold = .9
        self._tuning: Any = None
        self._step = Step.IDLE
        self._waited = 0
        self._frames = 0
        self._claimed = 0
        self._pending: tuple[int | None, bool, int | None, int | None] | None = None
        self._result: CollectionResult | None = None
        self._bus: Any = None
        self._reason = 'claimed'
        self._requested_at: float | None = None
        self._announced = False
        self._seen_news: set[str] = set()
        self._pending_news: str | None = None
        self._news_mode = False
        self._opened_news = 0
        self._seen_mail: set[str] = set()
        self._pending_mail: str | None = None
        self._mail_mode = False
        self._opened_mail = 0
        self._news_scrolls = 0
        self._scroll_fingerprint: tuple[str, ...] = ()
        self._scroll_pages: set[tuple[str, ...]] = set()

    @property
    def active(self) -> bool:
        return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        return {'status': 'running' if self.active else self._result.status if self._result else 'idle',
                'step': self._step.name.lower(), 'requested_at': self._requested_at,
                'claimed': self._claimed, 'target': 'mail',
                'news_read': self._opened_news,
                'news_scrolls': self._news_scrolls,
                'result': asdict(self._result) if self._result else None}

    def request(self, now: float | None = None) -> bool:
        if self.active:
            return False
        self._requested_at = time.time() if now is None else now
        self._result, self._pending = None, None
        self._frames = self._claimed = 0
        self._announced = False
        self._pending_news = None
        self._news_mode = False
        self._opened_news = 0
        self._pending_mail = None
        self._mail_mode = False
        self._opened_mail = 0
        self._seen_mail.clear()
        self._news_scrolls = 0
        self._scroll_fingerprint = ()
        self._scroll_pages.clear()
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
            bus.publish(events.ClaimStarted(target='mail'))
            self._announced = True
        page = mail_screen.scan(screen)
        if self._frames > MAX_VISIT_FRAMES and self._step not in {Step.RETURN, Step.CONFIRM_HOME}:
            self._uncertain('mail_frame_budget')
            if page.visible and page.back is not None and not page.error:
                self._end_news('mail_frame_budget')
            else:
                return self._finish('failed', 'mail_frame_budget', 'Mail visit exceeded its frame budget.', moment)
        if self._step is Step.OPEN:
            if page.visible or page.error or not at_home(state, readings.current_evidence()):
                return self._wait('home_not_confirmed', 'Mail opening requires confirmed home.', moment)
            badge = menu_badges.read_badge(screen, templates, 'mail')
            if badge is None:
                if self._news_mode or self._mail_mode:
                    return self._finish('completed', 'mail_badge_not_readable',
                                        'Back home; no numbered inbox badge could be read. '
                                        'Badge clearance was not verified.', moment)
                return self._finish('failed', 'mail_badge_unreadable', 'No numbered envelope on current frame.', moment)
            return self._tap_target(badge.control, device, 'mail_open', Step.READ, moment)
        if self._step is Step.CONFIRM_HOME:
            if not page.visible and not page.error and at_home(state, readings.current_evidence()):
                if (self._news_mode or self._mail_mode) and self._opened_news + self._opened_mail < MAX_INBOX_ENTRIES:
                    self._enter(Step.OPEN)
                    return None
                return self._finish('completed', self._reason, 'Returned from mail to the main menu.', moment)
            return self._wait('home_not_restored', 'Waiting for mail to close.', moment)
        if page.error or not page.visible:
            return self._wait('mail_not_readable', 'No confirmed MAIL/INBOX page; no control guessed.', moment)
        if self._step is Step.NEWS_SCROLL:
            if page.selected_tab != 'news' or not page.news:
                return self._wait('news_scroll_unreadable', 'Waiting for a readable News list after scrolling.', moment)
            fingerprint = tuple(mail_screen.normalise(entry.title) for entry in page.news)
            if fingerprint == self._scroll_fingerprint:
                self._waited += 1
                if self._waited <= self._budget:
                    return None
                # A dropped swipe and the end of the list look identical.
                # Neither justifies a repeated blind swipe on this visit.
                self._end_news('news_list_unchanged')
            elif fingerprint in self._scroll_pages:
                self._end_news('news_list_cycle')
            else:
                self._enter(Step.NEWS_LIST)
        if self._step is Step.NEWS_LIST:
            if page.selected_tab != 'news' or not page.news:
                return self._wait('news_list_unreadable', 'No supported outlined News entries.', moment)
            unread = [entry for entry in page.news
                      if mail_screen.normalise(entry.title) not in self._seen_news]
            if unread and self._opened_news + self._opened_mail < MAX_INBOX_ENTRIES:
                self._pending_news = unread[0].title
                return self._tap_target(unread[0].control, device, 'news_item', Step.NEWS_DETAIL, moment)
            if not unread and page.news_badge and self._news_scrolls < MAX_NEWS_SCROLLS:
                points = mail_screen.news_scroll_points(screen, page)
                if points is not None:
                    x, y, x2, y2 = points
                    fingerprint = tuple(mail_screen.normalise(entry.title) for entry in page.news)
                    device.swipe(x, y, x2, y2, .35)
                    self._news_scrolls += 1
                    self._scroll_fingerprint = fingerprint
                    self._scroll_pages.add(fingerprint)
                    self._enter(Step.NEWS_SCROLL)
                    return CollectionAction('news_scroll', 'news_scroll', x, y, 1.,
                                            (x, y2, 1, y - y2))
            self._end_news('news_scroll_budget' if self._news_scrolls >= MAX_NEWS_SCROLLS
                           else 'news_visible_entries_read')
        if self._step is Step.NEWS_DETAIL:
            if (page.news_detail_title is None or self._pending_news is None
                    or mail_screen.normalise(page.news_detail_title)
                    != mail_screen.normalise(self._pending_news)):
                return self._wait('news_detail_unconfirmed', 'Waiting for the selected News title.', moment)
            self._seen_news.add(mail_screen.normalise(self._pending_news))
            self._opened_news += 1
            bus.publish(events.NewsRead(title=self._pending_news))
            self._pending_news = None
            self._enter(Step.READ)
        if self._step is Step.MAIL_DETAIL:
            if (page.news_detail_title is None or self._pending_mail is None
                    or mail_screen.normalise(page.news_detail_title)
                    != mail_screen.normalise(self._pending_mail)):
                return self._wait('mail_detail_unconfirmed', 'Waiting for the selected mail title.', moment)
            self._seen_mail.add(mail_screen.normalise(self._pending_mail))
            self._opened_mail += 1
            self._pending_mail = None
            self._enter(Step.READ)
        if self._step is Step.VERIFY:
            before, was_confirmed, coins, gems = self._pending
            proof = ('unclaimed_count_decreased' if before is not None and page.unclaimed is not None
                     and page.unclaimed < before else 'reward_claimed_message'
                     if not was_confirmed and page.confirmed and page.claim is None else None)
            if proof is None:
                self._waited += 1
                if self._waited <= self._budget:
                    return None
                self._uncertain('mail_claim_not_confirmed')
                self._reason = 'mail_claim_not_confirmed'
                self._enter(Step.RETURN)
            else:
                self._pending = None
                self._claimed += 1
                bus.publish(events.MailClaimed(coins=coins, gems=gems, confirmation=proof))
                self._enter(Step.READ)
        if self._step is Step.READ:
            if page.claim is not None and page.unclaimed != 0 and self._claimed < self._max_claims:
                self._pending = (page.unclaimed, page.confirmed, page.coins, page.gems)
                return self._tap_target(page.claim, device, 'mail_claim', Step.VERIFY, moment)
            unread_mail = [entry for entry in page.mail
                           if mail_screen.normalise(entry.title) not in self._seen_mail]
            if (page.selected_tab == 'mail' and unread_mail
                    and self._opened_news + self._opened_mail < MAX_INBOX_ENTRIES):
                self._mail_mode = True
                self._pending_mail = unread_mail[0].title
                return self._tap_target(unread_mail[0].control, device, 'mail_item', Step.MAIL_DETAIL, moment)
            if page.selected_tab == 'mail':
                self._mail_mode = False
            if (page.news_tab is not None and page.news_badge
                    and self._opened_news + self._opened_mail < MAX_INBOX_ENTRIES):
                self._news_mode = True
                return self._tap_target(page.news_tab, device, 'news_tab', Step.NEWS_LIST, moment)
            if page.news_tab is not None:
                self._news_mode = False
            if self._claimed == 0:
                self._reason = 'no_eligible_mail_reward'
                bus.publish(events.ClaimSkipped(target='mail', reason=self._reason,
                                               detail='No unambiguous free claim button on this mail page.'))
            self._enter(Step.RETURN)
        if self._step is Step.RETURN:
            return self._tap_target(page.back, device, 'mail_return', Step.CONFIRM_HOME, moment)
        return None

    def _uncertain(self, reason: str) -> None:
        if self._pending is not None and self._bus is not None:
            self._bus.publish(events.ClaimUncertain(target='mail', reason=reason,
                                                   detail='A mail claim was tapped but not verified.'))
        self._pending = None

    def _enter(self, step: Step) -> None:
        self._step, self._waited = step, 0
        if step is Step.OPEN:
            # Opening another item's detail returns through the home screen,
            # so a later reopening may legitimately start at page one again.
            self._scroll_pages.clear()
            self._scroll_fingerprint = ()

    def _end_news(self, reason: str) -> None:
        self._news_mode = self._mail_mode = False
        self._reason = reason
        self._enter(Step.RETURN)

    def _wait(self, reason: str, detail: str, moment: float) -> None:
        self._waited += 1
        if self._waited > self._budget:
            self._uncertain(reason)
            if self._step in {Step.READ, Step.NEWS_LIST, Step.NEWS_SCROLL, Step.NEWS_DETAIL, Step.MAIL_DETAIL, Step.VERIFY}:
                self._reason = reason
                self._news_mode = False
                self._mail_mode = False
                self._enter(Step.RETURN)
            else:
                self._finish('failed', reason, detail, moment)

    def _finish(self, status: str, reason: str, detail: str, moment: float) -> None:
        self._result = CollectionResult(status, reason, detail, 'mail', moment)
        if self._bus is not None:
            self._bus.publish(events.ClaimEnded(target='mail', claimed=self._claimed,
                                               reason=reason, aborted=status != 'completed'))
        self._step = Step.IDLE
        self._pending = None
