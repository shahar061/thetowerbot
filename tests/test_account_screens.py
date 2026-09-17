"""Recorded English v29.0.1 and v29.0.2 account panels, native 1080×2400.

Stats PNG/OCR captured September 5, 2026, 08:46:51 UTC (summary) and
08:51:55 UTC (tiers), reviewed before inclusion. PNG SHA-256 values:
summary cdcc34514b3e6a9d42d22049e294883f5667e2f079f5741f6119f7567cb29385;
tiers 25ddf0b0f27c5ee2f9e57d8170d3a04a3f5a9d1efad5f272f4fd4523e26ce44e.
JSON retains OCR confidence/native bounds; missing zeros are not reconstructed.
settings_safe contains only safe SETTINGS/Stats/version OCR tokens; the matching
unredacted image and its other OCR are excluded because they carry an account
identifier. settings_redacted is a later capture of the same panel with that
identifier painted out of the image, so its PNG is kept and read. Aggregate
history and tier rows establish neither upgrade levels nor unlocks. Runtime frame
digests hash decoded BGR pixels, distinct from these PNG file digests. The
progressed v29.0.2 summary/tiers pair is one live Stats visit before/after a
scroll; its Settings image is withheld because it contains an account ID.
"""

from dataclasses import replace
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

import config
import ocr
import screens
from account_state import AccountState, AccountRepository
from strategy import Shopping

FIXTURES = Path(__file__).parent / 'fixtures' / 'account_screens'


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    return tuple(ocr.TextBox(b['text'], b['confidence'], config.Rect(*b['rect']))
                 for b in json.loads((FIXTURES / f'{name}.json').read_text()))


def frame(name: str = 'stats_summary') -> Any:
    return cv2.imread(str(FIXTURES / f'{name}.png'))


def parse(name: str, boxes: tuple[ocr.TextBox, ...] | None = None, **kwargs: Any) -> Any:
    import account_screens
    return account_screens.parse_frame(frame(name), recorded(name) if boxes is None else boxes,
                                       now=123., **kwargs)


def test_summary_preserves_raw_unknown_and_missing_values() -> None:
    result = parse('stats_summary')
    assert result.screen_id == 'account.stats.summary'
    fields = {f.key: f for f in result.fields}
    assert fields['coins_earned'].raw_value == '1.86K'
    assert fields['workshop_upgrades'].raw_value == '7'
    assert fields['cells_earned_per_hour'].status == 'insufficient_data'
    assert fields['cells_earned_per_hour'].raw_value == 'Need More Data'
    assert fields['interest_earned'].status == 'unreadable'
    assert fields['interest_earned'].raw_value is None
    assert result.frame_width == 1080 and result.frame_height == 2400
    assert len(result.frame_digest) == 64
    assert not result.tiers
    assert parse('stats_summary', tuple(reversed(recorded('stats_summary')))) == result


def test_tiers_match_each_column_without_filling_missing_cells() -> None:
    result = parse('stats_tiers')
    assert result.screen_id == 'account.stats.tiers'
    assert [r.tier for r in result.tiers] == list(range(1, 25))
    assert [result.tiers[0].wave.raw_value, result.tiers[0].coins.raw_value,
            result.tiers[0].cells.raw_value] == ['10', '43', '0']
    assert result.tiers[1].cells.status == 'unreadable'  # OCR confidence below .90
    assert result.tiers[3].wave.raw_value is None
    assert parse('stats_tiers', tuple(reversed(recorded('stats_tiers')))) == result


def test_duplicate_value_and_low_confidence_are_unreadable() -> None:
    boxes = recorded('stats_summary')
    value = next(b for b in boxes if b.text == '1.86K')
    for duplicate, altered in (
        (True, boxes + (value,)),
        (False, tuple(replace(b, confidence=.89) if b == value else b for b in boxes)),
    ):
        fields = {f.key: f for f in parse('stats_summary', altered).fields}
        assert fields['coins_earned'].status == 'unreadable'
        assert fields['coins_earned'].raw_value == (None if duplicate else '1.86K')
        assert fields['coins_earned'].confidence == (0. if duplicate else .89)
        assert (fields['coins_earned'].rect is None) == duplicate


