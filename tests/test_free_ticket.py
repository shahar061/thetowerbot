"""The tournament Free Ticket offer and its ticket reveal, against recorded frames."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2
import pytest

import config
import events
import free_ticket
import ocr
import screens
from strategy import Shopping
from supervisor import RecoveryState
from tests.conftest import _shopping_bot

FIXTURES = Path(__file__).parent / 'fixtures'


def recorded(name: str) -> tuple[Any, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / 'ocr' / f'{name}.json').read_text()))


OFFER_FRAME = cv2.imread(str(FIXTURES / 'menu_free_ticket_offer.png'))
OFFER_BOXES = recorded('menu_free_ticket_offer')
REWARD_FRAME = cv2.imread(str(FIXTURES / 'menu_free_ticket_reward.png'))
REWARD_BOXES = recorded('menu_free_ticket_reward')
MAIN_MENU_BOXES = tuple(b for b in OFFER_BOXES if b.rect.y < 800 or b.rect.y > 1800)


def box(text: str, rect: tuple[int, int, int, int], confidence: float = .98) -> Any:
    return ocr.TextBox(text, confidence, config.Rect(*rect))


def test_the_recorded_offer_over_the_main_menu_is_read_with_its_claim_centre() -> None:
    assert free_ticket.read(OFFER_FRAME, OFFER_BOXES) == free_ticket.FreeTicket(
        free_ticket.OFFER, (540, 1475))


def test_the_recorded_ticket_reveal_is_read_with_its_claim_centre() -> None:
    assert free_ticket.read(REWARD_FRAME, REWARD_BOXES) == free_ticket.FreeTicket(
        free_ticket.REWARD, (539, 1891))


def test_the_plain_main_menu_is_not_an_offer() -> None:
    assert free_ticket.read(OFFER_FRAME, MAIN_MENU_BOXES) is None


def test_the_offer_needs_its_claim_below_the_title_and_centred() -> None:
    title = next(b for b in OFFER_BOXES if b.text == 'Free Ticket')
    rest = tuple(b for b in OFFER_BOXES if b.text != 'CLAIM')
    for claim in (box('CLAIM', (466, 700, 148, 46)),     # above the title
                  box('CLAIM', (80, 1452, 148, 46)),     # off-centre
                  box('CLAIM', (466, 2200, 148, 46))):   # far below the dialog
        assert free_ticket.read(OFFER_FRAME, rest + (claim,)) is None
    assert free_ticket.read(OFFER_FRAME, rest) is None
    two_claims = OFFER_BOXES + (box('CLAIM', (466, 1552, 148, 46)),)
    assert free_ticket.read(OFFER_FRAME, two_claims) is None
    assert title in OFFER_BOXES


def test_low_confidence_text_grants_no_tap() -> None:
    shaky = tuple(box(b.text, tuple(b.rect), .5) if b.text == 'Free Ticket' else b
                  for b in OFFER_BOXES)
    assert free_ticket.read(OFFER_FRAME, shaky) is None


def test_the_reveal_reads_any_ticket_count_and_needs_its_skip() -> None:
    for caption in ('2 TICKETS', '1 Ticket', '10 TICKETS'):
        boxes = tuple(box(caption, tuple(b.rect)) if 'TICKET' in b.text else b
                      for b in REWARD_BOXES)
        assert free_ticket.read(REWARD_FRAME, boxes) is not None
    no_skip = tuple(b for b in REWARD_BOXES if b.text != 'SKIP')
    assert free_ticket.read(REWARD_FRAME, no_skip) is None


def test_a_crowded_page_mentioning_tickets_is_not_the_reveal() -> None:
    crowded = REWARD_BOXES + tuple(box(f'row {i}', (100, 400 + 60 * i, 200, 40))
                                   for i in range(8))
    assert free_ticket.read(REWARD_FRAME, crowded) is None


@pytest.mark.parametrize('capture, state, screen, claim', [
    ('menu_free_ticket_offer.png', screens.ScreenState.MAIN_MENU,
     free_ticket.OFFER, (540, 1475)),
    ('menu_free_ticket_reward.png', screens.ScreenState.UNKNOWN,
     free_ticket.REWARD, (539, 1891)),
])
def test_the_preflight_names_the_screen_and_claims_the_ticket(
        capture: str, state: screens.ScreenState, screen: str,
        claim: tuple[int, int]) -> None:
    bot = _shopping_bot('main_menu_resume', state=state, policy=Shopping(),
                        auto_navigate=True)
    bot._screen = cv2.imread(str(FIXTURES / capture))

    class Supervisor:
        current_account = 'ACCOUNT-A'
        observed_screen = None

        def observe(self, **facts: object) -> RecoveryState:
            self.observed_screen = facts['screen']
            return RecoveryState.READY

    guard = Supervisor()
    bot.supervisor = guard
    bot.run_once()
    assert guard.observed_screen == screen
    assert bot.device.taps == [claim]
    assert any(isinstance(e, events.Tapped) and e.action == 'free_ticket:claim'
               for e in bot.bus.published)


def test_a_paused_bot_leaves_the_offer_alone() -> None:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    bot._screen = OFFER_FRAME
    bot.supervisor = type('Supervisor', (), {
        'current_account': 'ACCOUNT-A',
        'observe': lambda self, **facts: RecoveryState.READY})()
    bot.controls.apply({'paused': True})
    bot.run_once()
    assert bot.device.taps == []
