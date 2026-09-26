"""The no-progress watchdog: when it trips, what it may tap, and when it pauses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cv2

import config
import events
import ocr
import screens
import stall_watchdog
import telegram_report
from sinks.state import BotState
from strategy import Shopping
from supervisor import RecoveryState, RecoveryStatus
from tests.conftest import _shopping_bot
from stall_watchdog import ESCAPE, PAUSE, StallWatchdog


class Clock:
    def __init__(self) -> None:
        self.now = 1000.

    def __call__(self) -> float:
        return self.now


def box(text: str, x: int = 440, y: int = 1450, w: int = 200, h: int = 50,
        confidence: float = .98) -> Any:
    return ocr.TextBox(text, confidence, config.Rect(x, y, w, h))


def watchdog(clock: Clock | None = None) -> StallWatchdog:
    return StallWatchdog(clock or Clock(), no_effect_limit=3, blocked_limit=60.)


def test_consecutive_no_effect_actions_trip_it() -> None:
    dog = watchdog()
    for _ in range(2):
        dog.note(had_pending=True, reason='action_no_effect')
        assert dog.verdict() is None
    dog.note(had_pending=True, reason='action_no_effect')
    assert dog.verdict() == ESCAPE
    assert dog.reason == 'no_effect_x3'


def test_an_action_that_changes_the_frame_resets_the_count() -> None:
    dog = watchdog()
    for _ in range(2):
        dog.note(had_pending=True, reason='action_no_effect')
    dog.note(had_pending=True, reason='fresh_evidence')
    dog.note(had_pending=True, reason='action_no_effect')
    assert dog.verdict() is None


def test_a_fresh_frame_with_no_action_pending_is_not_progress() -> None:
    dog = watchdog()
    for _ in range(2):
        dog.note(had_pending=True, reason='action_no_effect')
        dog.note(had_pending=False, reason='fresh_evidence')
    dog.note(had_pending=True, reason='action_no_effect')
    assert dog.verdict() == ESCAPE


def test_a_long_unknown_block_trips_it_and_a_short_one_does_not() -> None:
    clock = Clock()
    dog = watchdog(clock)
    dog.note(had_pending=False, reason='unknown_screen')
    clock.now += 59
    dog.note(had_pending=False, reason='unknown_screen')
    assert dog.verdict() is None
    dog.note(had_pending=False, reason='fresh_evidence')
    clock.now += 30
    dog.note(had_pending=False, reason='unknown_screen')
    clock.now += 59
    dog.note(had_pending=False, reason='unknown_screen')
    assert dog.verdict() is None
    clock.now += 1
    dog.note(had_pending=False, reason='unknown_screen')
    assert dog.verdict() == ESCAPE
    assert dog.reason == 'unknown_screen_60s'


def test_escapes_are_bounded_then_it_pauses() -> None:
    dog = watchdog()
    for _ in range(stall_watchdog.MAX_ESCAPES):
        for _ in range(3):
            dog.note(had_pending=True, reason='action_no_effect')
        assert dog.verdict() == ESCAPE
        dog.escaped()
        # The escape changed the frame, but no ordinary action has worked yet.
        dog.note(had_pending=True, reason='fresh_evidence')
    for _ in range(3):
        dog.note(had_pending=True, reason='action_no_effect')
    assert dog.verdict() == PAUSE


def test_ordinary_progress_after_an_escape_ends_the_episode() -> None:
    dog = watchdog()
    for _ in range(3):
        dog.note(had_pending=True, reason='action_no_effect')
    dog.escaped()
    dog.note(had_pending=True, reason='fresh_evidence')   # the escape tap
    dog.note(had_pending=True, reason='fresh_evidence')   # a normal tap
    assert dog.escapes == 0 and dog.verdict() is None


def test_reset_forgets_everything() -> None:
    dog = watchdog()
    for _ in range(3):
        dog.note(had_pending=True, reason='action_no_effect')
    dog.escaped()
    dog.reset()
    assert dog.verdict() is None and dog.escapes == 0


def test_the_escape_button_prefers_claim_over_skip() -> None:
    boxes = (box('Free Ticket', y=835), box('SKIP', x=842, y=272, w=110),
             box('CLAIM', y=1452))
    assert stall_watchdog.escape_button(boxes) == ('CLAIM', (540, 1477))


def test_the_escape_button_reads_other_safe_labels() -> None:
    for label in ('OK', 'Close', 'CONTINUE', 'No Thanks', 'Collect', 'x', '×'):
        found = stall_watchdog.escape_button((box(label),))
        assert found is not None and found[1] == (540, 1475)


def test_no_escape_on_a_frame_that_could_spend() -> None:
    for danger in ('BUY', 'Purchase', 'Upgrade', 'WATCH AD', '$4.99', 'Spend 50 gems'):
        boxes = (box(danger, y=900), box('OK'))
        assert stall_watchdog.escape_button(boxes) is None


def test_no_escape_when_the_button_is_ambiguous_or_untrusted() -> None:
    assert stall_watchdog.escape_button((box('OK'), box('OK', y=1800))) is None
    assert stall_watchdog.escape_button((box('OK', confidence=.5),)) is None
    assert stall_watchdog.escape_button((box('BATTLE'),)) is None
    assert stall_watchdog.escape_button((box('Tap OK to continue'),)) is None


# --- wired into the bot's recovery preflight ------------------------------

FIXTURES = Path(__file__).parent / 'fixtures'


class Supervisor:
    """Always READY; reports a fresh frame with no action pending."""

    current_account = 'ACCOUNT-A'

    def __init__(self) -> None:
        self.screens: list[str] = []

    def status(self) -> RecoveryStatus:
        return RecoveryStatus(RecoveryState.READY, 'fresh_evidence', 0, False, None, None)

    def observe(self, **facts: object) -> RecoveryState:
        self.screens.append(str(facts['screen']))
        return RecoveryState.READY


def stalled_bot(tmp_path: Path, frame: str | None = None) -> Any:
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=True)
    if frame is not None:
        bot._screen = cv2.imread(str(FIXTURES / frame))
    bot.supervisor = Supervisor()
    bot.stall_dir = tmp_path
    for _ in range(config.STALL_NO_EFFECT_LIMIT):
        bot.stall_watchdog.note(had_pending=True, reason='action_no_effect')
    return bot


def stalls(bot: Any) -> list[Any]:
    return [e for e in bot.bus.published if isinstance(e, events.WorkerStalled)]


def test_a_stalled_bot_presses_the_popups_safe_button(tmp_path: Path) -> None:
    bot = stalled_bot(tmp_path, 'menu_free_ticket_offer.png')
    bot.run_once()
    assert bot.supervisor.screens == [stall_watchdog.SCREEN_ID]
    assert bot.device.taps == [(540, 1475)]
    assert [(e.stage, e.button) for e in stalls(bot)] == [('escape', 'CLAIM')]
    assert bot.stall_watchdog.escapes == 1
    assert not bot.controls.snapshot().paused


def test_no_safe_button_pauses_the_bot_and_keeps_the_evidence(tmp_path: Path) -> None:
    bot = stalled_bot(tmp_path)
    bot.run_once()
    assert bot.device.taps == []
    assert bot.controls.snapshot().paused
    [stall] = stalls(bot)
    assert stall.stage == 'paused' and stall.reason.startswith('no_effect_x')
    assert Path(stall.snapshot_path).is_file()
    assert json.loads(Path(stall.snapshot_path).with_suffix('.json').read_text())
    assert any(isinstance(e, events.ControlChanged) and e.source == 'watchdog'
               and e.changed == {'paused': True} for e in bot.bus.published)
    assert bot.stall_watchdog.verdict() is None


def test_escapes_run_out_and_the_bot_pauses(tmp_path: Path) -> None:
    bot = stalled_bot(tmp_path, 'menu_free_ticket_offer.png')
    bot.stall_watchdog.escapes = stall_watchdog.MAX_ESCAPES
    bot.run_once()
    assert bot.device.taps == []
    assert bot.controls.snapshot().paused


def test_a_paused_bot_neither_escapes_nor_counts(tmp_path: Path) -> None:
    bot = stalled_bot(tmp_path, 'menu_free_ticket_offer.png')
    bot.controls.apply({'paused': True})
    bot.run_once()
    assert bot.device.taps == [] and stalls(bot) == []
    assert bot.stall_watchdog.verdict() is None


def test_the_digest_and_dashboard_say_a_worker_stalled() -> None:
    state = BotState()
    state.apply(events.WorkerStalled(reason='no_effect_x5', stage='paused'))
    assert state.snapshot()['stalled'] == 'no_effect_x5'
    state.apply(events.ScanCompleted(screen='MAIN_MENU', duration_ms=1.))
    digest = telegram_report.render_summary(state.snapshot(), paused=True)
    assert digest.splitlines()[1] == 'Stalled: no progress (no_effect_x5); paused for you'
    state.apply(events.ControlChanged(changed={'paused': False}))
    assert state.snapshot()['stalled'] is None


def test_an_unknown_screen_stall_escapes_with_its_reason(tmp_path: Path) -> None:
    clock = Clock()
    bot = stalled_bot(tmp_path, 'menu_free_ticket_offer.png')
    bot.stall_watchdog = StallWatchdog(clock, no_effect_limit=5, blocked_limit=60.)
    bot.stall_watchdog.note(had_pending=False, reason='unknown_screen')
    clock.now += 61
    bot.run_once()
    assert bot.device.taps == [(540, 1475)]
    assert [e.reason for e in stalls(bot)] == ['unknown_screen_61s']