@pytest.mark.parametrize('name,heading', [('stats_summary', 'STATS'), ('stats_tiers', 'Coins')])
def test_ambiguous_headings_rejected(name: str, heading: str) -> None:
    boxes = recorded(name)
    assert parse(name, boxes + (next(b for b in boxes if b.text == heading),)) is None


def test_geometry_locale_and_title_required() -> None:
    import account_screens
    assert parse('stats_summary', locale='fr') is None
    assert account_screens.parse_frame(np.zeros((1200, 540, 3), dtype=np.uint8),
                                      recorded('stats_summary'), now=123.) is None
    assert parse('stats_summary', tuple(b for b in recorded('stats_summary') if b.text != 'STATS')) is None


def test_settings_exposes_only_safe_version() -> None:
    import account_screens
    boxes = recorded('settings_safe') + (ocr.TextBox('PRIVATE-ACCOUNT-IDENTIFIER', .99, config.Rect(300, 1400, 300, 30)),)
    result = account_screens.parse_frame(frame(), boxes, now=123.)
    assert result.screen_id == 'account.settings'
    assert [(f.key, f.raw_value) for f in result.fields] == [('game_version', 'v29.0.1')]
    assert 'PRIVATE' not in str(result)


def test_readings_never_promote_account_facts_or_persist(tmp_path: Path) -> None:
    state = AccountState(AccountRepository(tmp_path / 'account.db'))
    state.screen_readings.observe(parse('stats_summary'))
    state.screen_readings.observe(parse('stats_tiers'))
    snapshot = state.snapshot()
    assert snapshot['revision'] is None
    assert snapshot['unknown_state']['workshop_levels'] is None
    assert len(snapshot['screen_readings']['readings']) == 2
    state.screen_readings.observe(None)
    assert state.snapshot()['screen_readings']['current_screen_id'] is None
    assert len(state.snapshot()['screen_readings']['readings']) == 2
    assert AccountState(AccountRepository(tmp_path / 'account.db')).snapshot()['screen_readings']['readings'] == []


@pytest.mark.parametrize('paused', [True, False])
@pytest.mark.parametrize('active_visit', [True, False])
def test_runtime_reads_without_any_action_even_when_main_menu_matches(
    bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch, paused: bool, active_visit: bool,
) -> None:
    from shopping import Step
    bot = bot_on_main_menu(Shopping(enabled=True))
    bot.account_state = AccountState()
    bot._screen = frame()
    bot.controls.paused = paused
    if active_visit:
        bot.shopping._step = Step.RETURN
    monkeypatch.setattr(screens, 'classify', lambda *_: screens.ScreenReading(screens.ScreenState.MAIN_MENU, 1., {'MAIN_MENU': 1.}))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: recorded('stats_summary'))
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail('account modal reached an action path')
    monkeypatch.setattr(bot, '_manage_speed', forbidden)
    monkeypatch.setattr(bot.shopping, 'begin', forbidden)
    monkeypatch.setattr(bot.shopping, 'advance', forbidden)
    monkeypatch.setattr(bot.navigator, 'maybe_navigate', forbidden)
    assert not bot.run_once()
    assert not bot.device.taps
    assert bot.account_state.snapshot()['screen_readings']['current_screen_id'] == 'account.stats.summary'


def test_runtime_ocr_error_blocks_and_preserves_only_historical_readings(
    bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_on_main_menu(Shopping(enabled=True))
    bot.account_state = AccountState()
    bot.account_state.screen_readings.observe(parse('stats_summary'))
    bot._screen = frame()
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError('secret exception contents')
    monkeypatch.setattr(ocr, 'read', fail)
    assert not bot.run_once()
    result = bot.account_state.snapshot()['screen_readings']
    assert result['current_screen_id'] is None and result['error']
    assert 'secret' not in result['error']
    assert len(result['readings']) == 1 and not bot.device.taps


def test_strict_ocr_error_is_visible_without_changing_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ocr, '_engine_or_none', lambda: None)
    assert ocr.read(frame()) == ()
    with pytest.raises(RuntimeError):
        ocr.read(frame(), strict=True)


