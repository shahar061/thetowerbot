"""O4 scan integration: real TowerBot + real DeviceSupervisor + real coordinator,
fake hardware and a suspended provider; runner lifecycle over fakes.

No credentials, no network, no device. Every store is a temporary directory.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import cv2
import pytest

import events
import stall_watchdog
from fleet.identity import Attempt
from recovery_coordinator import RecoveryCoordinator
from runtime_progress import ProgressRecorder
from supervisor import DeviceSupervisor
from tests.test_recovery_coordinator import FakeService, configure, grant
from tests.test_runner import runner_parts  # noqa: F401 - shared fixture
from tests.test_stall_watchdog import FIXTURES, box, stalled_bot

KEY = 'sk-or-synthetic-integration'


class Device:
    serial = '127.0.0.1:5555'

    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))


class Harness:
    """One real TowerBot stalled past its deterministic escapes."""

    def __init__(self, tmp_path: Path, fleet: Path, *, worker: str = 'worker-a',
                 account: str = 'account-a', lease: str = 'lease-a',
                 generation: str = 'generation-a', boot: str = 'boot-a') -> None:
        self.fleet = fleet
        self.loads: list[int] = []
        self.bot = bot = stalled_bot(tmp_path)
        self.attempt = Attempt(worker, Device.serial, lease, 'attempt-a', generation, time.time())
        self.progress = ProgressRecorder(tmp_path / worker / 'worker-heartbeat.json',
                                         attempt=self.attempt, account_id=account,
                                         boot_id=boot, pid=4242)
        bot.progress = self.progress
        self.device = Device()
        self.supervisor = DeviceSupervisor(path=tmp_path / 'supervisor.json',
                                           endpoint=Device.serial, connect=lambda: self.device,
                                           expected_account=account)
        self.supervisor.recover()
        self.supervisor.verify_account(account, observed_at=time.time())
        bot.supervisor = self.supervisor
        bot.stall_watchdog.escapes = stall_watchdog.MAX_ESCAPES  # Deterministic exhaustion.
        self.boxes: tuple[Any, ...] = (box('CLOSE'),)
        bot._preflight_boxes = lambda reading, reads: self.boxes

        def refresh() -> Any:  # What production refresh_screen records per capture.
            bot._capture_sequence += 1
            bot._screen_captured_at = time.time()
            bot._screen_captured_monotonic = time.monotonic()
            return bot._screen
        bot.refresh_screen = refresh
        self.status_path = tmp_path / worker / 'recovery-status.json'
        self.recovery = RecoveryCoordinator(fleet, worker=worker, status_path=self.status_path,
                                            key_loader=self._key, service_factory=FakeService)
        bot.recovery = self.recovery
        # A real (empty) purchase journal; its pending cache is refreshed on the
        # storage thread. Without one, pending state is unknown and blocks.
        from transactions import TransactionJournal
        self.journal = TransactionJournal(tmp_path / worker / 'account.db')
        bot.shopping.journal = self.journal
        self.recovery.set_journal(self.journal)
        deadline = time.monotonic() + 2
        while (self.recovery.storage.snapshot() is None
               or self.journal.recovery_snapshot()[1]):
            assert time.monotonic() < deadline
            time.sleep(.01)

    def _key(self) -> str:
        self.loads.append(1)
        return KEY

    def scan(self) -> bool:
        time.sleep(.002)  # Strictly increasing capture times for the supervisor.
        return self.bot.run_once()

    def until(self, predicate: Callable[[], bool], timeout: float = 4.) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.scan()
            if predicate():
                return
            time.sleep(.01)
        raise AssertionError(self.recovery.status())

    def service(self) -> FakeService:
        return FakeService.instances[-1]

    def clear_overlay(self) -> None:
        # Positive evidence: a trusted benign read on a known screen.
        self.boxes = (box('BATTLE', y=1700),)
        self.bot._screen = cv2.imread(str(FIXTURES / 'menu_free_ticket_offer.png'))

    def close(self) -> None:
        self.recovery.close()
        deadline = time.monotonic() + 3
        while not self.recovery.shutdown_complete and time.monotonic() < deadline:
            time.sleep(.01)


@pytest.fixture
def fleet(tmp_path: Path) -> Path:
    FakeService.instances = []
    return tmp_path / 'fleet'


@pytest.fixture
def harness(tmp_path: Path, fleet: Path) -> Any:
    made: list[Harness] = []

    def build(**kwargs: Any) -> Harness:
        made.append(Harness(tmp_path / f'h{len(made)}', fleet, **kwargs))
        return made[-1]
    yield build
    for item in made:
        item.close()


def stalls(bot: Any) -> list[Any]:
    return [e for e in bot.bus.published if isinstance(e, events.WorkerStalled)]


def episodes(fleet: Path) -> list[tuple[Any, ...]]:
    path = fleet / 'recovery-episodes.sqlite3'
    if not path.exists():
        return []
    with sqlite3.connect(path) as db:
        return db.execute('SELECT incident_id, closed_at, outcome FROM recovery_episodes').fetchall()


def to_provider(h: Harness) -> None:
    h.until(lambda: h.recovery.status()['phase'] == 'provider')


# -- default off / startup / shutdown ---------------------------------------

def test_default_off_keeps_watchdog_pause_and_opens_nothing(harness: Any, fleet: Path) -> None:
    h = harness()
    h.scan()
    assert h.bot.controls.snapshot().paused  # Existing deterministic pause, unchanged.
    assert [s.stage for s in stalls(h.bot)] == ['paused']
    assert h.device.taps == [] and episodes(fleet) == [] and h.loads == []
    assert not any(s.requests for s in FakeService.instances)
    assert h.recovery.status()['mode'] == 'off'


def test_missing_key_starts_no_transport_and_pauses_safely(harness: Any, fleet: Path) -> None:
    configure(fleet)  # Shadow for worker-a, but the key source is empty.
    h = harness()
    h._key = lambda: h.loads.append(1) or None
    h.recovery.key_loader = h._key
    h.until(lambda: h.bot.controls.snapshot().paused)
    assert h.loads and not h.service().status().configured
    assert h.device.taps == []
    assert all(not s.requests for s in FakeService.instances)


# -- scans continue while the provider is suspended --------------------------

def test_several_scans_complete_while_the_provider_is_suspended(harness: Any, fleet: Path) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    before = h.progress.snapshot()['scan_sequence']
    for _ in range(8):
        start = time.monotonic()
        assert h.scan() is False  # Recovery owns the lane; no ordinary input.
        assert time.monotonic() - start < 1.
    assert h.progress.snapshot()['scan_sequence'] == before + 8
    assert h.device.taps == [] and not h.bot.controls.snapshot().paused
    assert len(h.service().requests) == 1
    request = h.recovery.request
    scope = request.context.scope
    assert (scope.attempt_generation, scope.lease_id, scope.account_id) == (
        'generation-a', 'lease-a', 'account-a')
    assert scope.strategy_revision == h.bot.controls.recovery_revision
    assert scope.device_command_generation == h.supervisor.command_generation
    assert request.context.candidates[0].target == 'CLOSE'


# -- a valid fresh candidate dispatches once and needs a postcondition ------

def test_valid_fresh_candidate_dispatches_once_through_supervisor(harness: Any,
                                                                   fleet: Path) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    h.service().release(candidate_id='stall:CLOSE')
    h.until(lambda: len(h.device.taps) == 1)
    assert h.device.taps == [(540, 1475)]
    assert h.supervisor.command_generation >= 1
    for _ in range(3):  # Overlay still there: no confirmation, no second input.
        h.scan()
    assert h.device.taps == [(540, 1475)]
    assert h.recovery.status()['phase'] in {'postcondition', 'recording', 'choosing',
                                             'provider'}
    assert h.recovery.status()['last_outcome'] != 'recovered'
    h.clear_overlay()
    h.until(lambda: h.recovery.status()['last_outcome'] == 'recovered')
    assert h.device.taps == [(540, 1475)]
    assert not h.bot.controls.snapshot().paused
    [(incident, closed, outcome)] = episodes(fleet)
    assert outcome == 'recovered' and closed is not None
    assert any(isinstance(e, events.RecoveryStatusChanged) for e in h.bot.bus.published)
    assert KEY not in h.status_path.read_text()


# -- late replies never input -------------------------------------------------

@pytest.mark.parametrize('late', ['pause', 'account', 'lease', 'strategy', 'deadline', 'stop'])
def test_late_reply_never_produces_input(harness: Any, fleet: Path, late: str) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    if late == 'pause':
        h.bot.controls.apply({'paused': True})
    elif late == 'account':
        h.supervisor.current_account = 'account-b'
    elif late == 'lease':
        h.progress._heartbeat.lease_id = 'lease-b'
    elif late == 'strategy':
        h.bot.controls.request('speed_up')
    elif late == 'deadline':
        h.recovery.request = h.recovery.request.model_copy(
            update={'deadline_at_monotonic': time.monotonic() + .01})
        time.sleep(.02)
    elif late == 'stop':
        h.recovery.close()
    h.service().release(candidate_id='stall:CLOSE')
    for _ in range(8):  # Each scan offers the consumed reply's fresh-validation path.
        h.scan()
    assert h.device.taps == []
    if late != 'stop':
        assert h.service().invalidated or late == 'deadline'


# -- restart preserves combined allowances ----------------------------------

def test_restart_preserves_combined_action_and_call_allowance(harness: Any,
                                                               fleet: Path) -> None:
    from recovery_budget import RecoveryBudget
    grant(fleet)
    configure(fleet, mode='assist')
    first = harness()
    to_provider(first)
    incident = first.recovery.status()['incident_id']
    first.service().release(candidate_id='stall:CLOSE')
    first.until(lambda: len(first.device.taps) == 1)
    first.close()  # Crash-like stop in the postcondition window: action stays unresolved.
    # Durable call accounting is owned by the (real) budget: simulate the one call made.
    budget = RecoveryBudget(fleet)
    assert budget.reserve(request_id='call-1', incident_id=incident, day='2026-09-27',
                          maximum_microusd=1)
    second = harness(lease='lease-b', generation='generation-b', boot='boot-b')
    second.until(lambda: second.recovery.status()['blocker'] == 'unresolved'
                 or second.bot.controls.snapshot().paused)
    assert second.device.taps == []
    assert second.recovery.status()['incident_id'] == incident
    assert budget.calls_remaining(incident_id=incident) == 1
    assert all(not s.requests for s in FakeService.instances[1:])


# -- pending purchase blocks without mutation -------------------------------

def test_pending_purchase_blocks_recovery_without_mutation(harness: Any, fleet: Path) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()

    class Journal:
        def recovery_snapshot(self) -> tuple[int, bool]:
            return 7, True
    h.bot._recovery_journal = lambda: Journal().recovery_snapshot()
    h.scan()
    assert h.bot.controls.snapshot().paused  # Existing pause path; recovery never opened.
    assert episodes(fleet) == [] and h.device.taps == []
    assert all(not s.requests for s in FakeService.instances)


# -- blocked storage stays off the scan thread ------------------------------

def test_blocked_storage_stays_off_scan_thread(harness: Any, fleet: Path) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    gate = threading.Event()
    assert h.recovery.storage.submit('blocked', lambda stores: gate.wait(3))
    try:
        for _ in range(6):
            start = time.monotonic()
            h.scan()
            assert time.monotonic() - start < 1.
    finally:
        gate.set()
    assert h.device.taps == []


def test_raising_coordinator_returns_to_watchdog_path(harness: Any, fleet: Path) -> None:
    configure(fleet)
    h = harness()

    def broken(*_: Any, **__: Any) -> bool:
        raise RuntimeError('boom')
    h.recovery.step = broken
    h.scan()
    assert h.bot.controls.snapshot().paused and h.device.taps == []


# -- reconcile tool ------------------------------------------------------------

def test_operator_reconcile_resolves_unknown_with_audit(harness: Any, fleet: Path,
                                                         capsys: Any) -> None:
    from recovery_episodes import EpisodeNamespace, RecoveryEpisodes
    from tools.reconcile_recovery import main
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    h.service().release(candidate_id='stall:CLOSE')
    h.until(lambda: len(h.device.taps) == 1)
    incident = h.recovery.status()['incident_id']
    h.close()
    store = RecoveryEpisodes(fleet)
    namespace = EpisodeNamespace('worker-a', 'account-a')
    [action] = store.get(namespace=namespace, incident_id=incident).unresolved_action_ids
    main(['--fleet-root', str(fleet), 'list'])
    assert incident in capsys.readouterr().out
    base = ['--fleet-root', str(fleet), 'reconcile', '--worker', 'worker-a',
            '--account', 'account-a', '--incident', incident, '--action', action,
            '--operator', 'local-operator', '--evidence', 'overlay gone in recording']
    with pytest.raises(SystemExit):
        main(base[:-2] + ['--evidence', ' '])
    # The crash left an unrecorded (NULL) action, which may be in flight on a
    # live worker: refused until the operator confirms the worker stopped.
    with pytest.raises(SystemExit):
        main(base + ['--outcome', 'confirmed'])
    assert store.reconciliations(incident_id=incident) == ()
    main(base + ['--worker-stopped', '--outcome', 'confirmed'])
    episode = store.get(namespace=namespace, incident_id=incident)
    assert not episode.unresolved_action_ids and episode.closed_at is None
    [row] = store.reconciliations(incident_id=incident)
    assert row[0] == action and row[2:5] == ('confirmed', 'local-operator',
                                            'overlay gone in recording')
    with pytest.raises(SystemExit):
        main(base + ['--outcome', 'no_effect'])  # Known outcomes are never rewritten.


# -- runner lifecycle ------------------------------------------------------------

def _supervised_runner(runner_parts: Any, tmp_path: Path, **kwargs: Any) -> Any:
    from runtime_records import RuntimeRecords
    runner, made, _, _, _ = runner_parts
    runner._device_factory = lambda: type('Device', (), {'serial': '127.0.0.1:5555',
                                                         'click': lambda self, x, y: None})()
    runner._attempt = Attempt.new('worker-a', '127.0.0.1:5555', 'lease-a', 'attempt-a')
    runner._binding_path = tmp_path / f'{runner._attempt.generation}.json'
    runner._supervisor_path = tmp_path / 'supervisor.json'
    runner._runtime_records = RuntimeRecords(tmp_path / 'worker-a' / 'runtime-records.json')
    runner._recovery_root = tmp_path / 'fleet'
    loads: list[int] = []
    runner._recovery_key_loader = lambda: loads.append(1) or KEY
    runner._recovery_service_factory = kwargs.get('service_factory', FakeService)
    original = runner._bot_factory

    def factory(**bot_kwargs: Any) -> Any:
        bot = original(**bot_kwargs)
        bot.recovery = bot_kwargs.get('recovery')
        return bot
    runner._bot_factory = factory
    return runner, made, loads


def test_runner_builds_default_off_coordinator_and_closes_it_on_scan_thread(
        runner_parts: Any, tmp_path: Path) -> None:
    FakeService.instances = []
    runner, made, loads = _supervised_runner(runner_parts, tmp_path)
    runner.start()
    try:
        recovery = made[-1].kwargs['recovery']
        assert isinstance(recovery, RecoveryCoordinator)
        assert recovery.worker == 'worker-a'
        assert recovery.settings.mode == 'off'
    finally:
        runner.stop()
    deadline = time.monotonic() + 3
    while not recovery.shutdown_complete and time.monotonic() < deadline:
        time.sleep(.01)
    assert recovery.shutdown_complete
    assert runner.recovery_shutdown() == {'complete': True, 'failed': False}
    assert loads == []  # Off: the key source is never read.
    runner.start()  # Confirmed shutdown: a fresh coordinator is allowed.
    try:
        assert made[-1].kwargs['recovery'] is not recovery
    finally:
        runner.stop()


def test_runner_retains_unconfirmed_coordinator_and_surfaces_it(runner_parts: Any,
                                                                  tmp_path: Path) -> None:
    runner, made, _ = _supervised_runner(runner_parts, tmp_path)
    _, _, _, seen, _ = runner_parts
    runner.start()
    recovery = made[-1].kwargs['recovery']
    runner.stop()

    class Stuck:
        shutdown_complete = False

        def status(self) -> dict[str, Any]:
            return {'blocker': 'shutdown_failed'}
    runner._recovery = Stuck()
    runner.start()
    try:
        assert 'recovery' not in made[-1].kwargs
        assert isinstance(runner._recovery, Stuck)  # Retained, never replaced.
        assert runner.recovery_shutdown() == {'complete': False, 'failed': True}
        assert any(isinstance(e, events.BotError) and 'recovery_shutdown_pending' in e.message
                   for e in seen)
    finally:
        runner.stop()
    del recovery


def test_unsupervised_runner_never_builds_recovery(runner_parts: Any) -> None:
    runner, made, _, _, _ = runner_parts
    runner.start()
    try:
        assert 'recovery' not in made[-1].kwargs
    finally:
        runner.stop()


# -- Phase 2 fix round 1 regressions -------------------------------------------

def _unknown_stall(h: Harness) -> None:
    """Blank frame read as UNKNOWN, past its escapes, a safe CLOSE visible."""
    import numpy as np
    h.bot._screen = np.zeros_like(h.bot._screen)
    h.bot.stall_watchdog.reset()
    h.bot.stall_watchdog._blocked_since = time.time() - 10_000
    h.bot.stall_watchdog.escapes = stall_watchdog.MAX_ESCAPES


@pytest.mark.parametrize('mode', ['off', 'shadow', 'assist'])
def test_unknown_screen_exhaustion_pauses_or_opens_within_bounded_scans(
        harness: Any, fleet: Path, mode: str) -> None:
    if mode == 'assist':
        grant(fleet)
    if mode != 'off':
        configure(fleet, mode=mode)
    h = harness()
    _unknown_stall(h)
    for _ in range(6):
        h.scan()
        if h.bot.controls.snapshot().paused or episodes(fleet):
            break
    assert h.bot.controls.snapshot().paused or episodes(fleet)
    if mode == 'off':
        assert h.bot.controls.snapshot().paused and episodes(fleet) == []
    else:
        assert episodes(fleet)  # Recovery took the stall; it did not silently clear.
    assert h.device.taps == []


def test_unknown_screen_shadow_stall_reaches_pause(harness: Any, fleet: Path) -> None:
    configure(fleet)
    h = harness()
    _unknown_stall(h)
    h.until(lambda: h.recovery.status()['phase'] == 'provider')
    h.service().release(candidate_id='stall:CLOSE')
    h.until(lambda: h.bot.controls.snapshot().paused)
    assert h.device.taps == []


def test_coordinator_failure_while_owning_lane_falls_back_to_watchdog_pause(
        harness: Any, fleet: Path) -> None:
    configure(fleet)
    h = harness()
    to_provider(h)
    assert h.recovery.owns_lane

    def broken(*_: Any, **__: Any) -> bool:
        raise RuntimeError('boom')
    h.recovery.step = broken
    for _ in range(3):
        h.scan()
        if h.bot.controls.snapshot().paused:
            break
    assert h.bot.controls.snapshot().paused
    assert [s.stage for s in stalls(h.bot)] == ['paused']
    assert h.bot._recovery_failed and h.device.taps == []
    h.bot.controls.apply({'paused': False})
    h.scan()  # Latched off: no coordinator step, the watchdog alone decides.


@pytest.mark.parametrize('after', [
    pytest.param(lambda: (box('CLOSE'), box('Watch ad', y=1300)), id='close-and-watch-ad'),
    pytest.param(lambda: (), id='empty-read'),
    pytest.param(lambda: (box('CLOSE'), box('CLOSE', y=1200)), id='duplicate-close'),
    pytest.param(lambda: (box('CLAIM', y=1300),), id='higher-priority-label'),
    pytest.param(lambda: (box('CLOSE', confidence=.5),), id='untrusted-close'),
])
def test_postcondition_needs_positive_evidence(harness: Any, fleet: Path,
                                               after: Callable[[], tuple[Any, ...]]) -> None:
    grant(fleet)
    configure(fleet, mode='assist')
    h = harness()
    to_provider(h)
    h.service().release(candidate_id='stall:CLOSE')
    h.until(lambda: len(h.device.taps) == 1)
    h.boxes = after()
    h.bot._screen = cv2.imread(str(FIXTURES / 'menu_free_ticket_offer.png'))
    for _ in range(6):
        h.scan()
    assert h.recovery.status()['last_outcome'] != 'recovered'
    assert all(outcome != 'recovered' for _, _, outcome in episodes(fleet))


def test_overlay_absent_is_positive_evidence_only() -> None:
    assert stall_watchdog.overlay_absent((box('BATTLE'),), 'CLOSE')
    assert not stall_watchdog.overlay_absent((), 'CLOSE')
    assert not stall_watchdog.overlay_absent((box('CLOSE'),), 'CLOSE')
    assert not stall_watchdog.overlay_absent((box('BATTLE'), box('Watch ad')), 'CLOSE')
    assert not stall_watchdog.overlay_absent((box('Collect'),), 'CLOSE')
    assert stall_watchdog.overlay_absent((box('SKIP'),), 'CLOSE')  # Lower priority only.
    assert not stall_watchdog.overlay_absent((box('BATTLE'),), 'UNLISTED')


def _paused_scan_lines(h: Harness) -> int:
    """Lines of _run_once executed after the watchdog block on one paused scan."""
    import inspect
    import sys
    from tower_bot import TowerBot
    source, start = inspect.getsourcelines(TowerBot._run_once)
    marker = next(i for i, line in enumerate(source)
                  if 'if recovery is not RecoveryState.READY:' in line)
    boundary = start + marker
    code = TowerBot._run_once.__code__
    seen: list[int] = []

    def tracer(frame: Any, event: str, arg: Any) -> Any:
        if frame.f_code is code:
            if event == 'line' and frame.f_lineno >= boundary:
                seen.append(frame.f_lineno)
            return tracer
        return None
    sys.settrace(tracer)
    try:
        h.scan()
    finally:
        sys.settrace(None)
    return len(seen)


def test_recovery_requested_pause_keeps_paused_scan_observations(
        tmp_path: Path, fleet: Path) -> None:
    off = Harness(tmp_path / 'off', tmp_path / 'fleet-off')
    try:
        off.scan()
        assert off.bot.controls.snapshot().paused
        baseline = _paused_scan_lines(off)
    finally:
        off.close()
    configure(fleet)
    shadow = Harness(tmp_path / 'shadow', fleet)
    try:
        shadow.until(lambda: shadow.recovery.status()['phase'] == 'provider')
        shadow.service().release(candidate_id='stall:CLOSE')
        shadow.until(lambda: shadow.bot.controls.snapshot().paused)
        assert shadow.recovery.owns_lane  # Recovery requested this pause.
        lines = _paused_scan_lines(shadow)
    finally:
        shadow.close()
    assert baseline > 0 and lines == baseline
