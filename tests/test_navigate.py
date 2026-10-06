from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import pytest

import events
import navigate
import vision
from navigate import Navigator
from screens import ScreenState

FIXTURES = Path(__file__).parent / "fixtures"
TEMPLATES = Path(__file__).parent.parent / "templates"


def frame(name: str):
    return cv2.imread(str(FIXTURES / f"{name}.png"), cv2.IMREAD_COLOR)


@pytest.fixture
def nav() -> Navigator:
    return Navigator(vision.TemplateCache(TEMPLATES), events.EventBus())


def test_taps_retry_on_game_over(nav: Navigator) -> None:
    dev = MagicMock()
    target = nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=0.0)

    assert target == "RETRY"
    dev.click.assert_called_once()


def test_taps_battle_on_the_main_menu(nav: Navigator) -> None:
    dev = MagicMock()
    target = nav.maybe_navigate(frame("main_menu"), ScreenState.MAIN_MENU, dev, now=0.0)

    assert target == "BATTLE"
    dev.click.assert_called_once()


def test_taps_resume_battle_when_a_run_was_left_suspended(nav: Navigator) -> None:
    """A run abandoned mid-battle - a kill, a crash, a pause never returned
    from - leaves the menu offering RESUME BATTLE where BATTLE normally sits.
    Same slot, same 514x164 box, different glyphs: measured on this fixture
    buttons/battle.png scores 0.535 there, under the 0.8 threshold, so
    locate_template returned None and maybe_navigate declined SILENTLY - no
    tap, no event, nothing on the feed. Measured live: twenty minutes of
    `screen is MAIN_MENU` skips with no NAV line between them, and no way
    out, because a second shopping visit is not due until a run completes
    and no run can start.
    """
    dev = MagicMock()
    target = nav.maybe_navigate(
        frame("main_menu_resume"), ScreenState.MAIN_MENU, dev, now=0.0
    )

    assert target == "RESUME_BATTLE"
    dev.click.assert_called_once()


def test_the_resume_button_is_not_matched_on_a_fresh_menu() -> None:
    """The negative control, and the reason RESUME_BATTLE is its own target
    rather than a second crop filed under BATTLE. Only one of the two labels
    is ever on screen; a template loose enough to match both would report a
    resumed run as a fresh one on every single menu.

    0.8 is Navigator's default threshold - the score this crop has to stay
    under for the two buttons to remain distinguishable.
    """
    cache = vision.TemplateCache(TEMPLATES)
    score, _ = vision.best_score(
        frame("main_menu"), cache.get("buttons/resume_battle.png")
    )

    assert score < 0.8


def test_does_nothing_in_run(nav: Navigator) -> None:
    dev = MagicMock()
    assert nav.maybe_navigate(frame("in_run_lit"), ScreenState.IN_RUN, dev, now=0.0) is None
    dev.click.assert_not_called()


def test_cooldown_prevents_a_double_tap(nav: Navigator) -> None:
    dev = MagicMock()
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=0.0)
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=1.0) is None
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=4.0)


def test_cooldown_override_replaces_the_default_for_one_call(nav: Navigator) -> None:
    dev = MagicMock()
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=0.0)
    # Within the default 3.0 s, so refused without an override.
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=0.5) is None
    assert nav.maybe_navigate(
        frame("game_over"), ScreenState.GAME_OVER, dev, now=0.5, cooldown=0.0)
    # The override did not stick: the default applies again.
    assert nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev, now=1.0) is None