@pytest.mark.parametrize('duplicate', [False, True])
def test_candidate_modal_blocks_even_when_no_reading_can_be_accepted(
    monkeypatch: pytest.MonkeyPatch, duplicate: bool,
) -> None:
    from account_screens import ScreenReadings
    boxes = recorded('stats_summary')
    title = next(b for b in boxes if b.text == 'STATS')
    invalid = boxes + (title,) if duplicate else tuple(replace(b, confidence=.5) if b == title else b for b in boxes)
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: invalid)
    state = ScreenReadings()
    assert state.scan(frame()) is True
    assert state.snapshot()['current_screen_id'] is None
    assert state.snapshot()['error']


def test_title_prefilter_can_retain_low_confidence_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def engine(_: Any) -> Any:
        return [([[0, 0], [100, 0], [100, 40], [0, 40]], 'STATS', .5)], None
    monkeypatch.setattr(ocr, '_engine_or_none', lambda: engine)
    assert ocr.read(frame()) == ()
    assert ocr.read(frame(), strict=True, min_confidence=0.)[0].text == 'STATS'


def test_home_clears_current_but_retains_timestamped_history(monkeypatch: pytest.MonkeyPatch) -> None:
    from account_screens import ScreenReadings
    state = ScreenReadings()
    state.observe(parse('stats_summary'))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    assert not state.scan(frame())
    snapshot = state.snapshot()
    assert snapshot['current_screen_id'] is None and snapshot['error'] is None
    assert snapshot['readings'][0]['observed_at'] == 123.


def test_api_exposes_readings_and_explains_guard() -> None:
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from web.app import create_app
    from sinks.state import BotState
    from sinks.sse import SseSink
    from events import EventBus
    from control import Controls
    from strategy import Strategy
    from autopilot import AutopilotState
    account = AccountState()
    account.screen_readings.observe(parse('stats_summary'))
    runner = SimpleNamespace(account_state=account, autopilot_state=AutopilotState(),
                             status=lambda: {'running': True, 'since': 123., 'error': None})
    app = create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
                     runner=runner, account_state=account, controls=Controls(strategy=Strategy.from_config()))
    with TestClient(app) as client:
        payload = client.get('/api/account').json()
        assert payload['revision'] is None
        assert payload['screen_readings']['current_screen_id'] == 'account.stats.summary'
        readiness = client.get('/api/status').json()['runtime']['readiness']
        assert readiness['mode'] == 'observing'
        assert any('account' in reason.lower() for reason in readiness['reasons'])


def test_duplicate_tier_row_is_rejected_and_duplicate_cell_is_unknown() -> None:
    boxes = recorded('stats_tiers')
    tier = next(b for b in boxes if b.text == 'Tier 1')
    assert parse('stats_tiers', boxes + (tier,)) is None
    cell = next(b for b in boxes if b.text == '43')
    result = parse('stats_tiers', boxes + (cell,))
    assert result.tiers[0].coins.status == 'unreadable'
    assert result.tiers[0].wave.raw_value == '10'


def test_snapshot_is_detached_and_reset_keeps_only_historical_readings() -> None:
    from account_screens import ScreenReadings
    state = ScreenReadings()
    state.observe(parse('stats_summary'))
    snapshot = state.snapshot()
    snapshot['readings'][0]['screen_id'] = 'tampered'
    state.reset_current()
    actual = state.snapshot()
    assert actual['current_screen_id'] is None
    assert actual['readings'][0]['screen_id'] == 'account.stats.summary'


