"""A cancelled inbox walk can leave safely after pause is lifted."""
from pathlib import Path

import cv2
import pytest

import events
import mail_screen
import screens
from supervisor import RecoveryState
from strategy import Claims, Shopping
from tests.conftest import _shopping_bot


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
