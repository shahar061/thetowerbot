"""The one-time Cards visit: Home -> Cards -> clear the intro -> Home.

The first time the Cards tab is opened the game shows an intro dialog with a
"Claim" button, then a full-screen "40 GEMS" reward with CLAIM and SKIP. The
gems are only paid for walking in, so this walk exists to walk in once: it is
offered when the tab carries the green "new" arrow (nav_arrow), taps the tab,
clears every config.NAV_DISMISS popup it meets, and taps back to Battle.

Nothing here buys a card. The only taps it can issue are the CARDS tab, the
NAV_DISMISS buttons and the BATTLE tab, each located on the frame it is
tapped from - the same ControlTaps path the missions walks use.

The page is only left once it has been seen clear twice running: the intro
animates in over a page that is already drawn, so one clear frame straight
after the tab tap proves nothing. A popup met on the way home sends the walk
back to clearing rather than past it.

The scan loop drives it (see tower_bot.run_once); the bot owns the single
instance and offers it from the main menu - see TowerBot._offer_cards_intro.
"""

from __future__ import annotations

from dataclasses import asdict
from enum import Enum, auto
import threading
import time
from typing import TYPE_CHECKING, Any

import config
import pages
from account_collection import (
    CollectionAction, CollectionResult, ControlTaps, MATCH_THRESHOLD,
    STEP_FRAME_BUDGET, at_home, locate_control,
)
from device import Image

if TYPE_CHECKING:
    from strategy import Strategy

CARDS_TEMPLATE = config.NAV_TARGETS['CARDS']
BATTLE_TEMPLATE = config.NAV_TARGETS['BATTLE_TAB']

# Clear frames of the Cards page needed before leaving it.
SETTLE_FRAMES = 2
# Popups one visit may dismiss. The recorded chain is two; more means a
# button that keeps matching after its tap, not a longer chain.
MAX_DISMISSALS = 4
# How long a finished walk keeps the arrow from offering another. A completed
# walk clears the arrow, so this only ever matters after a failed one.
RETRY_SECONDS = 600.


class Step(Enum):
    IDLE = auto()
    OPEN_CARDS = auto()
    CLEAR = auto()
    CONFIRM_HOME = auto()


def popup_visible(screen: Image, templates: Any,
                  threshold: float = MATCH_THRESHOLD) -> bool:
    """Whether any NAV_DISMISS button is on the frame."""
    return _dismiss_target(screen, templates, threshold) is not None


def _dismiss_target(screen: Image, templates: Any, threshold: float) -> Any:
    """The first NAV_DISMISS button on the frame that is not 'absent'.

    Returned even when ambiguous or unusable, so the caller's tap path
    refuses it by name instead of the walk reading the page as clear.
    """
    for path in config.NAV_DISMISS:
        try:
            image = templates.get(path)
        except (OSError, ValueError, AttributeError):
            image = None
        target = locate_control(screen, image, 'dismiss_control', threshold)
        if target.status != 'absent':
            return target
    return None


