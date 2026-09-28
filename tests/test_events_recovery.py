"""The Events page must be named for the device preflight, or no walk can leave it."""
from pathlib import Path

import cv2
import pytest

import screens
from supervisor import RecoveryState
from strategy import Claims, Shopping
from tests.conftest import _shopping_bot


class Supervisor:
    current_account = 'ACCOUNT-A'
    observed_screen = None

    def observe(self, **facts: object) -> RecoveryState:
        self.observed_screen = facts['screen']
        return (RecoveryState.READY if self.observed_screen == 'EVENTS'
                else RecoveryState.BLOCKED)


def _events_bot(capture: str):
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=True, claims=Claims(enabled=True))
    frame = cv2.imread(str(Path(__file__).parent / 'fixtures' / capture))
    assert frame is not None
    bot._screen = frame
    guard = Supervisor()
    bot.supervisor = guard
    return bot, guard


@pytest.mark.parametrize('capture', ['menu_events_info.png', 'menu_events_missions.png'])
def test_events_page_is_named_for_device_preflight(capture: str) -> None:
    bot, guard = _events_bot(capture)
    bot.run_once()
    assert guard.observed_screen == 'EVENTS'
    assert len(bot.device.taps) == 1  # Safe return footer; the walk is inactive.


def test_first_visit_event_information_is_closed_by_the_walk() -> None:
    import events_claim
    bot, guard = _events_bot('menu_events_info.png')
    assert bot.claim.request_events()
    bot.claim._events._step = events_claim.Step.READ
    bot.run_once()
    assert guard.observed_screen == 'EVENTS'
    [(x, y)] = bot.device.taps  # The info X, jittered like every located tap.
    assert abs(x - 910) <= 10 and abs(y - 492) <= 10
