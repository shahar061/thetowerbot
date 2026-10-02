"""Badge offers share the existing claim transaction and do not block battle."""
import dataclasses
import time
from pathlib import Path
from types import SimpleNamespace

import cv2


import events
import screens
import tier_select
from evidence_scope import FactScope
from strategy import Claims, Shopping, TierPromotion
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


def test_settlement_counts_a_confirmed_weekly_chest_without_a_mission_card() -> None:
    bot = bot_with_claims()
    bot._notifications.begin('missions', 100)
    assert bot.claim.request(now=100)
    bot.claim._chests_claimed = 1
    bot.claim._finish('completed', 'claimed', 'weekly chest confirmed', 102)
    bot._mission_attempt = True

    bot._settle_mission_attempt()

    assert not bot._notifications.snapshot()['kinds']['missions']['in_flight']
    assert bot._notifications.snapshot()['last_claim_at'] is not None


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


def test_events_dot_arms_the_shared_transaction_without_tapping() -> None:
    bot = bot_with_claims()
    bot._last_claim['missions'] = time.time()
    bot._screen = cv2.imread(str(Path(__file__).parent / 'fixtures' /
                                 'menu_main_events_badge_tier_next.png'))
    bot._observe_menu_notifications(time.time())
    assert bot._events_badge
    assert bot._offer_claim(bot.controls.snapshot()) == 'events'
    assert bot.claim.active
    assert bot.claim.snapshot()['target'] == 'events'
    assert bot._offer_claim(bot.controls.snapshot()) is None
    assert bot.device.taps == []


def promoting_bot(thresholds: dict[int, int], *, tier: int | None = 1,
                  best: int | None = 126) -> TowerBot:
    """A bot on the lit-arrow Tier 1 menu, whose strategy promotes at `thresholds`."""
    bot = bot_with_claims()
    bot.controls.replace(dataclasses.replace(
        bot.controls.snapshot().strategy,
        tier_promotion=TierPromotion(tuple(thresholds.items()))))
    bot._screen = cv2.imread(str(Path(__file__).parent / 'fixtures' /
                                 'menu_main_events_badge_tier_next.png'))
    bot._ladder_tier = tier
    if tier is not None and best is not None:
        bot._tier_best_wave[tier] = best
    return bot


def test_lit_tier_arrow_is_left_alone_without_a_promotion_threshold() -> None:
    bot = promoting_bot({})
    assert not bot._advance_tier(bot.controls.snapshot())
    assert bot.device.taps == []


def test_lit_tier_arrow_is_left_alone_below_the_tiers_threshold() -> None:
    bot = promoting_bot({1: 127})
    assert not bot._advance_tier(bot.controls.snapshot())
    assert bot.device.taps == []


def test_lit_tier_arrow_is_left_alone_before_the_played_tier_is_known() -> None:
    bot = promoting_bot({1: 100}, tier=None)
    assert not bot._advance_tier(bot.controls.snapshot())
    assert bot.device.taps == []


def test_reaching_the_threshold_taps_once_and_holds_battle_until_the_panel_redraws() -> None:
    bot = promoting_bot({1: 126})
    settings = bot.controls.snapshot()
    assert bot._advance_tier(settings)
    assert len(bot.device.taps) == 1
    x, y = bot.device.taps[0]
    assert abs(x - 689) <= 20 and abs(y - 1320) <= 20
    assert bot._advance_tier(settings)
    assert len(bot.device.taps) == 1
    bot._tier_tap_at = float('-inf')
    bot._screen = cv2.imread(str(Path(__file__).parent / 'fixtures' /
                                 'menu_main_events_badge_tier2_top.png'))
    assert not bot._advance_tier(settings)
    assert len(bot.device.taps) == 1


def test_one_tier_step_per_finished_run() -> None:
    """The played tier is only learned when a run ends, so a still-lit arrow
    after one tap must wait for that run rather than climb a second tier."""
    bot = promoting_bot({1: 100})
    settings = bot.controls.snapshot()
    assert bot._advance_tier(settings)
    bot._tier_tap_at = float('-inf')
    assert not bot._advance_tier(settings)
    assert len(bot.device.taps) == 1
    bot.runs.completed += 1
    assert bot._advance_tier(settings)
    assert len(bot.device.taps) == 2


def test_resume_battle_menu_keeps_its_tier() -> None:
    bot = promoting_bot({1: 100})
    # A lit arrow pasted over the resume frame's dim one: neither the arrow
    # nor a met threshold may move a suspended run's tier.
    fixtures = Path(__file__).parent / 'fixtures'
    lit = bot._screen
    bot._screen = cv2.imread(str(fixtures / 'main_menu_resume.png'))
    bot._screen[1278:1362, 652:726] = lit[1278:1362, 652:726]
    assert tier_select.read_next(bot._screen, bot.templates).available
    assert not bot._advance_tier(bot.controls.snapshot())
    assert bot.device.taps == []
