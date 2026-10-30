"""A cancelled inbox walk can leave safely after pause is lifted."""
from pathlib import Path

import cv2
import pytest

import events
import mail_screen
import missions_screen
import screens
from supervisor import RecoveryState
from strategy import Claims, Shopping
from tests.conftest import _shopping_bot
from tower_bot import TowerBot


def missions_bot(*, paused: bool = False, auto_navigate: bool = True) -> TowerBot:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=auto_navigate)
    bot._screen = cv2.imread(str(Path(__file__).parent / 'fixtures' /
                                'menu_missions_claimable_no_status_bar.png'))
    bot.controls.apply({'paused': paused})
    return bot


def test_orphan_missions_returns_without_retrying_a_reward() -> None:
    bot = missions_bot()
    bot.run_once()
    assert len(bot.device.taps) == 1
    assert bot.device.taps[0][1] > 2400 * .85
    assert not bot.claim.active
    assert any(isinstance(event, events.Tapped) and event.action == 'missions_recovery:return'
               for event in bot.bus.published)
    assert not any(isinstance(event, (events.MissionClaimed, events.WeeklyChestClaimed,
                                     events.ClaimStarted)) for event in bot.bus.published)


@pytest.mark.parametrize('paused,auto_navigate', [(True, True), (False, False)])
def test_orphan_missions_respects_operator_controls(paused: bool, auto_navigate: bool) -> None:
    bot = missions_bot(paused=paused, auto_navigate=auto_navigate)
    bot.run_once()
    assert bot.device.taps == []


def test_orphan_missions_return_attempts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = missions_bot()
    monkeypatch.setattr('tower_bot.config.NAVIGATION_COOLDOWN_SECONDS', 0.)
    for _ in range(6):
        bot.run_once()
    assert len(bot.device.taps) == 3


def test_orphan_missions_without_readable_footer_never_taps(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = missions_bot()
    monkeypatch.setattr(missions_screen, 'return_control', lambda *args: None)
    bot.run_once()
    assert bot.device.taps == []


def test_orphan_missions_is_named_for_supervisor_preflight() -> None:
    bot = missions_bot()
    class Supervisor:
        current_account = 'ACCOUNT-A'
        observed_screen = None
        def observe(self, **facts: object) -> RecoveryState:
            self.observed_screen = facts['screen']
            return (RecoveryState.READY if self.observed_screen == 'MISSIONS' else RecoveryState.BLOCKED)
    guard = Supervisor()
    bot.supervisor = guard
    bot.run_once()
    assert guard.observed_screen == 'MISSIONS'
    assert len(bot.device.taps) == 1


@pytest.mark.parametrize('capture', ['menu_mail_empty_cua.png', 'menu_news_list_cua.png',
                                    'menu_news_detail_cua.png'])
def test_paused_inbox_exits_after_resume_without_claiming(capture: str) -> None:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=True, claims=Claims(enabled=True))
    source = cv2.imread(str(Path(__file__).parent / 'fixtures' / capture))
    bot._screen = cv2.resize(source[32:869, 47:424], (1080, 2400))
    assert bot.claim.request_mail()
    bot.controls.apply({'paused': True})
    bot.run_once()
    assert not bot.claim.active
    assert bot.device.taps == []
    bot.controls.apply({'paused': False})
    bot.run_once()
    assert len(bot.device.taps) == 1
    assert bot.device.taps[0][1] > 2400 * .85
    assert not any(isinstance(event, (events.MailClaimed, events.NewsRead))
                   for event in bot.bus.published)


def test_orphan_inbox_without_readable_exit_holds_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    monkeypatch.setattr(mail_screen, 'parse', lambda *args: mail_screen.MailReading(visible=True))
    bot.run_once()
    assert bot.device.taps == []


def test_orphan_inbox_exit_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    from account_screens import ControlTarget
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=True)
    footer = ControlTarget('mail_return', (540, 2250), 'located', 1., (320, 2220, 440, 60))
    monkeypatch.setattr(mail_screen, 'parse', lambda *args: mail_screen.MailReading(visible=True, back=footer))
    monkeypatch.setattr('tower_bot.config.NAVIGATION_COOLDOWN_SECONDS', 0.)
    for _ in range(6):
        bot.run_once()
    assert len(bot.device.taps) == 3


def test_live_inbox_is_named_for_device_preflight() -> None:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=True)
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / 'menu_mail_empty_live_39.jpg'))
    assert frame is not None
    bot._screen = frame

    class Supervisor:
        current_account = 'ACCOUNT-A'
        observed_screen = None

        def observe(self, **facts: object) -> RecoveryState:
            self.observed_screen = facts['screen']
            return (RecoveryState.READY if self.observed_screen == 'INBOX'
                    else RecoveryState.BLOCKED)

    guard = Supervisor()
    bot.supervisor = guard
    bot.run_once()
    assert guard.observed_screen == 'INBOX'
    assert len(bot.device.taps) == 1  # Safe return footer; the walk is inactive.


def test_orphan_inbox_exits_while_a_purchase_reconciliation_is_held(monkeypatch: pytest.MonkeyPatch) -> None:
    from account_screens import ControlTarget
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=True)
    # An unanswered purchase keeps shopping "active" without any visit
    # walking; its inspection waits for the menu the footer restores.
    monkeypatch.setattr(type(bot.shopping), 'active', property(lambda self: True))
    assert not bot.shopping.visit_in_progress
    footer = ControlTarget('mail_return', (540, 2250), 'located', 1., (320, 2220, 440, 60))
    monkeypatch.setattr(mail_screen, 'parse', lambda *args: mail_screen.MailReading(visible=True, back=footer))
    bot.run_once()
    assert len(bot.device.taps) == 1
    assert any(isinstance(event, events.Tapped) and event.action == 'mail_recovery:return'
               for event in bot.bus.published)