def test_now_none_reads_a_fresh_clock_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Omitting `now` must sample a live clock on every call, not freeze at a
    fixed value. The buggy `moment = 0.0 if now is None else now` froze
    `moment` at 0.0 forever, so after the first successful tap `self._last`
    also became 0.0 and every later call - real elapsed time notwithstanding
    - saw `moment - self._last == 0.0 < cooldown`, permanently blocking
    navigation. `cooldown=0.0` cannot expose this (0 - 0 < 0.0 is False
    either way), so this test uses a small nonzero cooldown with the clock
    monkeypatched to controlled, increasing values - deterministic, and no
    real sleeping required."""
    dev = MagicMock()
    ticks = iter([100.0, 100.2])
    monkeypatch.setattr(navigate.time, "monotonic", lambda: next(ticks))
    nav2 = Navigator(vision.TemplateCache(TEMPLATES), events.EventBus(), cooldown=0.05)

    assert nav2.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev) == "RETRY"
    assert nav2.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, dev) == "RETRY"


def test_retry_is_located_not_hardcoded(nav: Navigator) -> None:
    """The modal moves ~46px between the two game-over fixtures. Locating the
    button by match must find it in both, at different y positions."""
    cache = vision.TemplateCache(TEMPLATES)
    template = cache.get("buttons/retry.png")

    a = vision.locate_template(frame("game_over"), template, 0.8)
    b = vision.locate_template(frame("game_over_fade"), template, 0.8)

    assert a is not None and b is not None
    assert a.center[1] != b.center[1]


# --- The between-runs detour to MAIN_MENU ---------------------------------
# RETRY restarts from the death screen without ever passing through
# MAIN_MENU, and MAIN_MENU is the only screen a Workshop visit can begin
# from. When a visit is due the caller asks for HOME instead.


def test_taps_home_on_game_over_when_a_visit_is_due(nav: Navigator) -> None:
    dev = MagicMock()
    target = nav.maybe_navigate(
        frame("game_over"), ScreenState.GAME_OVER, dev, now=0.0, go_home=True
    )

    assert target == "HOME"
    dev.click.assert_called_once()


def test_home_and_retry_are_different_buttons(nav: Navigator) -> None:
    """Both live on the death screen; tapping one must not land on the other."""
    retry_dev, home_dev = MagicMock(), MagicMock()
    nav.maybe_navigate(frame("game_over"), ScreenState.GAME_OVER, retry_dev, now=0.0)
    nav.maybe_navigate(
        frame("game_over"), ScreenState.GAME_OVER, home_dev, now=0.0, go_home=True
    )

    assert retry_dev.click.call_args != home_dev.click.call_args


def test_go_home_does_not_disturb_the_main_menu(nav: Navigator) -> None:
    """The detour is a GAME_OVER concern. On MAIN_MENU the bot has already
    arrived, so go_home must not displace the menu's own candidates."""
    dev = MagicMock()
    target = nav.maybe_navigate(
        frame("main_menu"), ScreenState.MAIN_MENU, dev, now=0.0, go_home=True
    )

    assert target == "BATTLE"


@pytest.mark.parametrize("fixture, page", [
    ("menu_workshop_utility", "WORKSHOP"),
    ("menu_cards", "CARDS"),
])
def test_a_named_menu_page_gets_a_route_back_to_the_menu(
    nav: Navigator, fixture: str, page: str,
) -> None:
    """NAV_BUTTONS is keyed by ScreenState, and a menu page is UNKNOWN to it -
    so a bot parked on the workshop could neither act (the action loop gates
    on IN_RUN) nor leave. Measured live: twenty unbroken minutes of doing
    nothing on the UTILITY tab.
    """
    dev = MagicMock()
    target = nav.maybe_navigate(
        frame(fixture), ScreenState.UNKNOWN, dev, now=0.0, menu_page=page
    )

    assert target == "BATTLE_TAB"
    dev.click.assert_called_once()


def test_an_unnamed_unknown_screen_is_still_left_alone(nav: Navigator) -> None:
    """The positive control: UNKNOWN with no page named is a screen nobody
    modelled, and tapping a guess at where its exit might be is exactly the
    blind tap this module refuses to make elsewhere."""
    dev = MagicMock()
    assert nav.maybe_navigate(
        frame("menu_workshop_utility"), ScreenState.UNKNOWN, dev, now=0.0
    ) is None
    dev.click.assert_not_called()
