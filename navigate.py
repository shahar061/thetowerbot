"""Move between runs unattended.

Nothing here spends permanent resources: in-run upgrades are bought with
per-run cash that resets, and coins are only ever earned.
"""

from __future__ import annotations

import logging
import time

from adbutils import AdbDevice

import config
import events
import jitter
import vision
from device import Image, tap
from screens import ScreenState
from strategy import Strategy

logger = logging.getLogger("tower_bot.navigate")


class Navigator:
    def __init__(
        self,
        templates: vision.TemplateCache,
        bus: events.EventBus,
        cooldown: float = config.NAVIGATION_COOLDOWN_SECONDS,
        threshold: float = 0.8,
    ) -> None:
        self._templates = templates
        self._bus = bus
        self._cooldown = cooldown
        self._threshold = threshold
        self._last = float("-inf")

    def maybe_navigate(
        self,
        screen: Image,
        state: ScreenState,
        device: AdbDevice,
        now: float | None = None,
        tuning: Strategy | None = None,
        go_home: bool = False,
        menu_page: str | None = None,
        dismiss: bool = False,
    ) -> str | None:
        """Tap this screen's nav button, if there is one and it is due.

        `go_home` swaps RETRY for HOME on the death screen, so the next run
        starts from MAIN_MENU instead of from here. The caller decides when
        that is worth a detour - Navigator is told, not asked, for the same
        reason it is handed a policy rather than reading Controls itself.
        Ignored on every other screen: MAIN_MENU is where going home ENDS,
        and its own candidates - BATTLE, or RESUME BATTLE when a run was
        left suspended - are the way onward from there.

        `tuning` carries the live jitter policy, taken from the same
        snapshot run_once used for the rest of the pass. Passing it is what
        turns jitter on: with `None` this behaves exactly as it did before
        jitter existed, which is what keeps direct callers - and the tests
        that predate this - working unchanged. Navigator is deliberately
        not given a Controls of its own to read; it is handed a policy or it
        uses none.

        `menu_page` names the menu page on screen, when the caller has one to
        name. It is consulted only where `state` has no button of its own,
        which in practice means UNKNOWN: every menu page reads UNKNOWN to the
        tracker by design (see pages.py), and without this a bot parked on
        one could neither act nor leave. Absent, the behaviour is exactly
        what it was before menu pages had an exit - UNKNOWN is left alone.
        """
        # A screen offers a tuple of candidates, not one button, because one
        # slot can be drawn more than one way - see config.NAV_BUTTONS. The
        # single-button sources are wrapped rather than special-cased: there
        # is exactly one way off a menu page, and exactly one HOME.
        candidates = config.NAV_BUTTONS.get(state.value, ())
        if not candidates and menu_page is not None:
            exit_button = config.MENU_NAV_BUTTONS.get(menu_page)
            candidates = () if exit_button is None else (exit_button,)
        if go_home and state is ScreenState.GAME_OVER:
            candidates = (config.GAME_OVER_HOME,)
        # Last resort, and only when the caller says the bot is genuinely
        # stuck: a full-screen ceremony is not a menu page, so it has no
        # exit in MENU_NAV_BUTTONS and nothing above matches it. NAV_DISMISS
        # is the set shopping.py already walks for the popups that sit
        # between a tab and its page, and its ORDER is the safety property -
        # claim before skip, so a reward modal is collected rather than
        # thrown away by a blind dismissal.
        if not candidates and dismiss:
            candidates = tuple(("dismiss", path) for path in config.NAV_DISMISS)
        if not candidates:
            return None

        moment = time.monotonic() if now is None else now
        # stretch(), not spread(): this cooldown waits out a screen
        # transition, so jitter may only lengthen it. Shortening it is how
        # the second tap lands mid-animation - the double-navigation the
        # cooldown exists to prevent.
        due = (
            self._cooldown
            if tuning is None
            else jitter.stretch(self._cooldown, tuning.timing_jitter)
        )
        if moment - self._last < due:
            return None

        # First candidate that clears the threshold wins, and the order in
        # config is the priority. Nothing matching still returns None without
        # touching `self._last`, so a screen mid-animation is retried on the
        # very next scan rather than waiting out a cooldown it never spent.
        for target, template_path in candidates:
            match = vision.locate_template(
                screen, self._templates.get(template_path), self._threshold
            )
            if match is not None:
                break
        else:
            return None

        x, y = match.center
        if tuning is not None:
            x, y = jitter.point(x, y, tuning.tap_jitter_px)
            jitter.pause(tuning.tap_delay, tuning.timing_jitter)
        tap(device, x, y)
        self._last = moment
        self._bus.publish(events.Navigated(target=target))
        return target
