"""Perk ownership through real scan passes, including supervised workers."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

import config
import events
import ocr
from ocr import TextBox
from tests.test_perks import choice, summary
from tests.test_perks import regen_choice


def setup_bot(bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch,
              images: list[np.ndarray], *, enabled: bool = True) -> Any:
    bot = bot_with_frames(['in_run_early'])
    bot.controls.apply({'auto_pick_perks': enabled})
    bot.gem.observe = lambda **kwargs: False
    monkeypatch.setattr(config, 'IN_GAME_AD_WATCH_ADS', False)
    index = 0

    def refresh() -> np.ndarray:
        nonlocal index
        bot._screen = images[min(index, len(images) - 1)]
        index += 1
        return bot._screen

    _, panel_boxes = choice()
    button_boxes = (TextBox('New Perk', .99, config.Rect(450, 40, 185, 40)),)

    def boxes(frame: np.ndarray, **kwargs: Any) -> tuple[TextBox, ...]:
        if np.array_equal(frame, summary()[0]):
            return summary()[1]
        if np.array_equal(frame, choice_image):
            return panel_boxes
        if np.array_equal(frame, available_image):
            return button_boxes
        return ()

    choice_image = choice()[0]
    available_image = battle(True)
    monkeypatch.setattr(ocr, 'read', boxes)
    monkeypatch.setattr(ocr.FrameReads, 'battle', lambda self: boxes(self.screen))
    bot.refresh_screen = refresh
    return bot


def battle(available: bool = False) -> np.ndarray:
    image = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'in_run_early.png'))
    if available:
        image[25:100, 350:750] = (245, 45, 165)
    return image


def test_supervised_joined_heading_still_selects_first_perk(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tests.test_battle_menu_loop import _supervised
    image, boxes = regen_choice()
    bot = setup_bot(bot_with_frames, monkeypatch, [image])
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: boxes)
    monkeypatch.setattr(ocr.FrameReads, 'battle', lambda self: boxes)
    hardware = _supervised(bot, tmp_path)
    bot.run_once()
    assert hardware.taps == [(540, 611)]
    assert bot.perks.active


@pytest.mark.parametrize('supervised', [False, True])
def test_pick_first_once_per_frame_and_resume_battle(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, supervised: bool) -> None:
    from tests.test_battle_menu_loop import _supervised
    image, _ = choice()
    bot = setup_bot(bot_with_frames, monkeypatch, [battle(True), image, image, battle()])
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    assert bot.run_once()
    assert len(hardware.taps) == 1
    assert bot.perks.active
    assert bot.run_once()
    assert len(hardware.taps) == 2
    assert 520 < hardware.taps[-1][1] < 702
    bot.run_once()
    assert len(hardware.taps) == 2
    bot.run_once()
    assert not bot.perks.active
    assert any(isinstance(e, events.PerkPicked) for e in bot.bus.published)


@pytest.mark.parametrize('supervised', [False, True])
def test_preopened_panel_is_recognized_without_prior_runtime(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, supervised: bool) -> None:
    from tests.test_battle_menu_loop import _supervised
    image, _ = choice()
    bot = setup_bot(bot_with_frames, monkeypatch, [image])
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    bot.run_once()
    assert len(hardware.taps) == 1
    assert 520 < hardware.taps[0][1] < 702


def test_pausing_cancels_opening_without_selecting(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = setup_bot(bot_with_frames, monkeypatch, [battle(True), choice()[0]])
    bot.run_once()
    bot.controls.apply({'paused': True})
    bot.run_once()
    assert len(bot.device.taps) == 1
    assert not bot.perks.active


def test_disabled_profile_leaves_available_perk_alone(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = setup_bot(bot_with_frames, monkeypatch, [battle(True)], enabled=False)
    bot.run_once()
    assert not bot.perks.active
    assert bot.device.taps == []


def test_pending_purchase_prevents_opening_perks(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = setup_bot(bot_with_frames, monkeypatch, [battle(True)])
    bot.autopilot.pending = object()
    monkeypatch.setattr(bot.autopilot, 'step', lambda *args, **kwargs: None)
    bot.run_once()
    assert not bot.perks.active
    assert bot.device.taps == []


def test_unreadable_panel_holds_battle_actions(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = setup_bot(bot_with_frames, monkeypatch, [choice()[0]])

    def unreadable(self: ocr.FrameReads) -> tuple[TextBox, ...]:
        raise RuntimeError('OCR unavailable')

    monkeypatch.setattr(ocr.FrameReads, 'full', unreadable)
    bot.run_once()
    assert bot.device.taps == []


def test_perk_panel_releases_stale_menu_owner(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from battle_menu_visit import Step
    bot = setup_bot(bot_with_frames, monkeypatch, [choice()[0]])
    bot.battle_menu._step = Step.OPENING
    bot.run_once()
    assert not bot.battle_menu.active
    assert len(bot.device.taps) == 1
    assert bot.perks.active


@pytest.mark.parametrize('supervised', [False, True])
def test_selection_summary_is_closed_before_battle_resumes(
        bot_with_frames: Any, monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path, supervised: bool) -> None:
    from tests.test_battle_menu_loop import _supervised
    bot = setup_bot(bot_with_frames, monkeypatch, [choice()[0], summary()[0], battle()])
    hardware = _supervised(bot, tmp_path) if supervised else bot.device
    bot.run_once()
    bot.run_once()
    bot.run_once()
    assert len(hardware.taps) == 2
    assert hardware.taps[-1] == (935, 400)
    assert not bot.perks.active
    assert len([e for e in bot.bus.published if isinstance(e, events.PerkPicked)]) == 1
