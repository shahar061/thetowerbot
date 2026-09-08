"""A menu page that holds actions forever with nothing walking.

The bug this file exists for: claiming a milestone whose reward was
`Unlock Lab` left the game on a full-screen "Lab unlocked" ceremony. That
ceremony carries a SKIP button, and milestones_screen.scan treats a trusted
SKIP as proof a milestones screen is up - so it held every action. The
claim walk had already ended (failed: modal_unreadable, having correctly
refused to tap what it could not read), so nothing was armed to advance and
clear the screen, and the guard's early return runs BEFORE navigation - so
the recovery path never got a frame either.

The screen could only be cleared by an action, and the screen forbade
actions. The bot sat there indefinitely; observed for ~180 consecutive
scans on a live device before it was freed by hand.

tests/fixtures/milestones_lab_unlocked.png is that exact screen, captured
off the device while it was stuck. It OCRs to three boxes: SKIP, "Lab
unlocked", OK.
"""

from __future__ import annotations

import screens
from strategy import Shopping
from tests.conftest import _shopping_bot


def stuck_bot():
    """A bot facing the ceremony, with nothing armed and navigation on."""
    return _shopping_bot(
        "milestones_lab_unlocked",
        state=screens.ScreenState.UNKNOWN,
        policy=Shopping(),
        auto_navigate=True,
    )


def test_the_guard_holds_at_first_because_a_walk_may_be_about_to_arm():
    # The floor must not be so eager that it fights a legitimate hold. A
    # claim walk arms on the main menu and takes several scans to reach the
    # ladder; releasing after one or two would race it.
    bot = stuck_bot()

    for _ in range(3):
        bot.run_once()

    assert bot.device.taps == []


def test_a_held_page_with_nothing_walking_eventually_lets_navigation_recover():
    # The actual bug. Without a floor this loop taps nothing, forever.
    bot = stuck_bot()

    for _ in range(15):
        bot.run_once()

    assert bot.device.taps, (
        "the guard held every action for 15 scans with no transaction armed - "
        "this is the deadlock"
    )
    # And it recovered through the ceremony's own SKIP button, at
    # (843,272)-(953,320), rather than tapping somewhere arbitrary. A
    # recovery that taps a random point is not a recovery.
    x, y = bot.device.taps[0]
    assert 800 <= x <= 990 and 230 <= y <= 350, f"tapped {x},{y}, not SKIP"


def test_a_reward_modal_is_claimed_before_it_is_skipped():
    # NAV_DISMISS's order is the safety property of this fix. Both CLAIM and
    # SKIP appear on a reward ceremony, and a dismissal that reached for
    # SKIP first would throw the reward away - the exact hazard
    # milestones_screen.scan holds actions to avoid.
    import config

    assert config.NAV_DISMISS.index("nav/claim_reward.png") < config.NAV_DISMISS.index(
        "nav/skip.png"
    )


def test_nothing_taps_underneath_a_walk_that_is_still_reading():
    # The floor counts only scans where NOTHING is walking. A claim walk
    # owns the menus while it runs, and letting navigation tap the modal it
    # is halfway through reading is the exact hazard milestones_screen.scan
    # holds actions to prevent.
    #
    # On this frame the walk gives up almost at once - modal_unreadable, the
    # same verdict it reached on the live device - so what this pins is the
    # window while it IS active, however short.
    bot = stuck_bot()
    bot.milestones_claim.request()

    while bot.milestones_claim.active:
        bot.run_once()
        assert bot.device.taps == [], "navigation tapped under a live walk"
