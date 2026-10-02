"""The in-battle menu visit, reached through a real scan pass.

test_battle_menu_visit.py (Task 5) drives the state machine directly. This
file asserts the only thing that file cannot: that run_once() actually opens
the menu on a badged hamburger, keeps stepping an active visit even off an
IN_RUN frame (after the supervisor has observed that frame, one tap per
observed frame in worker mode), and never starts a visit while something
else already owns the tap.
"""

from __future__ import annotations

import events
import config
import pytest


@pytest.fixture(autouse=True)
def menu_fixture_has_no_claimable_video(monkeypatch: pytest.MonkeyPatch) -> None:
    # This suite isolates hamburger navigation; its shared battle capture
    # also happens to show a lit six-gem tile, which has higher priority.
    monkeypatch.setattr(config, "IN_GAME_AD_WATCH_ADS", False)


def test_in_run_badged_hamburger_is_tapped_and_scan_ends(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged"], battle_menu_opt_in=True)

    assert bot.run_once() is True

    assert len(bot.device.taps) == 1 and bot.device.taps[0][0] > 950
    assert bot.battle_menu.active


def test_active_visit_owns_unknown_page(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged",
                           "battle_menu/event_page"], battle_menu_opt_in=True)

    results = [bot.run_once() for _ in range(3)]

    # Every one of the three scans acted: opened, read the menu (which owns
    # the frame without necessarily tapping - HOLD counts as "owned", not
    # "idle"), then tapped into the Event page.
    assert results == [True, True, True]
    # The visit is still mid-page - it has not queued back to the menu or
    # closed yet - so it must still be the one holding the scan.
    assert bot.battle_menu.active
    assert bot.battle_menu.current is not None
    # The Event page classifies UNKNOWN; recovery must not have been
    # stepped, and every tap that did land is the visit's own: the
    # hamburger, then a queued icon, then the page's "Tap To Return To
    # Game" footer - not a recovery escape or any other reader guessing at
    # an unread page.
    assert not any(type(e).__name__.startswith("Recovery") for e in bot.bus.published)
    assert len(bot.device.taps) == 3


def test_no_menu_visit_while_shopping_is_active(bot_with_frames, monkeypatch):
    bot = bot_with_frames(["battle_menu/collapsed_badged"], battle_menu_opt_in=True)
    monkeypatch.setattr(type(bot.shopping), "active", property(lambda self: True))

    bot.run_once()

    assert not bot.battle_menu.active


def test_a_paused_bot_never_opens_the_menu(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged"], battle_menu_opt_in=True)
    bot.controls.apply({"paused": True})

    bot.run_once()

    assert bot.device.taps == []
    assert not bot.battle_menu.active


def test_a_paused_bot_cancels_a_visit_already_underway(bot_with_frames):
    # Unlike the test above (never opens one to begin with), this starts a
    # visit unpaused, then pauses mid-visit - the case the promoted review
    # finding was about: a frozen, not cancelled, visit resuming stale on
    # whatever screen is up whenever the bot is unpaused again.
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged"],
                          battle_menu_opt_in=True)
    bot.run_once()
    assert bot.battle_menu.active
    taps_before_pause = len(bot.device.taps)

    bot.controls.apply({"paused": True})
    bot.run_once()

    assert not bot.battle_menu.active
    assert len(bot.device.taps) == taps_before_pause


def test_an_ordinary_battle_frame_taps_nothing(bot_with_frames):
    # The counterpart to the badged-hamburger test above: without this, a
    # loop that tapped every IN_RUN frame regardless of a badge would pass
    # the first test just as well. "in_run_lit" is IN_RUN with an unbadged
    # hamburger, unlike most of the other in_run_* fixtures.
    bot = bot_with_frames(["in_run_lit"], battle_menu_opt_in=True)

    bot.run_once()

    assert bot.device.taps == []
    assert not bot.battle_menu.active


def test_opening_the_menu_publishes_battle_menu_opened(bot_with_frames):
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged"],
                          battle_menu_opt_in=True)

    for _ in range(2):
        bot.run_once()

    assert any(isinstance(e, events.BattleMenuOpened) for e in bot.bus.published)


def test_an_inert_default_bot_never_opens_the_menu(bot_with_frames):
    # Every other test above opts in explicitly. Without opting in, a bot on
    # the same badged fixture must leave the menu alone - this is what makes
    # every other loop-test bot in this repo safe to build on a fixture that
    # happens to carry a badged hamburger, incidental to what it is testing.
    bot = bot_with_frames(["battle_menu/collapsed_badged"])

    bot.run_once()

    assert bot.device.taps == []
    assert not bot.battle_menu.active


class _SupervisedDevice:
    """The fake hardware behind a real DeviceSupervisor."""
    serial = "127.0.0.1:5555"

    def __init__(self):
        self.taps, self.swipes = [], []

    def click(self, x, y):
        self.taps.append((x, y))

    def swipe(self, *args):
        self.swipes.append(args)


def _supervised(bot, tmp_path):
    # Worker mode as runner.py builds it: every input goes through
    # DeviceSupervisor._action, which refuses a second input until a fresh
    # frame has been observed after the first.
    import time as _time

    from supervisor import DeviceSupervisor, GuardedDevice

    hardware = _SupervisedDevice()
    supervisor = DeviceSupervisor(path=tmp_path / "supervisor.json",
                                  endpoint=hardware.serial, connect=lambda: hardware,
                                  expected_account="account-a")
    supervisor.recover()
    supervisor.verify_account("account-a", observed_at=_time.time())
    bot.supervisor = supervisor
    bot.device = GuardedDevice(supervisor)
    refresh = bot.refresh_screen

    def captured():
        _time.sleep(.002)   # strictly increasing capture times
        screen = refresh()
        bot._screen_captured_at = _time.time()
        return screen
    bot.refresh_screen = captured
    return hardware


def test_supervised_visit_is_accepted_one_tap_per_observed_frame(bot_with_frames, tmp_path):
    bot = bot_with_frames(["battle_menu/collapsed_badged", "battle_menu/open_badged",
                           "battle_menu/event_page", "battle_menu/open_badged",
                           "battle_menu/event_page", "battle_menu/open_badged",
                           "battle_menu/collapsed_badged"],
                          battle_menu_opt_in=True)
    hardware = _supervised(bot, tmp_path)

    for _ in range(7):
        bot.run_once()   # a refused tap would raise RecoveryPreflightBlocked here

    # hamburger, icon, return footer, icon, return footer, close X
    assert len(hardware.taps) == 6
    assert not bot.battle_menu.active
    handled = [e for e in bot.bus.published if isinstance(e, events.BattleMenuIconHandled)]
    assert [e.outcome for e in handled] == ["visited", "visited"]
