"""The one-time Cards visit: Home -> Cards -> clear the intro -> Home.

Driven through the real scan loop (`TowerBot.run_once`) over committed
captures, like test_missions_visit. `menu_main.png` carries the Cards tab's
green "new" arrow; `main_menu_resume.png` is a main menu without it.

No committed capture shows the intro dialog or the "40 GEMS" reward, so those
frames are `menu_cards.png` with the NAV_DISMISS button pasted on - the real
templates on a real page, which is what the walk locates.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

import cards_intro
import config
import events
import ocr
from strategy import Shopping
from vision import TemplateCache

FIXTURES = Path(__file__).parent / 'fixtures'
TEMPLATES = TemplateCache(config.TEMPLATE_DIR)

# Tab template centres, measured on the 1080x2400 captures.
CARDS_TAB = (446, 2312)
BATTLE_TAB = (84, 2312)
# Where the popup buttons are pasted.
POPUP_AT = (400, 1500)


def image(name: str) -> np.ndarray:
    frame = cv2.imread(str(FIXTURES / f'{name}.png'))
    assert frame is not None, f'missing fixture: {name}.png'
    return frame


def popup(template: str) -> np.ndarray:
    """menu_cards.png with one NAV_DISMISS button drawn at POPUP_AT."""
    frame = image('menu_cards').copy()
    button = TEMPLATES.get(template)
    x, y = POPUP_AT
    frame[y:y + button.shape[0], x:x + button.shape[1]] = button
    return frame


def centre(template: str) -> tuple[int, int]:
    h, w = TEMPLATES.get(template).shape[:2]
    return (POPUP_AT[0] + w // 2, POPUP_AT[1] + h // 2)


CLAIM = popup('nav/claim.png')
REWARD = popup('nav/claim_reward.png')


def drive(bot: Any, monkeypatch: pytest.MonkeyPatch, frames: list[Any]) -> None:
    """One scan per frame, each a fixture name or an image. No OCR text."""
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    for frame in frames:
        bot._screen = image(frame) if isinstance(frame, str) else frame
        bot.run_once()


def walk_taps(bot: Any) -> list[tuple[int, int]]:
    return [(e.x, e.y) for e in bot.bus.published
            if isinstance(e, events.Tapped) and e.action.startswith('cards_intro:')]


@pytest.fixture
def bot(bot_on_main_menu: Any) -> Any:
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    return bot


# --- offering -------------------------------------------------------------

def test_the_arrow_on_the_cards_tab_arms_the_visit(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['menu_main'])
    assert bot.cards_intro.active


def test_a_cards_tab_without_the_arrow_arms_nothing(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['main_menu_resume', 'main_menu_resume'])
    assert not bot.cards_intro.active
    assert bot.cards_intro.snapshot()['status'] == 'idle'


def test_a_paused_bot_arms_nothing(bot: Any, monkeypatch) -> None:
    bot.controls.apply({'paused': True})
    drive(bot, monkeypatch, ['menu_main'])
    assert not bot.cards_intro.active


# --- the walk -------------------------------------------------------------

def test_the_visit_claims_both_popups_and_returns_home(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, [
        'menu_main',              # arrow seen: armed
        'menu_main',              # the Cards tab is tapped
        CLAIM,                    # the intro's Claim
        REWARD,                   # the full-screen reward's CLAIM
        'menu_cards', 'menu_cards',   # clear twice running: tap Battle
        'menu_main', 'menu_main',
    ])
    snapshot = bot.cards_intro.snapshot()
    assert snapshot['status'] == 'completed', snapshot
    assert snapshot['dismissed'] == 2
    assert walk_taps(bot) == [CARDS_TAB, centre('nav/claim.png'),
                              centre('nav/claim_reward.png'), BATTLE_TAB]


def test_a_popup_that_animates_in_late_is_still_claimed(bot: Any, monkeypatch) -> None:
    """One clear frame of the page is not enough to leave it."""
    drive(bot, monkeypatch, ['menu_main', 'menu_main', 'menu_cards', CLAIM])
    assert walk_taps(bot) == [CARDS_TAB, centre('nav/claim.png')]
    assert bot.cards_intro.active


def test_a_page_that_never_arrives_ends_the_visit_without_another_tap(
        bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['menu_main', 'menu_main'] + ['menu_missions'] * 8)
    result = bot.cards_intro.snapshot()['result']
    assert result['status'] == 'failed'
    assert result['reason'] == 'cards_not_reached'
    assert walk_taps(bot) == [CARDS_TAB]


def test_a_failed_visit_is_not_offered_again_straight_away(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['menu_main', 'menu_main'] + ['menu_missions'] * 8)
    assert not bot.cards_intro.active
    drive(bot, monkeypatch, ['menu_main'])
    assert not bot.cards_intro.active


def test_a_popup_that_never_goes_away_stops_the_visit(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['menu_main', 'menu_main'] + [CLAIM] * 8)
    result = bot.cards_intro.snapshot()['result']
    assert result['reason'] == 'dismiss_loop'
    assert len(walk_taps(bot)) == 1 + cards_intro.MAX_DISMISSALS


def test_pausing_the_bot_ends_a_visit_in_flight(bot: Any, monkeypatch) -> None:
    drive(bot, monkeypatch, ['menu_main', 'menu_main'])
    bot.controls.apply({'paused': True})
    drive(bot, monkeypatch, [CLAIM])
    assert bot.cards_intro.snapshot()['result']['reason'] == 'paused'
    assert walk_taps(bot) == [CARDS_TAB]


# --- the popup detector ---------------------------------------------------

@pytest.mark.parametrize('name', ['menu_main', 'menu_cards', 'menu_cards_stocked', 'main_menu'])
def test_no_committed_page_reads_as_a_popup(name: str) -> None:
    assert not cards_intro.popup_visible(image(name), TEMPLATES)


def test_a_pasted_popup_reads_as_one() -> None:
    assert cards_intro.popup_visible(CLAIM, TEMPLATES)
    assert cards_intro.popup_visible(REWARD, TEMPLATES)
