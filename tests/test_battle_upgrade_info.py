"""An accidental upgrade-info panel must not trap a battle or death screen."""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest

import battle_upgrade_info
import ocr
import screens

FIXTURES = Path(__file__).parent / "fixtures"


def reported_frame():
    screenshot = cv2.imread(str(FIXTURES / "battle_upgrade_info_over_game_over_bluestacks.png"))
    assert screenshot is not None
    # The report contains BlueStacks window chrome; the bot sees this game viewport.
    return screenshot[74:1748, 88:936]


def test_reported_upgrade_info_is_located_and_tap_stays_outside_panel() -> None:
    frame = reported_frame()
    boxes = ocr.read(frame)
    point = battle_upgrade_info.dismiss_point(boxes, frame.shape)
    assert point is not None
    assert battle_upgrade_info.death_controls_visible(boxes, frame.shape)
    assert 0 <= point[0] < frame.shape[1] * .12
    assert frame.shape[0] * .35 < point[1] < frame.shape[0] * .6


@pytest.mark.parametrize("name", ["game_over", "in_run_lit", "menu_workshop_utility"])
def test_normal_screens_do_not_look_like_upgrade_info(name: str) -> None:
    frame = cv2.imread(str(FIXTURES / f"{name}.png"))
    assert frame is not None
    assert battle_upgrade_info.dismiss_point(ocr.read(frame), frame.shape) is None


def test_bot_dismisses_reported_panel_before_battle_actions(bot_in_run_on, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot._screen = reported_frame()
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.IN_RUN, 1.0,
        {state.value: 0.0 for state in screens.ScreenState},
    ))
    assert bot.run_once()
    assert len(bot.device.taps) == 1
    assert bot.device.taps[0][0] < bot._screen.shape[1] * .12
    assert any(event.type == "Tapped" and event.action == "battle_upgrade_info:dismiss"
               for event in bot.bus.published)


def test_bot_can_recover_when_overlay_hides_all_screen_anchors(bot_in_run_on, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot._screen = reported_frame()
    bot.tracker.state = screens.ScreenState.UNKNOWN
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.UNKNOWN, .2,
        {state.value: 0.0 for state in screens.ScreenState},
    ))
    assert bot.run_once()
    assert len(bot.device.taps) == 1


def test_dismissals_are_spaced_capped_and_reset_only_after_clear_frame(
    bot_in_run_on, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_in_run_on("in_run_lit")
    popup = reported_frame()
    bot._screen = popup
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.IN_RUN, 1.0,
        {state.value: 0.0 for state in screens.ScreenState},
    ))
    assert bot.run_once()
    assert not bot.run_once()  # same overlay, cooldown has not elapsed
    assert len(bot.device.taps) == 1
    for _ in range(2):
        bot._battle_info_dismiss_at = float("-inf")
        assert bot.run_once()
    bot._battle_info_dismiss_at = float("-inf")
    assert not bot.run_once()  # three failed dismissals must not tap forever
    assert len(bot.device.taps) == 3

    bot._screen = cv2.imread(str(FIXTURES / "in_run_lit.png"))
    assert not bot.run_once()
    bot._screen = popup
    assert bot.run_once()
    assert len(bot.device.taps) == 4


def test_workshop_info_is_not_treated_as_battle_info(bot_in_run_on, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot._screen = cv2.imread(str(FIXTURES / "menu_workshop_info_panel.png"))
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.UNKNOWN, .2,
        {state.value: 0.0 for state in screens.ScreenState},
    ))
    assert not bot.run_once()
    assert bot.device.taps == []


def test_paused_bot_does_not_dismiss_upgrade_info(bot_in_run_on, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = bot_in_run_on("in_run_lit")
    bot._screen = reported_frame()
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.IN_RUN, 1.0,
        {state.value: 0.0 for state in screens.ScreenState},
    ))
    bot.controls.apply({"paused": True})
    assert not bot.run_once()
    assert bot.device.taps == []