class CardsIntro(ControlTaps):
    """At most one first Cards visit, driven one frame at a time."""

    def __init__(self, *, threshold: float = MATCH_THRESHOLD,
                 frame_budget: int = STEP_FRAME_BUDGET) -> None:
        self._lock = threading.RLock()
        self._threshold = threshold
        self._budget = frame_budget
        self._step = Step.IDLE
        self._waited = 0
        self._clear_frames = 0
        self._dismissed = 0
        self._requested_at: float | None = None
        self._finished_at: float | None = None
        self._result: CollectionResult | None = None
        self._trail: list[str] = []
        self._tuning: Strategy | None = None

    # -- reporting ---------------------------------------------------------
    @property
    def active(self) -> bool:
        with self._lock:
            return self._step is not Step.IDLE

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            status = 'running' if self._step is not Step.IDLE else (
                self._result.status if self._result is not None else 'idle')
            return {'status': status, 'step': self._step.name.lower(),
                    'requested_at': self._requested_at, 'dismissed': self._dismissed,
                    'trail': list(self._trail),
                    'result': asdict(self._result) if self._result is not None else None}

    # -- lifecycle ---------------------------------------------------------
    def due(self, now: float) -> bool:
        """Whether a request now would be more than a retry of a walk just run."""
        with self._lock:
            return (self._step is Step.IDLE
                    and (self._finished_at is None
                         or now - self._finished_at >= RETRY_SECONDS))

    def request(self, now: float | None = None) -> bool:
        """Arm a visit. False when one is already running; never queues."""
        with self._lock:
            if self._step is not Step.IDLE:
                return False
            self._requested_at = time.time() if now is None else now
            self._result = None
            self._waited = 0
            self._clear_frames = 0
            self._dismissed = 0
            self._trail = []
            self._enter(Step.OPEN_CARDS)
            return True

    def cancel(self, reason: str, detail: str, now: float | None = None) -> None:
        """End a visit the loop can no longer honour. Idempotent."""
        with self._lock:
            if self._step is Step.IDLE:
                return
            self._finish('failed', reason, detail, time.time() if now is None else now)

    # -- one step ----------------------------------------------------------
    def advance(self, *, screen: Image, device: Any, templates: Any, readings: Any,
                state: str, now: float | None = None,
                tuning: Strategy | None = None) -> CollectionAction | None:
        """One frame of the visit. Returns the tap it issued, if any."""
        with self._lock:
            self._tuning = tuning
            if self._step is Step.IDLE:
                return None
            moment = time.time() if now is None else now

            if self._step is Step.OPEN_CARDS:
                if not at_home(state, readings.current_evidence()):
                    return self._wait('home_not_confirmed', 'The main menu was not confirmed '
                                      'on this frame, so no control was tapped.', moment)
                return self._tap(screen, device, templates, CARDS_TEMPLATE,
                                 'cards_tab', Step.CLEAR, moment)

            popup = _dismiss_target(screen, templates, self._threshold)
            if popup is not None:
                return self._dismiss(popup, device, moment)

            page = pages.classify_page(screen, templates).page
            if self._step is Step.CLEAR:
                if page != 'CARDS':
                    self._clear_frames = 0
                    return self._wait('cards_not_reached', 'The Cards page was not observed '
                                      'after the Cards tab was tapped.', moment)
                self._clear_frames += 1
                if self._clear_frames < SETTLE_FRAMES:
                    return None
                return self._tap(screen, device, templates, BATTLE_TEMPLATE,
                                 'battle_tab', Step.CONFIRM_HOME, moment)

            if page != 'MAIN_MENU' or not at_home(state, readings.current_evidence()):
                return self._wait('home_not_restored', 'The Cards page was cleared, but the '
                                  'main menu was not confirmed again.', moment)
            return self._finish('completed', 'visited',
                                f'The Cards page was opened and {self._dismissed} intro '
                                'popup(s) were cleared.', moment)

    # -- internals ---------------------------------------------------------
    def _dismiss(self, target: Any, device: Any, moment: float) -> CollectionAction | None:
        if self._dismissed >= MAX_DISMISSALS:
            return self._finish('failed', 'dismiss_loop', f'{self._dismissed} popups were '
                                'dismissed and another is still up; stopping.', moment)
        self._clear_frames = 0
        action = self._tap_target(target, device, 'dismiss_control', Step.CLEAR, moment)
        if action is not None:
            self._dismissed += 1
        return action

    def _enter(self, step: Step) -> None:
        self._step = step
        self._waited = 0
        self._trail.append(step.name.lower())

    def _wait(self, reason: str, detail: str, moment: float) -> CollectionAction | None:
        self._waited += 1
        if self._waited > self._budget:
            return self._finish('failed', reason, detail, moment)
        return None

    def _finish(self, status: str, reason: str, detail: str,
                moment: float) -> CollectionAction | None:
        self._result = CollectionResult(status, reason, detail, None, moment)
        self._finished_at = moment
        self._step = Step.IDLE
        self._waited = 0
        return None
