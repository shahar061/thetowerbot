"""The floating-gem claim, reached through a real scan pass.

test_gem_claim.py drives the state machine directly. This file asserts the
only thing that file cannot: that run_once() actually gets there, on a real
battle frame, with the anchor and the tuning the loop is responsible for
handing over.
"""

from __future__ import annotations

import cv2
import numpy as np

import events
import screens
from strategy import Shopping
from tests.conftest import _shopping_bot


def paint_gem(screen, centre=(560, 700), radius=26):
    """The sprite, in the measured glow colour. See test_floating_gem.py."""
    x, y = centre
    swatch = np.zeros((1, 1, 3), dtype=np.uint8)
    swatch[0, 0] = (151, 198, 130)  # dim like the live sprite; bosses glow brighter
    glow = tuple(int(c) for c in cv2.cvtColor(swatch, cv2.COLOR_HSV2BGR)[0, 0])
    points = np.array(
        [[x, y - radius], [x + radius, y], [x, y + radius], [x - radius, y]]
    )
    cv2.polylines(screen, [points], True, glow, 7)
    return screen


def battle_bot(frame_name: str = "in_run_early"):
    return _shopping_bot(
        frame_name,
        state=screens.ScreenState.IN_RUN,
        policy=Shopping(),
        auto_navigate=False,
    )


def test_a_scan_pass_taps_a_gem_on_the_ring():
    bot = battle_bot()
    paint_gem(bot._screen)

    bot.run_once()

    assert bot.device.taps, "the scan loop never reached the gem claim"


def test_a_scan_pass_taps_nothing_on_an_ordinary_battle_frame():
    # The same pass over the untouched fixture. Without this, the test
    # above would pass just as well for a loop that tapped every frame.
    bot = battle_bot()

    bot.run_once()

    assert bot.device.taps == []


def test_a_tapped_gem_that_never_confirms_says_so_on_the_bus():
    # The frame never changes, so the counter never moves. That is the
    # half worth asserting through the loop: an unconfirmed claim ends as
    # ClaimUncertain rather than as a silently dropped tap.
    bot = battle_bot()
    paint_gem(bot._screen)

    for _ in range(5):
        bot.run_once()

    uncertain = [e for e in bot.bus.published if isinstance(e, events.ClaimUncertain)]
    assert uncertain, "a tapped gem that never confirmed must say so"
    assert uncertain[0].target == "floating_gem"


def test_a_paused_bot_never_taps_the_gem():
    # "Always on" means no strategy field of its own - not immunity from
    # the pause button.
    bot = battle_bot()
    paint_gem(bot._screen)
    bot.controls.apply({"paused": True})

    bot.run_once()

    assert bot.device.taps == []
