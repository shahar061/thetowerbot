"""Badge offers share the existing claim transaction and do not block battle."""
import time
from pathlib import Path
from types import SimpleNamespace

import cv2


import events
import screens
from evidence_scope import FactScope
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
    bot._notifications.observe('missions', True, 100, frame_id='capture-1')
    bot._notifications.observe('missions', True, 101, frame_id='capture-2')
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


def test_new_confirmed_mission_generation_offers_without_one_hour_wait() -> None:
    bot = bot_with_claims()
    bot._last_claim['missions'] = time.time()
    now = time.time()
    bot._notifications.observe('missions', True, now, frame_id='capture-1')
    assert bot._offer_claim(bot.controls.snapshot()) is None
    bot._notifications.observe('missions', True, now + 1, frame_id='capture-2')
    assert bot._offer_claim(bot.controls.snapshot()) == 'missions'
    assert bot.device.taps == []


def test_uncertain_previous_mission_claim_cannot_rearm_on_restart() -> None:
    bot = bot_with_claims()
    bot._notifications.observe('missions', True, 100, frame_id='capture-1')
    bot._notifications.observe('missions', True, 101, frame_id='capture-2')
    bot._notifications.begin('missions', 101)
    bot._notifications.finish('missions', 102, claimed=None)
    assert bot._offer_claim(bot.controls.snapshot()) is None


def test_identical_recorded_pixels_confirm_on_new_capture_only() -> None:
    bot = bot_with_claims()
    bot._last_claim['missions'] = time.time()
    bot._last_claim['mail'] = time.time()
    bot._screen = cv2.imread(str(Path(__file__).parent / 'fixtures' /
                                 'menu_main_bluestacks_1920.png'))
    bot._capture_sequence = 1
    bot._observe_menu_notifications(time.time())
    bot._observe_menu_notifications(time.time() + 1)
    assert bot._claim_due(bot.controls.snapshot()) is None
    bot._capture_sequence = 2
    bot._observe_menu_notifications(time.time() + 2)
    assert bot._claim_due(bot.controls.snapshot()) == 'missions'


def test_paused_bot_never_arms_confirmed_mission_claim() -> None:
    bot = bot_with_claims()
    bot._notifications.observe('missions', True, 100, frame_id='capture-1')
    bot._notifications.observe('missions', True, 101, frame_id='capture-2')
    bot.controls.apply({'paused': True})
    assert bot._offer_claim(bot.controls.snapshot()) is None
    assert bot.device.taps == []


def test_browser_armed_mission_walk_has_durable_receipt_attempt() -> None:
    bot = bot_with_claims()
    assert bot.claim.request()
    bot.run_once()
    assert bot._notifications.snapshot()["kinds"]["missions"]["in_flight"]


def test_settlement_uses_durable_tap_intent_after_cancellation() -> None:
    bot = bot_with_claims()
    assert bot.claim.request()
    bot._notifications.begin('missions', 100)
    bot._notifications.prepare_claim(mission='Kill enemies', mission_id='kill',
                                     coins=25, gems=3, completed_before=2,
                                     completed_target=35, visible_before=[], now=101)
    bot._mission_attempt = True
    bot.claim.cancel('paused', 'operator paused', now=102)
    bot._settle_mission_attempt()
    assert bot._notifications.snapshot()['pending_claim'] is not None
    assert bot._notifications.snapshot()['kinds']['missions']['uncertain']
    assert bot._offer_claim(bot.controls.snapshot()) is None


def test_notification_scope_uses_only_matching_verified_fact_epoch(tmp_path) -> None:
    bot = bot_with_claims()
    heartbeat = {'account_id': 'account-1', 'lease_id': 'lease-1',
                 'attempt_id': 'attempt-1', 'generation': 'generation-1'}
    bot.progress = SimpleNamespace(path=tmp_path / 'worker-heartbeat.json',
                                   snapshot=lambda: heartbeat)
    bot.account_state = SimpleNamespace(verified_scope=FactScope(
        'account-1', 'lease-1', 'generation-1', 7))
    bot._bind_notification_scope()
    assert bot._notifications.snapshot()['scope']['fact_epoch'] == 7
    bot.account_state = SimpleNamespace(verified_scope=FactScope(
        'account-1', 'lease-1', 'different-generation', 8))
    bot._bind_notification_scope()
    assert 'fact_epoch' not in bot._notifications.snapshot()['scope']


def test_invalidated_account_identity_blocks_mission_claim(tmp_path) -> None:
    bot = bot_with_claims()
    heartbeat = {'account_id': 'account-1', 'lease_id': 'lease-1',
                 'attempt_id': 'attempt-1', 'generation': 'generation-1'}
    bot.progress = SimpleNamespace(path=tmp_path / 'worker-heartbeat.json',
                                   snapshot=lambda: heartbeat)
    bot.account_state = SimpleNamespace(verified_scope=None)
    bot._bind_notification_scope()
    now = time.time()
    bot._notifications.observe('missions', True, now, frame_id='one')
    bot._notifications.observe('missions', True, now + 1, frame_id='two')
    assert bot._offer_claim(bot.controls.snapshot()) is None
    assert bot.device.taps == []


def test_read_only_reconciliation_is_due_even_after_claims_are_disabled() -> None:
    bot = bot_with_claims()
    bot._notifications.begin('missions', 100)
    bot._notifications.prepare_claim(mission='Kill enemies', mission_id='kill',
                                     coins=25, gems=3, completed_before=2,
                                     completed_target=35, visible_before=[], now=101)
    disabled = SimpleNamespace(strategy=SimpleNamespace(
        claims=SimpleNamespace(enabled=False)))
    assert bot._claim_owed(disabled)