@pytest.mark.parametrize('name,first_label,second_label,values', [
    ('stats_summary', 'Coins Earned', 'Cash Earned', ('1.86K',)),
    ('stats_tiers', 'Tier 1', 'Tier 2', ('10', '43')),
])
@pytest.mark.parametrize('offset', [0, 30])
def test_overlapping_row_anchors_cannot_claim_the_same_values(
    name: str, first_label: str, second_label: str, values: tuple[str, ...], offset: int,
) -> None:
    boxes = recorded(name)
    first = next(b for b in boxes if b.text == first_label)
    altered = tuple(
        replace(b, rect=b.rect._replace(y=first.rect.y + offset)) if b.text == second_label
        else replace(b, rect=b.rect._replace(y=b.rect.y + offset // 2)) if b.text in values
        else b for b in boxes)
    assert parse(name, altered) is None


@pytest.mark.parametrize('confidence', [float('inf'), 1.1])
def test_invalid_confidence_cannot_be_hidden_by_label_confidence(confidence: float) -> None:
    boxes = tuple(replace(b, confidence=confidence) if b.text == '1.86K' else b
                  for b in recorded('stats_summary'))
    field = next(f for f in parse('stats_summary', boxes).fields if f.key == 'coins_earned')
    assert field.status == 'unreadable'
    assert field.raw_value is None


# -- Collect stats transaction ----------------------------------------------
# Home -> Settings -> Stats -> Home, read-only. The recorded Settings capture
# is withheld (it shows an account identifier), so these drive the recorded
# Settings OCR over a Stats frame: the reader identifies a panel from its OCR,
# never from the pixels underneath, and the transaction only ever tests the
# identity the reader published for that frame.
def drive(bot: Any, monkeypatch: pytest.MonkeyPatch, script: list[tuple[str, Any]]) -> None:
    """Run one scan per (fixture, ocr boxes) pair in `script`."""
    for name, boxes in script:
        bot._screen = any_frame(name)
        monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: boxes)
        bot.run_once()


def settings_boxes(extra: tuple[ocr.TextBox, ...] = ()) -> tuple[ocr.TextBox, ...]:
    return recorded('settings_safe') + extra


def home() -> tuple[str, tuple[ocr.TextBox, ...]]:
    return ('main_menu', ())


def any_frame(name: str) -> Any:
    """An account fixture or a menu fixture, by name."""
    path = FIXTURES / f'{name}.png'
    if not path.exists():
        path = FIXTURES.parent / f'{name}.png'
    image = cv2.imread(str(path))
    assert image is not None, f'missing fixture: {name}.png'
    return image


@pytest.fixture
def collecting(bot_on_main_menu: Any) -> Any:
    """An armed transaction with jitter off, so tap coordinates are exact.

    The transaction taps through the shared jitter helpers like every other
    tap path; zeroing the radius here is what lets the coordinates below be
    recomputed from the fixtures by hand. That the helpers are reached at all
    is pinned separately, by test_transaction_taps_go_through_the_shared_jitter.
    """
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.account_state = AccountState()
    bot.controls.apply({'tap_jitter_px': 0, 'tap_delay': 0})
    bot.collection.request(now=100.)
    return bot


def test_collect_stats_walks_home_settings_stats_and_home_read_only(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Success: four located taps, one per panel transition, and a reading."""
    drive(collecting, monkeypatch, [
        home(),                                   # tap the settings control
        ('stats_summary', settings_boxes()),      # Settings seen: tap Stats
        ('stats_summary', recorded('stats_summary')),  # Stats read: close Stats
        ('settings_redacted', settings_boxes()),       # close Settings
        # Two home frames: the tracker debounces, so the first frame after
        # the panel closes has not confirmed the main menu again yet.
        home(), home(),
    ])
    result = collecting.collection.snapshot()
    assert result['status'] == 'completed'
    assert result['result']['reason'] == 'collected'
    assert result['result']['screen_id'] == 'account.stats.summary'
    assert result['trail'] == ['open_settings', 'open_stats', 'collect',
                               'close_settings', 'confirm_home']
    # The Stats control is tapped where THIS frame's OCR put it (664+114//2,
    # 826+42//2); the other three are template matches on recorded frames.
    assert collecting.device.taps == [(1029, 373), (721, 847), (904, 508), (904, 508)]
    readings = collecting.account_state.snapshot()['screen_readings']['readings']
    assert [r['screen_id'] for r in readings] == ['account.settings', 'account.stats.summary']
    assert not collecting.collection.active


def test_collect_stats_closes_settings_after_stats_before_confirming_home(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    drive(collecting, monkeypatch, [
        home(),
        ('settings_redacted', settings_boxes()),
        ('stats_summary', recorded('stats_summary')),
        ('settings_redacted', settings_boxes()),
        home(), home(),
    ])
    assert collecting.device.taps == [
        (1029, 373), (721, 847), (904, 508), (904, 508),
    ]
    assert collecting.collection.snapshot()['status'] == 'completed'


def test_collect_stats_stops_after_one_tap_when_settings_never_opens(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry budget: the step waits, then fails closed without tapping again.

    Exactly as many frames as the transaction owns: the scan after it stops
    belongs to ordinary automation again, which is free to tap BATTLE.
    """
    drive(collecting, monkeypatch, [home()] * 8)
    result = collecting.collection.snapshot()
    assert result['status'] == 'failed'
    assert result['result']['reason'] == 'settings_not_reached'
    assert result['result']['screen_id'] is None
    assert collecting.device.taps == [(1029, 373)]


def test_collect_stats_refuses_an_ambiguous_stats_control_without_tapping(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ambiguous: two Stats candidates are not a target to choose between."""
    duplicate = next(b for b in recorded('settings_safe') if b.text == 'Stats')
    drive(collecting, monkeypatch, [
        home(),
        ('stats_summary', settings_boxes((duplicate,))),
    ])
    result = collecting.collection.snapshot()
    assert result['result']['reason'] == 'stats_control_ambiguous'
    assert collecting.device.taps == [(1029, 373)]


def test_collect_stats_takes_no_action_from_a_screen_it_cannot_confirm(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No-action failure: an unconfirmed home never yields a first tap."""
    collecting.tracker.state = screens.ScreenState.UNKNOWN
    monkeypatch.setattr(screens, 'classify', lambda *_: screens.ScreenReading(
        screens.ScreenState.UNKNOWN, 1., {'MAIN_MENU': 1.}))
    drive(collecting, monkeypatch, [home()] * 8)
    result = collecting.collection.snapshot()
    assert result['status'] == 'failed'
    assert result['result']['reason'] == 'home_not_confirmed'
    assert collecting.device.taps == []


def test_collect_stats_fails_closed_when_the_reader_cannot_read_the_panel(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable is not absent: an OCR error stops the transaction at once."""
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError('engine gone')
    drive(collecting, monkeypatch, [home()])
    monkeypatch.setattr(ocr, 'read', fail)
    collecting._screen = any_frame('stats_summary')
    collecting.run_once()
    result = collecting.collection.snapshot()
    assert result['result']['reason'] == 'settings_unreadable'
    assert 'engine gone' not in result['result']['detail']
    assert collecting.device.taps == [(1029, 373)]


def test_an_idle_transaction_leaves_the_passive_guard_exactly_as_it_was(
    bot_on_main_menu: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = bot_on_main_menu(Shopping(enabled=False))
    bot.account_state = AccountState()
    drive(bot, monkeypatch, [('stats_summary', recorded('stats_summary'))])
    assert bot.device.taps == []
    assert bot.collection.snapshot()['status'] == 'idle'
    assert bot.account_state.snapshot()['screen_readings']['current_screen_id'] == 'account.stats.summary'


def test_control_targets_keep_absent_ambiguous_and_unreadable_apart() -> None:
    from account_screens import control_targets
    boxes = recorded('settings_safe')
    stats = next(b for b in boxes if b.text == 'Stats')
    assert control_targets('account.settings', boxes)['stats'].status == 'located'
    assert control_targets('account.settings', boxes + (stats,))['stats'].status == 'ambiguous'
    assert control_targets('account.settings', tuple(
        replace(b, confidence=.5) if b == stats else b for b in boxes))['stats'].status == 'unreadable'
    assert control_targets('account.settings', ())['stats'].status == 'absent'
    # Tap targets are navigation evidence for one frame, never account facts.
    assert control_targets('account.stats.summary', boxes) == {}


def test_a_missing_or_ambiguous_template_is_never_tapped() -> None:
    import numpy as np
    from account_collection import locate_control
    blank = np.zeros((2400, 1080, 3), dtype=np.uint8)
    assert locate_control(blank, None, 'settings_control').status == 'unusable'
    assert locate_control(blank, np.zeros((40, 40, 3), dtype=np.uint8),
                          'settings_control').status == 'ambiguous'
    assert locate_control(any_frame('menu_main'), np.full((40, 40, 3), 7, dtype=np.uint8),
                          'settings_control').status == 'absent'
    # A template larger than the frame, and a frame that is not the one every
    # coordinate in this repo was measured at: both unusable, never a tap.
    assert locate_control(blank, np.zeros((2500, 40, 3), dtype=np.uint8),
                          'settings_control').status == 'unusable'
    assert locate_control(np.zeros((1200, 540, 3), dtype=np.uint8),
                          np.zeros((40, 40, 3), dtype=np.uint8),
                          'settings_control').status == 'unusable'


def test_an_unexamined_frame_is_never_read_as_a_clear_main_menu(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resized emulator is unknown geometry, not an empty menu.

    The screen tracker is debounced, so it still says MAIN_MENU while the
    account reader has looked at nothing at all. That pair must not become
    the precondition for the one tap made before a panel identity exists.
    """
    import numpy as np
    monkeypatch.setattr(screens, 'classify', lambda *_: screens.ScreenReading(
        screens.ScreenState.MAIN_MENU, 1., {'MAIN_MENU': 1.}))
    monkeypatch.setattr(ocr, 'read', lambda *args, **kwargs: ())
    for _ in range(8):
        collecting._screen = np.zeros((1200, 540, 3), dtype=np.uint8)
        collecting.run_once()
    evidence = collecting.account_state.screen_readings.current_evidence()
    assert evidence == {'screen_id': None, 'error': None, 'scanned': False, 'controls': {}}
    assert collecting.collection.snapshot()['result']['reason'] == 'home_not_confirmed'
    assert collecting.device.taps == []


def test_a_scanned_clear_menu_is_distinguishable_from_one_never_read() -> None:
    from account_screens import ScreenReadings
    state = ScreenReadings()
    assert state.current_evidence()['scanned'] is False
    state.observe(parse('stats_summary'))
    assert state.current_evidence()['scanned'] is True
    state.reset_current()
    assert state.current_evidence()['scanned'] is False


def test_pausing_mid_walk_ends_the_transaction_before_its_next_tap(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pause is the operator's stop control; it must reach the one path that
    taps while the account guard holds every other action."""
    drive(collecting, monkeypatch, [home()])
    assert collecting.device.taps == [(1029, 373)]
    collecting.controls.apply({'paused': True})
    # The very frames that would otherwise have produced the remaining two taps.
    drive(collecting, monkeypatch, [
        ('stats_summary', settings_boxes()),
        ('stats_summary', recorded('stats_summary')),
    ])
    result = collecting.collection.snapshot()
    assert result['status'] == 'failed' and result['result']['reason'] == 'paused'
    assert collecting.device.taps == [(1029, 373)]


def test_every_transaction_tap_is_published_and_drawn(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tap nobody can find afterwards is the failure being guarded against."""
    from frames import FrameBuffer
    import events as events_module
    collecting.frames = FrameBuffer()
    drive(collecting, monkeypatch, [home()])
    # The scan that tapped leaves a crosshair on the device view, not a blank
    # overlay: it is what an operator opens to see where the bot touched.
    assert collecting.frames.boxes() == [{
        'name': 'collect_stats:settings_control', 'x': 1000, 'y': 351, 'w': 58, 'h': 44,
        'tap_x': 1029, 'tap_y': 373, 'score': pytest.approx(.994, abs=.01), 'tapped': True,
    }]
    drive(collecting, monkeypatch, [
        ('stats_summary', settings_boxes()),
        ('stats_summary', recorded('stats_summary')),
        home(), home(),
    ])
    published = collecting.bus.published
    taps = [e for e in published if isinstance(e, events_module.Tapped)]
    assert [(e.action, e.x, e.y) for e in taps] == [
        ('collect_stats:open_settings', 1029, 373),
        ('collect_stats:open_stats', 721, 847),
        ('collect_stats:collect', 904, 508),
    ]
    assert all(e.score >= .9 for e in taps)
    # The scans that tapped do not also claim actions were held.
    held = [e for e in published if isinstance(e, events_module.Skipped)
            and e.reason == 'collect_stats_transaction']
    assert len(held) == 2
    assert collecting.device.taps == [(1029, 373), (721, 847), (904, 508)]


def test_transaction_taps_go_through_the_shared_jitter(
    collecting: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jitter
    seen: list[tuple[int, int, float]] = []
    real = jitter.point
    monkeypatch.setattr('account_collection.jitter.point',
                        lambda x, y, radius, *a: seen.append((x, y, radius)) or real(x, y, radius))
    collecting.controls.apply({'tap_jitter_px': 8, 'tap_delay': 0})
    drive(collecting, monkeypatch, [home()])
    assert seen == [(1029, 373, 8)]
    tapped = collecting.device.taps[0]
    assert abs(tapped[0] - 1029) <= 8 * 4 and abs(tapped[1] - 373) <= 8 * 4


# --- The nested Settings -> Stats menu --------------------------------------
#
# settings_redacted is the first Settings *image* that may be kept: the account
# identifier is painted out of the capture itself, so the panel's geometry can
# be checked rather than only its safe OCR tokens.
#
# stats_summary_early and stats_tiers_early were recorded later than the
# originals and parse to exactly the same values. They are deliberately NOT
# treated here as a second unlock stage - screen_discovery's stage table
# excludes them for that reason - but they do show the reader returning the
# same reading from a separate capture of the same panel.

@pytest.mark.parametrize('name,screen_id', [
    ('stats_summary_early', 'account.stats.summary'),
    ('stats_tiers_early', 'account.stats.tiers'),
    ('settings_redacted', 'account.settings'),
])
def test_each_recorded_account_panel_resolves_its_own_screen_id(
    name: str, screen_id: str,
) -> None:
    assert parse(name).screen_id == screen_id


@pytest.mark.parametrize('later,original', [
    ('stats_summary_early', 'stats_summary'),
    ('stats_tiers_early', 'stats_tiers'),
])
def test_a_separate_capture_of_one_panel_reads_the_same_values(
    later: str, original: str,
) -> None:
    """Same panel, different capture, same reading - and no value invented.

    Asserting the equality is what keeps these fixtures from being mistaken
    for a second unlock stage somewhere down the line: if a future capture
    really did come from a younger account, this test would fail and say so.
    """
    first, second = parse(original), parse(later)
    assert {f.key: f.raw_value for f in first.fields} == {f.key: f.raw_value for f in second.fields}
    assert len(first.tiers) == len(second.tiers)
    assert all(f.raw_value is not None or f.status != 'observed' for f in second.fields)


def test_the_early_summary_keeps_a_missing_value_missing() -> None:
    fields = {f.key: f for f in parse('stats_summary_early').fields}
    assert fields['cells_earned_per_hour'].status == 'insufficient_data'
    assert fields['cells_earned_per_hour'].raw_value == 'Need More Data'


def test_the_tier_table_is_read_without_row_positions() -> None:
    result = parse('stats_tiers_early')
    assert result.tiers
    assert parse('stats_tiers_early', tuple(reversed(recorded('stats_tiers_early')))) == result


def test_progressed_tier_history_keeps_large_coins_and_literal_zero_distinct() -> None:
    summary = parse('stats_summary_progressed')
    table = parse('stats_tiers_progressed')
    assert summary.screen_id == 'account.stats.summary' and not summary.tiers
    assert table.screen_id == 'account.stats.tiers'
    assert len(table.tiers) == 24
    assert table.tiers[0].wave.raw_value == '10088'
    assert table.tiers[0].coins.raw_value == '109.71B'
    assert table.tiers[14].wave.raw_value == '46'
    assert table.tiers[15].wave.raw_value == '0'
    assert table.tiers[15].wave.status == 'observed'


def test_the_stats_control_exists_only_on_the_settings_panel() -> None:
    """The nested menu, stated as the two facts that keep it safe.

    Settings offers a way into Stats; a Stats panel does not offer a way into
    itself. 'absent' is the honest answer on the inner screens, and it is not
    the same answer as 'ambiguous' or 'unreadable' - none of which may become
    a tap.
    """
    import account_screens
    located = account_screens.control_targets('account.settings', recorded('settings_redacted'))
    assert located['stats'].status == 'located'
    assert located['stats'].point is not None
    for name in ('stats_summary_early', 'stats_tiers_early'):
        inner = account_screens.control_targets('account.settings', recorded(name))
        assert inner['stats'].status == 'absent'
        assert inner['stats'].point is None


def test_the_redacted_settings_capture_carries_no_account_identifier() -> None:
    """The reason this image may be kept at all, asserted rather than assumed."""
    texts = {b.text.strip() for b in recorded('settings_redacted')}
    assert 'Stats' in texts
    assert not any(t.startswith('PRIVATE') for t in texts)
