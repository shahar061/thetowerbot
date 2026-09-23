"""Measured launch of Tower from a newly created BlueStacks clone."""

from __future__ import annotations

import time
from typing import Any, Callable

import ocr
from device import Image, capture_screen
from geometry import supported_frame


_GAME_CENTER = "com.bluestacks.gamecenter"
_LAUNCHER = "com.uncube.launcher3"
_TOWER = "com.TechTreeGames.TheTower"


def launch_tower_from_game_center(
    device: Any, *,
    capture: Callable[[Any], Image] = capture_screen,
    read_text: Callable[[Image], tuple[ocr.TextBox, ...]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Leave Game Center and press the labeled Tower launcher icon.

    A clone may already be on the launcher if Home was pressed during an
    earlier inspected run. Every press is guarded by a foreground package and
    the icon's current native frame position.
    """
    last_home = -10
    for tick in range(240):
        package = getattr(device.app_current(), "package", None)
        if package == _TOWER:
            return
        if package == _GAME_CENTER and tick - last_home >= 10:
            device.shell("input keyevent KEYCODE_HOME")
            last_home = tick
        elif package == _LAUNCHER:
            frame = capture(device)
            if not supported_frame(frame.shape[1], frame.shape[0]):
                raise ValueError("unsupported BlueStacks launcher geometry")
            boxes = (read_text(frame) if read_text is not None
                     else ocr.read(frame, strict=True, min_confidence=0.))
            icons = [box for box in boxes if "".join(box.text.split()).casefold() == "thetower"
                     and box.confidence >= .9
                     and 300 <= box.rect.y <= 600
                     and 0 <= box.rect.x < box.rect.x + box.rect.w <= 1080
                     and 0 < box.rect.h <= 80]
            if len(icons) > 1:
                raise ValueError("ambiguous Tower icon on launcher")
            if len(icons) == 1:
                label = icons[0].rect
                device.click(label.x + label.w // 2, label.y - 110)
                for _ in range(120):
                    if getattr(device.app_current(), "package", None) == _TOWER:
                        return
                    sleep(.5)
                raise ValueError("Tower did not open from verified launcher icon")
        sleep(.5)
    raise ValueError("verified BlueStacks Home and Tower icon unavailable")


def wait_for_tower_ready(
    device: Any, observe: Callable[[Any], Any], *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait for a recognized pre-R00 screen without touching game controls."""
    for _ in range(120):
        frame = observe(device)
        if frame.conflict_dialog:
            raise ValueError("session_conflict" if any(
                phrase in frame.conflict_dialog.lower() for phrase in (
                    "new session detected", "cloud session different than local session",
                )) else "ambiguous dialog")
        # The link-account prompt dims a ready Home; Home is still underneath.
        if frame.screen in {"home", "google_play_profile", "link_account_prompt"}:
            return
        if frame.screen != "unknown":
            raise ValueError("unexpected Tower startup screen")
        sleep(1.)
    raise ValueError("Tower ready screen unavailable")


def complete_first_launch_onboarding(
    device: Any, observe: Callable[[Any], Any], *,
    sleep: Callable[[float], None] = time.sleep,
    before_action: Callable[[str, Any], None] | None = None,
    consent_already_recorded: bool = False,
) -> None:
    """Accept measured clone-only consent and wait through the first short run.

    The caller must attest that this is a new clone with Tower never launched.
    No source instance or New Account action belongs in this flow.
    """
    agreed = consent_already_recorded
    dismissed_profile = False
    left_game_over = False
    for _ in range(180):
        frame = observe(device)
        if frame.conflict_dialog:
            raise ValueError("session_conflict" if any(
                phrase in frame.conflict_dialog.lower() for phrase in (
                    "new session detected", "cloud session different than local session",
                )) else "ambiguous dialog")
        if frame.screen == "google_play_profile":
            if dismissed_profile or set(frame.controls) != {"dismiss_google_play_profile"}:
                raise ValueError("unexpected Play Games profile prompt")
            if before_action is not None:
                before_action("dismiss_google_play_profile", frame)
            device.click(*frame.controls["dismiss_google_play_profile"])
            dismissed_profile = True
        elif frame.screen == "tower_consent":
            if agreed or set(frame.controls) != {"i_agree"}:
                raise ValueError("Tower consent control unavailable")
            if before_action is not None:
                before_action("i_agree", frame)
            device.click(*frame.controls["i_agree"])
            agreed = True
        elif frame.screen == "game_over":
            if not agreed or left_game_over or set(frame.controls) != {"home_from_game_over"}:
                raise ValueError("unexpected first-run game stats")
            if before_action is not None:
                before_action("home_from_game_over", frame)
            device.click(*frame.controls["home_from_game_over"])
            left_game_over = True
        elif frame.screen in {"home", "link_account_prompt"}:
            if not agreed or not left_game_over:
                raise ValueError("first-launch consent or tutorial unverified")
            return
        elif frame.screen == "battle" and agreed and not left_game_over:
            pass  # the first run, when read as in-run rather than unknown
        elif frame.screen != "unknown":
            raise ValueError("unexpected first-launch screen")
        sleep(1.)
    raise ValueError("first-launch onboarding did not reach Tower home")
