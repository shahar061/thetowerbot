"""The in-battle menu visit, reached through a real scan pass.

test_battle_menu_visit.py (Task 5) drives the state machine directly. This
file asserts the only thing that file cannot: that run_once() actually opens
the menu on a badged hamburger, keeps stepping an active visit ahead of
supervisor recovery even off an IN_RUN frame, and never starts a visit while
something else already owns the tap.
"""

from __future__ import annotations

import events


def test_in_run_badged_hamburger_is_tapped_and_scan_ends(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged"])

    assert bot.run_once() is True

    assert len(bot.device.taps) == 1 and bot.device.taps[0][0] > 950
    assert bot.battle_menu.active


def test_active_visit_owns_unknown_page_before_recovery(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged",
                           "battle_menu/event_page"])

    for _ in range(3):
        bot.run_once()

    # The Event page classifies UNKNOWN; recovery must not have been stepped.
    assert not any(type(e).__name__.startswith("Recovery") for e in bot.bus.published)


def test_no_menu_visit_while_shopping_is_active(bot_with_frames, monkeypatch):
    bot = bot_with_frames(["battle_menu/collapsed_badged"])
    monkeypatch.setattr(type(bot.shopping), "active", property(lambda self: True))

    bot.run_once()

    assert not bot.battle_menu.active


def test_a_paused_bot_never_opens_the_menu(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged"])
    bot.controls.apply({"paused": True})

    bot.run_once()

    assert bot.device.taps == []
    assert not bot.battle_menu.active


def test_an_ordinary_battle_frame_taps_nothing(bot_with_frames):
    # The counterpart to the badged-hamburger test above: without this, a
    # loop that tapped every IN_RUN frame regardless of a badge would pass
    # the first test just as well. "in_run_lit" is IN_RUN with an unbadged
    # hamburger, unlike most of the other in_run_* fixtures.
    bot = bot_with_frames(["in_run_lit"])

    bot.run_once()

    assert bot.device.taps == []
    assert not bot.battle_menu.active


def test_opening_the_menu_publishes_battle_menu_opened(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged"])

    for _ in range(2):
        bot.run_once()

    assert any(isinstance(e, events.BattleMenuOpened) for e in bot.bus.published)
