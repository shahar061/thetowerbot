"""Claim the six-gem video tile on the battle HUD.

The video owns all frames from the first tap through balance confirmation.
Only a witnessed close control and the exact six-gem CLAIM screen authorize
ad taps. A failed attempt stays latched until the tile disappears, so a
returned battle frame cannot start the same uncertain video repeatedly.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

import cv2

import ad_exit
import battle_menu
import config
import events
import jitter
import ocr
from device import Image, tap
from vision import TemplateCache

logger = logging.getLogger("tower_bot.in_game_ad")

_PLAY = "in_game_ad/play.png"
_PLAY_THRESHOLD = .88
_LATE_REWARD_WINDOW = 600.0


def find_tile(screen: Image, anchor: tuple[int, int] | None,
              templates: TemplateCache) -> tuple[int, int] | None:
    """Find the lit six-gem play icon relative to the battle cash HUD."""
    if anchor is None or screen.shape[:2] != (2400, 1080):
        return None
    template = templates.get(_PLAY)
    if template is None:
        return None
    x0, y0 = anchor[0] + 100, anchor[1] + 1260
    x1, y1 = anchor[0] + 240, anchor[1] + 1410
    region = screen[max(0, y0):min(screen.shape[0], y1),
                    max(0, x0):min(screen.shape[1], x1)]
    if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
        return None
    _, score, _, (dx, dy) = cv2.minMaxLoc(
        cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    if score < _PLAY_THRESHOLD:
        return None
    return (max(0, x0) + dx + template.shape[1] // 2,
            max(0, y0) + dy + template.shape[0] // 2)


class InGameAdClaim:
    """A single bounded rewarded-video attempt, never concurrent with buying."""

    def __init__(self, bus: Any, templates: TemplateCache, reader: Any,
                 *, sleep: Callable[[float], Any] | None = None) -> None:
        self._bus = bus
        self._templates = templates
        self._reader = reader
        self._sleep = sleep
        self._phase = "idle"
        self._started = 0.0
        self._claimed_at = 0.0
        self._last_close = float("-inf")
        self._closes = 0
        self._recover_started = 0.0
        self._last_back = float("-inf")
        self._backs = 0
        self._before: int | None = None
        self._run_id: int | None = None
        self._tile_latched = False

    @property
    def active(self) -> bool:
        return self._phase != "idle"

    def cancel(self, reason: str, detail: str = "") -> None:
        if self.active:
            self._uncertain(reason)

    def may_claim_late_reward(self, now: float, run_id: int | None) -> bool:
        """Keep a timed-out attempt eligible for its exact reward briefly."""
        return (self._phase == "idle" and self._before is not None
                and self._run_id == run_id
                and self._started <= now <= self._started + _LATE_REWARD_WINDOW)

    def observe(self, screen: Image, anchor: tuple[int, int] | None,
                device: Any, policy: Any, now: float, run_id: int | None,
                in_run: bool) -> bool:
        """Return True while this ad owns the frame, including waiting scans."""
        tile = find_tile(screen, anchor, self._templates) if in_run else None
        if not self.active:
            if self.may_claim_late_reward(now, run_id):
                claim = battle_menu.ad_reward_claim(screen, ocr.read(screen), amount=6)
                if claim is not None:
                    self._tap(device, policy, claim)
                    self._claimed_at = now
                    self._phase = "confirming"
                    return True
            if tile is None:
                self._tile_latched = False
                return False
            if self._tile_latched or not config.IN_GAME_AD_WATCH_ADS:
                return False
            self._before = self._reader.read(screen, config.GEMS_FROM_CASH,
                                             anchor, "wallet")
            self._run_id = run_id
            self._started = now
            self._closes = 0
            self._last_close = float("-inf")
            self._tile_latched = True
            self._phase = "watching"
            self._tap(device, policy, tile)
            return True

        if self._phase == "watching":
            claim = battle_menu.ad_reward_claim(screen, ocr.read(screen), amount=6)
            if claim is not None:
                self._tap(device, policy, claim)
                self._claimed_at = now
                self._phase = "confirming"
                return True
            if (now - self._started >= config.BATTLE_MENU_AD_CLOSE_MIN_SECONDS
                    and self._closes < 3 and now - self._last_close >= 3):
                close = ad_exit.find_close(screen, self._templates, device)
                if close is not None:
                    self._tap(device, policy, close)
                    self._closes += 1
                    self._last_close = now
                    return True
            if now - self._started >= config.BATTLE_MENU_AD_TIMEOUT:
                if ad_exit.ad_foreground(device):
                    device.press_back()
                    self._phase = "recovering"
                    self._recover_started = self._last_back = now
                    self._backs = 1
                else:
                    self._uncertain("ad_timeout")
            elif in_run and now - self._started >= 8:
                self._uncertain("ad_not_started")
            return True

        if self._phase == "recovering":
            claim = battle_menu.ad_reward_claim(screen, ocr.read(screen), amount=6)
            if claim is not None:
                self._tap(device, policy, claim)
                self._claimed_at = now
                self._phase = "confirming"
                return True
            if in_run and anchor is not None:
                after = self._reader.read(screen, config.GEMS_FROM_CASH, anchor, "wallet")
                if self._before is not None and after is not None and after - self._before == 6:
                    self._record_claim(after)
                else:
                    self._uncertain("ad_timeout")
                return True
            if (self._backs < 3 and now - self._last_back >= 5
                    and ad_exit.ad_foreground(device)):
                device.press_back()
                self._backs += 1
                self._last_back = now
                return True
            if now - self._recover_started >= 20:
                self._uncertain("ad_timeout")
            return True

        if in_run and anchor is not None:
            after = self._reader.read(screen, config.GEMS_FROM_CASH, anchor, "wallet")
            if (self._before is not None and after is not None
                    and after - self._before == 6):
                self._record_claim(after)
                return True
        if now - self._claimed_at >= config.BATTLE_MENU_AD_CONFIRM_TIMEOUT:
            self._uncertain("balance_unconfirmed")
        return True

    def _record_claim(self, after: int) -> None:
        before = self._before
        if before is None:
            return
        self._bus.publish(events.InGameAdGemClaimed(
            gems_before=before, gems_after=after, delta=6,
            run_id=self._run_id))
        self._phase = "idle"
        self._before = None
        logger.info("claimed in-game ad: %s -> %s gems", before, after)

    def _uncertain(self, reason: str) -> None:
        self._bus.publish(events.ClaimUncertain(
            target="in_game_ad_gems", reason=reason,
            detail=f"run_id={self._run_id}"))
        self._phase = "idle"
        logger.info("in-game ad claim uncertain: %s", reason)

    def _tap(self, device: Any, policy: Any, point: tuple[int, int]) -> None:
        x, y = jitter.point(*point, policy.tap_jitter_px)
        if self._sleep is None:
            jitter.pause(policy.tap_delay, policy.timing_jitter)
        else:
            jitter.pause(policy.tap_delay, policy.timing_jitter, sleep=self._sleep)
        tap(device, x, y)
