"""Visit the in-battle menu's badged icons, one tap per frame."""
from __future__ import annotations

import logging
import random
from enum import Enum, auto
from typing import Any, Callable

import battle_menu
import config
import events
import jitter
from battle_menu import Icon
from battle_menu_state import BattleMenuState
from device import Image, tap

logger = logging.getLogger(__name__)


class Outcome(Enum):
    IDLE = auto()    # not involved; the loop carries on
    HOLD = auto()    # owns this frame but did not tap (waiting for a page)
    TAPPED = auto()  # tapped; the loop must wait for a fresh frame


class Step(Enum):
    IDLE = auto()
    OPENING = auto()
    IN_PAGE = auto()
    RETURNING = auto()
    CLOSING = auto()


class BattleMenuVisit:
    def __init__(self, bus: Any, templates: Any, state: BattleMenuState, *,
                 sleep: Callable[[float], Any] | None = None,
                 rng: random.Random | None = None) -> None:
        self._bus = bus
        self._templates = templates
        self._state = state
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._step = Step.IDLE
        self._waited = 0
        self._queue: list[Icon] = []
        self.current: Icon | None = None
        self._badge: battle_menu.Badge | None = None
        self._scrolls = 0
        self._outcome = "visited"
        # The "now" at which the current icon was tapped into, not the time
        # we happen to finish or give up on it - a cooldown measures from
        # when the icon was attended to, so two icons queued together and
        # visited back-to-back expire together too.
        self._entered = 0.0

    @property
    def active(self) -> bool:
        return self._step is not Step.IDLE

    def cancel(self, reason: str = "cancelled", detail: str = "") -> None:
        """End an active visit outright: no tap, no state record.

        Every other walk in the loop is cancelled the same way when the bot
        is paused mid-walk (see tower_bot.py's `settings.paused` block) -
        this is that same rule applied here. Unlike `_bail`, which is a
        mid-visit failure that still records the icon as failed (so its
        cooldown backs off) and taps a return/close point to leave the page
        cleanly, `cancel` must not touch the device at all: a paused bot's
        screen is stale the moment the operator steps away, and tapping
        whatever return point that stale frame happens to show would act on
        a page that may no longer even be there by the time the tap lands.
        Recording no state means a resumed visit is free to pick the same
        icon back up on its own terms next time it is due, rather than
        carrying a phantom "failed" backoff from a visit the operator ended.
        """
        if reason or detail:
            logger.info("battle menu visit cancelled: %s%s", reason,
                       f" ({detail})" if detail else "")
        self._go(Step.IDLE)

    def observe(self, *, screen: Image, boxes: Callable[[], tuple], device: Any,
                policy: Any, now: float, in_run: bool) -> Outcome:
        if self._step is Step.IDLE:
            return self._idle(screen, device, policy, now, in_run)
        self._waited += 1
        if self._waited > config.BATTLE_MENU_STEP_FRAMES:
            return self._bail(screen, boxes, device, policy, now)
        handler = {Step.OPENING: self._opening, Step.IN_PAGE: self._in_page,
                   Step.RETURNING: self._returning, Step.CLOSING: self._closing}[self._step]
        return handler(screen, boxes, device, policy, now)

    # -- steps -----------------------------------------------------------------
    def _idle(self, screen, device, policy, now, in_run) -> Outcome:
        if not in_run or not self._state.session_allowed(now) or not self._state.worth_opening(now):
            return Outcome.IDLE
        button = battle_menu.collapsed(screen, self._templates)
        if button is None or not button.badged:
            return Outcome.IDLE
        self._state.session_started(now)
        self._tap(device, policy, button.point)
        self._go(Step.OPENING)
        return Outcome.TAPPED

    def _opening(self, screen, boxes, device, policy, now) -> Outcome:
        menu = battle_menu.read_menu(screen, self._templates)
        if menu is None:
            return Outcome.HOLD
        self._state.remember(menu)
        self._queue = self._state.due(menu, now)
        self._rng.shuffle(self._queue)
        self._bus.publish(events.BattleMenuOpened(due=tuple(self._queue)))
        return self._next(screen, menu, device, policy, now)

    def _in_page(self, screen, boxes, device, policy, now) -> Outcome:
        reading = battle_menu.read_page(boxes())
        if reading.page == "none" or reading.return_point is None:
            return Outcome.HOLD
        if reading.page == "event_info":
            close = battle_menu.event_modal_close(screen, boxes())
            if close is None:
                return Outcome.HOLD
            self._tap(device, policy, close)
            return Outcome.TAPPED
        # The Event page itself is visit-and-return: main's Events walk
        # (events_claim.EventsClaim) owns claiming and scrolling, armed from
        # home by the main-menu Events dot, which a visit here leaves lit.
        if reading.page == "store":
            if battle_menu.free_gem_tile(screen, boxes()) is not None:
                self._outcome = "free_tile_seen"   # Task 7 turns this into an ad watch
            elif self._scrolls < 2:
                self._scrolls += 1
                self._swipe(device, policy)
                self._waited = 0
                return Outcome.TAPPED
        self._look(policy)
        self._tap(device, policy, reading.return_point)
        self._go(Step.RETURNING)
        return Outcome.TAPPED

    def _returning(self, screen, boxes, device, policy, now) -> Outcome:
        menu = battle_menu.read_menu(screen, self._templates)
        if menu is not None:
            self._finish_icon()
            return self._next(screen, menu, device, policy, now)
        if battle_menu.collapsed(screen, self._templates) is not None:
            self._finish_icon()
            self._go(Step.IDLE)
            return Outcome.HOLD
        return Outcome.HOLD

    def _closing(self, screen, boxes, device, policy, now) -> Outcome:
        if battle_menu.collapsed(screen, self._templates) is not None:
            self._go(Step.IDLE)
            return Outcome.IDLE
        return Outcome.HOLD

    # -- helpers ---------------------------------------------------------------
    def _next(self, screen, menu, device, policy, now) -> Outcome:
        if not self._queue:
            close = battle_menu.close_point(screen, self._templates)
            if close is None:
                return self._bail(screen, lambda: (), device, policy, now)
            self._tap(device, policy, close)
            self._go(Step.CLOSING)
            return Outcome.TAPPED
        self.current = self._queue.pop(0)
        reading = menu[self.current]
        self._badge, self._scrolls, self._outcome = reading.badge, 0, "visited"
        self._entered = now
        self._tap(device, policy, reading.point)
        self._go(Step.IN_PAGE)
        return Outcome.TAPPED

    def _finish_icon(self) -> None:
        if self.current is None:
            return
        self._state.handled(self.current, self._badge, self._entered)
        self._bus.publish(events.BattleMenuIconHandled(icon=self.current, outcome=self._outcome))
        self.current = None

    def _bail(self, screen, boxes, device, policy, now) -> Outcome:
        if self.current is not None:
            self._state.failed(self.current, self._badge, self._entered)
            self._bus.publish(events.BattleMenuIconHandled(icon=self.current, outcome="failed"))
            self.current = None
        self._go(Step.IDLE)
        reading = battle_menu.read_page(boxes())
        point = reading.return_point or battle_menu.close_point(screen, self._templates)
        if point is None:
            return Outcome.IDLE
        self._tap(device, policy, point)
        return Outcome.TAPPED

    def _go(self, step: Step) -> None:
        self._step, self._waited = step, 0
        if step is Step.IDLE:
            self._queue, self.current = [], None

    def _look(self, policy) -> None:
        jitter.pause(self._rng.uniform(0.4, 1.2), 0.0, rng=self._rng,
                     **({"sleep": self._sleep} if self._sleep else {}))

    def _tap(self, device, policy, point) -> None:
        jitter.pause(policy.tap_delay, policy.timing_jitter, rng=self._rng,
                     **({"sleep": self._sleep} if self._sleep else {}))
        x, y = jitter.point(point[0], point[1], policy.tap_jitter_px, rng=self._rng)
        tap(device, x, y)

    def _swipe(self, device, policy) -> None:
        x = 540 + self._rng.randint(-60, 60)
        device.swipe(x, 1900 + self._rng.randint(-40, 40), x + self._rng.randint(-20, 20),
                     500 + self._rng.randint(-40, 40), self._rng.uniform(0.5, 0.8))
