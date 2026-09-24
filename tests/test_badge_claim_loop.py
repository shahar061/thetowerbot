"""Badge offers share the existing claim transaction and do not block battle."""
import time

import events
import screens
from strategy import Claims, Shopping
from tests.conftest import _shopping_bot
from tower_bot import TowerBot


def bot_with_claims() -> TowerBot:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True, claims=Claims(enabled=True))
    bot._best_wave = None
    bot._last_claim['missions'] = time.time() - 3700
    bot.cards_intro._done = True
    return bot


def test_missions_badge_offers_existing_verified_claim_walk() -> None:
    bot = bot_with_claims()
    bot._missions_badge = True
    assert bot._offer_claim(bot.controls.snapshot()) == 'missions'
    assert bot.claim.active
    assert bot.device.taps == []


def test_mail_arms_shared_transaction_once_without_tapping_until_next_frame() -> None:
    bot = bot_with_claims()
    bot._last_claim['missions'] = time.time()
    bot._mail_badge = True
    assert bot._offer_claim(bot.controls.snapshot()) == 'mail'
    assert bot._offer_claim(bot.controls.snapshot()) is None
    assert bot.claim.active
    assert bot.claim.snapshot()['target'] == 'mail'
    assert bot.device.taps == []
