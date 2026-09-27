"""Completed work and scoped capability progress, without running the game."""

from __future__ import annotations

import json
import threading
import time as real_time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import ocr
import screens
import tower_bot
from fleet.identity import Attempt
from runtime_progress import ProgressRecorder
from strategy import Shopping
from supervisor import DeviceSupervisor, RecoveryState
from tests.conftest import _shopping_bot
from tests.test_stall_watchdog import stalled_bot


class Clock:
    def __init__(self) -> None:
        self.wall = 1000.0
        self.mono = 50.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def recorder(path: Path, clock: Clock) -> ProgressRecorder:
    return ProgressRecorder(path, attempt=Attempt.new('worker-a', 'endpoint-a',
                            'lease-a', 'attempt-a'), account_id='account-a',
                            boot_id='boot-a', pid=1234,
                            clock=lambda: clock.wall, monotonic=lambda: clock.mono)


def test_scan_completes_only_after_explicit_completion_and_is_scoped(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    with pytest.raises(RuntimeError), progress.phase('ocr_inference', 2):
        raise RuntimeError('interrupted')
    assert progress.snapshot()['scan_sequence'] == 0
    assert progress.snapshot()['phase'] == 'ocr_inference'
    progress.complete_scan(7, 'frame-1')
    saved = json.loads((tmp_path / 'heartbeat.json').read_text())
    assert saved['scan_sequence'] == 1
    assert saved['run_id'] == 7
    assert saved['frame_generation'] == 1
    assert saved['generation'] == progress.attempt.generation
    assert saved['boot_id'] == 'boot-a'
    assert saved['account_id'] == 'account-a'


def test_observed_run_uses_original_capture_time_and_clock_epoch(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    with progress.phase('capture', 20):
        clock.advance(1)
        progress.observe_capture()
    captured_at = clock.wall
    clock.advance(2)
    progress.observe_run(7, wave=31, game_speed=2.5, observed_at=clock.wall)
    progress.complete_scan(7, 'frame-1')
    saved = progress.snapshot()
    assert saved['run_observation'] == {
        'run_id': 7, 'wave': 31, 'game_speed': 2.5,
        'observed_at_utc': captured_at, 'clock_epoch': 0}
    with progress.phase('capture', 20):
        progress.observe_capture()
    progress.observe_run(7, wave=32, game_speed=None, observed_at=captured_at - 1)
    assert progress.snapshot()['run_observation']['wave'] == 31
    clock.wall += 3600
    with progress.phase('capture', 20):
        pass
    assert progress.snapshot()['clock_epoch'] == 1
    assert progress.snapshot()['run_observation']['clock_epoch'] == 0
    progress.observe_run(7, wave=32, game_speed=None, observed_at=clock.wall)
    assert progress.snapshot()['run_observation']['wave'] == 31


def test_initial_startup_remains_timed_until_first_completed_scan(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    assert progress.snapshot()['phase'] == 'startup'
    clock.advance(61)
    assert progress.health()['status'] == 'blocked_scan'
    with progress.phase('scan', 90):
        progress.complete_scan(1, 'fresh-frame')
    assert progress.snapshot()['phase'] == 'idle'


def test_expected_wait_expires_and_clock_jump_needs_new_observation(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    progress.complete_scan(1, 'frame-1')
    progress.wait('funds', 'wallet reaches 20', clock.wall + 30)
    assert progress.health()['status'] == 'expected_wait'
    clock.advance(31)
    assert progress.health()['status'] == 'wait_expired'
    progress.wait('cooldown', 'next check', clock.wall + 30)
    clock.wall += 3600
    assert progress.health()['status'] == 'observation_required'
    progress.complete_scan(1, 'frame-2')
    assert progress.health()['status'] == 'observation_required'
    with progress.phase('capture', 20):
        pass
    progress.observe_capture()
    progress.complete_scan(1, 'frame-3')
    assert progress.health()['status'] == 'healthy'


def test_animated_frame_does_not_hide_starved_capability(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    progress.observe_capability('workshop', 'actionable_unknown', 'unreadable', 20)
    assert progress.snapshot()['capabilities']['workshop']['observation_due'] is True
    assert json.loads(progress.path.read_text())['capabilities']['workshop']['observation_due'] is True
    for wave in (10, 11, 12):
        clock.advance(10)
        progress.meaningful_progress('wave', str(wave))
        progress.complete_scan(1, f'animated-{wave}')
    assert progress.health()['status'] == 'healthy'
    assert progress.health()['capabilities']['workshop']['status'] == 'starved'
    assert progress.health()['capabilities']['workshop']['observation_due']
    progress.observe_capability('workshop', 'progress', 'purchase-confirmed', 20)
    assert progress.health()['capabilities']['workshop']['status'] == 'healthy'
    assert progress.snapshot()['capabilities']['workshop']['observation_due'] is False
    assert json.loads(progress.path.read_text())['capabilities']['workshop']['observation_due'] is False


def test_capture_before_clock_jump_cannot_refresh_observation(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    with progress.phase('capture', 20):
        pass
    progress.observe_capture()
    progress.complete_scan(1, 'old-frame')
    first = progress.snapshot()

    with progress.phase('capture', 20):
        pass
    progress.observe_capture()
    clock.wall += 3600  # Suspend/resume after capture, while OCR is working.
    progress.complete_scan(1, 'pre-resume-frame')
    stale = progress.snapshot()
    assert stale['scan_sequence'] == 2
    assert stale['observation_required'] is True
    assert stale['frame_digest'] == 'old-frame'
    assert stale['frame_generation'] == first['frame_generation']

    with progress.phase('capture', 20):
        pass
    progress.observe_capture()
    progress.complete_scan(1, 'post-resume-frame')
    fresh = progress.snapshot()
    assert fresh['observation_required'] is False
    assert fresh['frame_digest'] == 'post-resume-frame'
    assert fresh['frame_generation'] == first['frame_generation'] + 1


def test_unresolved_settling_wait_keeps_first_deadline_across_scans(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    progress.wait('settling', 'purchase txn-1 reconciled', clock.wall + 15)
    first_deadline = progress.snapshot()['next_wake_utc']
    for number in range(20):
        clock.advance(1)
        progress.wait('settling', 'purchase txn-1 reconciled', clock.wall + 15)
        progress.complete_scan(1, f'frame-{number}')
    assert progress.snapshot()['next_wake_utc'] == first_deadline
    assert progress.health()['status'] == 'wait_expired'
    progress.clear_wait()
    progress.wait('settling', 'purchase txn-2 reconciled', clock.wall + 15)
    assert progress.snapshot()['next_wake_utc'] > first_deadline


def test_manual_pause_wait_can_renew_without_recovery_alarm(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    progress.wait('pause', 'operator resumes', clock.wall + 15)
    clock.advance(20)
    progress.wait('pause', 'operator resumes', clock.wall + 15)
    assert progress.health()['status'] == 'expected_wait'


def test_bot_reconciliation_scans_keep_transaction_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=False)
    bot.progress = progress
    transaction = SimpleNamespace(key='txn-1')
    monkeypatch.setattr(bot.shopping, '_unanswered_transaction', lambda: transaction)
    monkeypatch.setattr(bot.shopping, 'advance', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tower_bot, 'time', SimpleNamespace(
        time=lambda: clock.wall, monotonic=real_time.monotonic,
        strftime=real_time.strftime))
    for _ in range(20):
        bot.run_once()
        clock.advance(1)
    assert progress.snapshot()['wait_condition'] == 'purchase txn-1 reconciled'
    assert progress.health()['status'] == 'wait_expired'


def test_unknown_route_publishes_durable_workshop_observation_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    bot = _shopping_bot('main_menu_resume', state=screens.ScreenState.MAIN_MENU,
                        policy=Shopping(), auto_navigate=False)
    bot.progress = progress
    bot.reroll_progress = SimpleNamespace(
        _route_evaluation=SimpleNamespace(status='unknown'),
        route_error='Workshop price unreadable',
        note_menu_wallet=lambda _wallet: None,
        shopping_policy=lambda base: base,
        resource_evaluation=lambda _coins, _gems: None,
        initial_workshop_due=lambda: False,
        stats_due=lambda: False,
    )
    monkeypatch.setattr(bot, '_offer_cards_intro', lambda: False)
    bot.run_once()
    row = json.loads(progress.path.read_text())['capabilities']['workshop']
    assert row['observation_due'] is True
    assert row['evidence_ref'] == 'Workshop price unreadable'


def test_observed_wave_hook_records_semantic_progress(
    tmp_path: Path, bot_in_run_on: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    bot = bot_in_run_on('in_run_lit')
    bot.progress = progress
    monkeypatch.setattr(type(bot.autopilot), 'has_work', property(lambda _self: True))
    monkeypatch.setattr(tower_bot, 'observe_frame', lambda *_args, **_kwargs: object())
    monkeypatch.setattr(tower_bot, 'frame_combat', lambda *_args: {'wave': 11})
    monkeypatch.setattr(bot.autopilot, 'step', lambda *_args, **_kwargs: False)
    bot.run_once()
    saved = progress.snapshot()
    assert saved['last_semantic_kind'] == 'wave'
    assert saved['last_semantic_evidence_ref'] == f'{bot.runs.current_id}:11'


def test_repeated_unknown_observations_and_noisy_frames_do_not_reset_deadline(
    tmp_path: Path,
) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    progress.observe_capability('workshop', 'actionable_unknown', 'price-unreadable', 20)
    for frame in ('noise-a', 'noise-b', 'noise-c'):
        clock.advance(10)
        progress.observe_capability('workshop', 'actionable_unknown', 'price-unreadable', 20)
        progress.complete_scan(1, frame)
    workshop = progress.health()['capabilities']['workshop']
    assert workshop['status'] == 'starved'
    assert workshop['since_utc'] == 1000.0
    assert progress.snapshot()['last_semantic_progress_utc'] is None


def test_bot_early_return_completes_a_scan_but_exception_does_not(tmp_path: Path) -> None:
    clock = Clock()
    progress = recorder(tmp_path / 'heartbeat.json', clock)
    bot = stalled_bot(tmp_path)
    bot.progress = progress
    assert bot.run_once() is False
    assert progress.snapshot()['scan_sequence'] == 1
    bot._settle_milestones_attempt = lambda: (_ for _ in ()).throw(RuntimeError('broken'))
    with pytest.raises(RuntimeError, match='broken'):
        bot.run_once()
    assert progress.snapshot()['scan_sequence'] == 1


def test_ocr_lock_and_inference_publish_the_actual_blocked_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    entered = threading.Event()
    release = threading.Event()

    def infer(_screen: np.ndarray) -> tuple[list[object], float]:
        entered.set()
        assert release.wait(2)
        return [], 0.0

    monkeypatch.setattr(ocr, '_engine_or_none', lambda: infer)
    screen = np.zeros((20, 20, 3), dtype=np.uint8)
    worker = threading.Thread(target=lambda: _read_with_progress(progress, screen))
    worker.start()
    try:
        assert entered.wait(2)
        assert progress.snapshot()['phase'] == 'ocr_inference'
        assert progress.snapshot()['scan_sequence'] == 0
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


def _read_with_progress(progress: ProgressRecorder, screen: np.ndarray) -> None:
    with ocr.progress_scope(progress):
        ocr.read(screen, strict=True)


def test_device_input_phase_tracks_real_command(tmp_path: Path) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    entered = threading.Event()
    release = threading.Event()

    class Device:
        def click(self, _x: int, _y: int) -> None:
            entered.set()
            assert release.wait(2)

    supervisor = DeviceSupervisor(
        path=tmp_path / 'device.json', endpoint='endpoint-a',
        connect=Device, expected_account='account-a', progress=progress,
    )
    supervisor._device = Device()
    supervisor._state = RecoveryState.READY
    supervisor._last_digest = 'before'
    supervisor._last_observed_at = supervisor.clock()
    worker = threading.Thread(target=lambda: supervisor.tap(10, 20))
    worker.start()
    try:
        assert entered.wait(2)
        assert progress.snapshot()['phase'] == 'input'
        assert progress.snapshot()['scan_sequence'] == 0
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()


def test_atomic_publish_preserves_previous_record_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    before = progress.path.read_bytes()
    monkeypatch.setattr('runtime_progress.os.replace',
                        lambda *_args: (_ for _ in ()).throw(OSError('replace failed')))
    with pytest.raises(OSError, match='replace failed'):
        progress.meaningful_progress('wave', '12')
    assert progress.path.read_bytes() == before
    assert list(tmp_path.glob('.heartbeat.json.*')) == []


def test_account_binding_cannot_switch_a_live_generation(tmp_path: Path) -> None:
    progress = recorder(tmp_path / 'heartbeat.json', Clock())
    with pytest.raises(ValueError, match='cannot change'):
        progress.bind_account('another-account')
    assert progress.snapshot()['account_id'] == 'account-a'
