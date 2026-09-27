"""Scan-owned recovery coordinator: off-thread storage, durable allowances, fresh guards."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from recovery_policy import (RecoveryCandidate, RecoveryContext, RecoveryProposal, RecoveryReply,
                             RecoveryScope, RecoverySettings, VerifiedControl)

SECRET = 'sk-or-synthetic-never-shown'


def context(generation: int = 1, *, scope: dict[str, Any] | None = None,
            **changes: object) -> RecoveryContext:
    fields = dict(worker='worker-a', account_id='account-a', lease_id='lease-a',
                  attempt_id='attempt-a', attempt_generation='generation-a', boot_id='boot-a',
                  worker_generation=1, strategy_revision=0, device_command_generation=0,
                  pending_transaction_generation=0) | (scope or {})
    candidate = RecoveryCandidate(candidate_id='dismiss:CLOSE', action_id='close_overlay',
        screen='STALL_ESCAPE', control_generation=0, target='CLOSE',
        expected_postcondition='overlay_absent',
        control=VerifiedControl(x=1, y=1, width=2, height=2, frame_width=10, frame_height=10))
    return RecoveryContext(scope=RecoveryScope(**fields), screen='STALL_ESCAPE',
        screen_generation=1, observation_generation=generation,
        observed_at_monotonic=time.monotonic(), candidates=(candidate,)).model_copy(update=changes)


class Scans:
    """Monotonic observation generations shared by step and live_context."""
    def __init__(self, **defaults: Any) -> None:
        self.generation, self.defaults = 1, defaults

    def __call__(self, **changes: Any) -> RecoveryContext:
        self.generation += 1
        return context(self.generation, **(self.defaults | changes))

    def tap(self, sink: list[Any], guard: Callable[[], bool]) -> None:
        """Fake DeviceSupervisor.recovery_tap: guard, input, command epoch +1."""
        allowed = guard()
        sink.append(allowed)
        if not allowed:
            from supervisor import RecoveryPreflightBlocked
            raise RecoveryPreflightBlocked('recovery_scope_changed')
        if allowed:
            scope = dict(self.defaults.get('scope') or {})
            scope['device_command_generation'] = scope.get('device_command_generation', 0) + 1
            self.defaults = self.defaults | {'scope': scope}


def advance(coordinator: Any, predicate: Callable[[], bool], scans: Scans | None = None,
            timeout: float = 3, **kwargs: Any) -> None:
    scans = scans or Scans()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        coordinator.step(scans(), **kwargs)
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError(coordinator.status())


def configure(root: Path, mode: str = 'shadow', **settings: Any) -> None:
    from recovery_settings import RecoverySettingsStore
    store = RecoverySettingsStore(root)
    state = store.read()
    store.save(RecoverySettings(mode=mode, **settings),
               shadow_worker='worker-a' if mode == 'shadow' else None,
               expected_settings_revision=state.settings_revision,
               expected_policy_revision=state.policy.active.revision)


def grant(root: Path, **changes: Any) -> None:
    from recovery_capabilities import CalibrationEvidence, CapabilityRegistry
    now = time.time()
    registry = CapabilityRegistry(root)
    identifier = registry.register(CalibrationEvidence.model_validate(dict(
        schema_version=1, host_contract='towerbot-dismiss-v1', action='close_overlay',
        model='openai/gpt-5.4-nano', worker='worker-a', account_id='account-a',
        screen='STALL_ESCAPE', target='CLOSE', replay_sha256='a' * 64, canary_sha256='b' * 64,
        canary_kind='recorded_hardware', replay_passed=True,
        semantic_postcondition='overlay_absent', canary_postcondition_confirmed=True,
        recorded_at=now - 10, expires_at=now + 3600, operator='test-operator') | changes),
        now=now)
    registry.activate(identifier, operator='test-operator', now=now)


class FakeService:
    """Suspended provider: replies only when the test releases one."""
    instances: list[FakeService] = []

    def __init__(self, root: Path, *, worker: str, settings: RecoverySettings,
                 api_key: str | None) -> None:
        self.settings, self.key = settings, api_key
        self.requests: list[Any] = []
        self.reply: RecoveryReply | None = None
        self.invalidated: list[str] = []
        self.closed = self.stuck = False
        FakeService.instances.append(self)

    def submit(self, request: Any) -> bool:
        if not self.status().configured or self.closed:
            return False
        self.requests.append(request)
        return True

    def poll(self, *, incident_id: str, request_id: str | None = None) -> RecoveryReply | None:
        reply, self.reply = self.reply, None
        return reply

    def invalidate(self, *, reason: str) -> None:
        self.invalidated.append(reason)
        self.reply = None

    def close(self) -> None:
        self.closed = True

    def release(self, **proposal: Any) -> None:
        self.reply = RecoveryReply(request_id=self.requests[-1].request_id, cost_microusd=0,
            proposal=RecoveryProposal(**(dict(action_id='close_overlay',
                candidate_id='dismiss:CLOSE', expected_postcondition='overlay_absent',
                explanation=f'provider text {SECRET}') | proposal)))

    def status(self) -> Any:
        return SimpleNamespace(configured=self.settings.mode != 'off' and bool(self.key),
            shutdown_complete=self.closed and not self.stuck,
            shutdown_failed=self.closed and self.stuck, budget=None,
            budget_observed_at_monotonic=None, blocker=None)


@pytest.fixture
def make(tmp_path: Path) -> Any:
    FakeService.instances = []
    made: list[Any] = []
    loads: list[int] = []

    def build(root: Path = tmp_path, **kwargs: Any) -> Any:
        from recovery_coordinator import RecoveryCoordinator
        def key() -> str:
            loads.append(1)
            return SECRET
        coordinator = RecoveryCoordinator(root, worker='worker-a', key_loader=key,
                                          service_factory=FakeService, **kwargs)
        made.append(coordinator)
        return coordinator
    build.loads = loads
    yield build
    for coordinator in made:
        coordinator.close()


def episode(root: Path, incident_id: str) -> Any:
    from recovery_episodes import EpisodeNamespace, RecoveryEpisodes
    return RecoveryEpisodes(root).get(namespace=EpisodeNamespace('worker-a', 'account-a'),
                                      incident_id=incident_id)


def test_storage_command_does_not_block_scan_and_is_bounded(tmp_path: Path) -> None:
    from recovery_coordinator import RecoveryStorage
    gate = threading.Event()
    storage = RecoveryStorage(tmp_path, worker='worker-a')
    try:
        assert storage.submit('blocked', lambda stores: gate.wait(2))
        start = time.monotonic()
        assert not storage.submit('second', lambda stores: None)
        for _ in range(100):
            assert storage.poll() is None
        assert time.monotonic() - start < .1
    finally:
        gate.set()
        storage.close()


def test_default_off_never_owns_lane_opens_incident_or_loads_key(tmp_path: Path, make: Any) -> None:
    coordinator = make(status_path=tmp_path / 'worker-a' / 'recovery-status.json')
    advance(coordinator, lambda: coordinator.status()['settings_revision'] is not None,
            stalled=True, deterministic=True)
    for _ in range(20):
        assert coordinator.step(context(), stalled=True, deterministic=True,
                                dispatch=lambda *a: pytest.fail('input')) is False
    status = coordinator.status()
    assert status['mode'] == 'off' and status['incident_id'] is None
    assert status['configured'] is False
    assert make.loads == []
    import sqlite3
    if (tmp_path / 'recovery-episodes.sqlite3').exists():
        with sqlite3.connect(tmp_path / 'recovery-episodes.sqlite3') as db:
            assert db.execute('SELECT COUNT(*) FROM recovery_episodes').fetchone()[0] == 0
    coordinator.close()
    deadline = time.monotonic() + 2
    while not coordinator.shutdown_complete and time.monotonic() < deadline:
        time.sleep(.01)
    assert coordinator.shutdown_complete


def test_blocked_storage_stays_off_scan(tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['mode'] == 'shadow', stalled=False)
    gate = threading.Event()
    assert coordinator.storage.submit('blocked', lambda stores: gate.wait(3))
    try:
        for _ in range(30):
            start = time.monotonic()
            coordinator.step(context(), stalled=True, deterministic=True,
                             dispatch=lambda *a: pytest.fail('input'))
            assert time.monotonic() - start < .05
    finally:
        gate.set()


def test_deterministic_reservation_then_semantic_confirmation(tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    taps: list[bool] = []
    scans = Scans()
    coordinator = make()
    advance(coordinator, lambda: len(taps) == 1, scans, stalled=True, deterministic=True,
            dispatch=lambda candidate, guard: scans.tap(taps, guard), live_context=scans)
    assert taps == [True]
    assert coordinator.status()['phase'] == 'postcondition'
    incident = coordinator.status()['incident_id']
    coordinator.step(scans(screen='MAIN_MENU', screen_generation=2, candidates=()),
                     stalled=True, semantic_postcondition=True)
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'recovered', scans,
            stalled=False)
    assert coordinator.status()['phase'] == 'idle'
    stored = episode(tmp_path, incident)
    assert stored.actions_used == 1 and stored.outcome == 'recovered'
    assert stored.cooldown_until is not None


def test_scope_change_after_reservation_keeps_allowance_and_never_dispatches(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    scans = Scans()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'dispatch', scans,
            stalled=True, deterministic=True)
    incident = coordinator.status()['incident_id']
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated',
            Scans(paused=True), stalled=True,
            dispatch=lambda candidate, guard: pytest.fail('input after pause'))
    stored = episode(tmp_path, incident)
    assert stored.actions_used == 1 and not stored.unresolved_action_ids


@pytest.mark.parametrize('scope', [dict(account_id='account-b'), dict(lease_id='lease-b'),
                                   dict(strategy_revision=1), dict(pending_transaction_generation=1)])
def test_final_guard_denies_input_when_scope_moves_after_checkpoint(
        tmp_path: Path, make: Any, scope: dict[str, Any]) -> None:
    configure(tmp_path)
    coordinator = make()
    scans, moved = Scans(), Scans(scope=scope)
    live = [scans]
    sent: list[bool] = []

    def dispatch(candidate: Any, guard: Callable[[], bool]) -> None:
        from supervisor import RecoveryPreflightBlocked
        live[0] = moved  # Scope changes between checkpoint and input.
        if not guard():
            raise RecoveryPreflightBlocked('recovery_scope_changed')
        sent.append(True)
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated', scans,
            stalled=True, deterministic=True, dispatch=dispatch, live_context=lambda: live[0]())
    assert sent == []
    stored = episode(tmp_path, _latest_incident(tmp_path))
    assert stored.actions_used == 1 and not stored.unresolved_action_ids


def _latest_incident(root: Path) -> str:
    import sqlite3
    with sqlite3.connect(root / 'recovery-episodes.sqlite3') as db:
        return db.execute('SELECT incident_id FROM recovery_episodes '
                          'ORDER BY rowid DESC LIMIT 1').fetchone()[0]


def test_calls_remaining_survives_known_zero_and_restart(tmp_path: Path) -> None:
    from recovery_budget import RecoveryBudget
    budget = RecoveryBudget(tmp_path)
    assert budget.calls_remaining(incident_id='incident') == 2
    assert budget.reserve(request_id='one', incident_id='incident', day='2026-09-27',
                          maximum_microusd=1)
    budget.mark_sent('one')
    budget.settle('one', actual_microusd=0)
    assert RecoveryBudget(tmp_path).calls_remaining(incident_id='incident') == 1


def test_configured_actions_and_cooldown_are_stricter(tmp_path: Path, make: Any) -> None:
    configure(tmp_path, max_actions=1, cooldown_seconds=1200.)
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'postcondition', stalled=True,
            deterministic=True, dispatch=lambda candidate, guard: None)
    incident = coordinator.status()['incident_id']
    coordinator._dispatched_at -= 6
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'exhausted',
            stalled=True, deterministic=True, dispatch=lambda *a: pytest.fail('second input'))
    stored = episode(tmp_path, incident)
    assert stored.actions_used == 1 and stored.outcome == 'exhausted'
    # Past the store's 600 s but inside the configured 1200 s cooldown.
    import sqlite3
    with sqlite3.connect(tmp_path / 'recovery-episodes.sqlite3') as db:
        db.execute('UPDATE recovery_episodes SET closed_at = closed_at - 900, '
                   'cooldown_until = cooldown_until - 900')
    restarted = make()
    advance(restarted, lambda: restarted.status()['blocker'] == 'cooldown', stalled=True,
            deterministic=True, dispatch=lambda *a: pytest.fail('cooldown input'))


def test_restart_preserves_combined_allowance_and_unresolved_blocks_replay(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    first = make()
    advance(first, lambda: first.status()['phase'] == 'postcondition', stalled=True,
            deterministic=True, dispatch=lambda candidate, guard: None)
    incident = first.status()['incident_id']
    first._dispatched_at -= 6  # No semantic postcondition: no_effect, slot kept.
    advance(first, lambda: first.status()['phase'] == 'provider', stalled=True)
    first.close()
    # New boot/lease/attempt: same stable worker/account/semantic incident.
    restart = dict(boot_id='boot-b', lease_id='lease-b', attempt_id='attempt-b')
    second = make()
    advance(second, lambda: second.status()['phase'] == 'provider', Scans(scope=restart),
            stalled=True, deterministic=True, dispatch=lambda *a: pytest.fail('replayed'))
    assert second.status()['incident_id'] == incident
    assert episode(tmp_path, incident).actions_used == 1
    # A dispatched action without a classified postcondition stays unresolved.
    second.close()
    third = make()
    from recovery_episodes import EpisodeNamespace, RecoveryEpisodes
    episodes = RecoveryEpisodes(tmp_path)
    namespace = EpisodeNamespace('worker-a', 'account-a')
    current = episodes.get(namespace=namespace, incident_id=incident)
    assert episodes.reserve_action(namespace=namespace, incident_id=incident,
        expected_revision=current.revision, scope_token=current.scope_token,
        action_id='crash-mid-input', source='deterministic')
    later = dict(restart, boot_id='boot-c')
    advance(third, lambda: third.status()['blocker'] == 'unresolved', Scans(scope=later),
            stalled=True, deterministic=True, dispatch=lambda *a: pytest.fail('replayed'))
    assert episode(tmp_path, incident).actions_used == 2


def test_shadow_records_redacted_proposal_and_pauses_without_model_input(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    status_path = tmp_path / 'worker-a' / 'recovery-status.json'
    coordinator = make(status_path=status_path)
    paused: list[int] = []
    advance(coordinator, lambda: coordinator.status()['phase'] == 'provider', stalled=True,
            pause=lambda: paused.append(1))
    assert make.loads == [1]
    service = FakeService.instances[-1]
    service.release()
    advance(coordinator, lambda: bool(paused), stalled=True, pause=lambda: paused.append(1),
            dispatch=lambda *a: pytest.fail('shadow input'))
    status = coordinator.status()
    assert status['last_proposal'] == dict(action_id='close_overlay', candidate_id='dismiss:CLOSE',
                                           expected_postcondition='overlay_absent')
    assert status['last_outcome'] == 'exhausted' and paused == [1]
    for _ in range(5):
        assert coordinator.step(context(paused=True), stalled=True, pause=lambda: paused.append(1))
    assert paused == [1]
    assert coordinator.step(context(), stalled=True, pause=lambda: paused.append(1)) is False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if status_path.exists() and 'last_proposal' in status_path.read_text():
            break
        time.sleep(.01)
    text = status_path.read_text()
    assert SECRET not in text and 'provider text' not in text and 'frame_width' not in text
    assert SECRET not in json.dumps(coordinator.status())


def test_assist_without_worker_grant_makes_no_provider_calls(tmp_path: Path, make: Any) -> None:
    grant(tmp_path, worker='worker-other')
    configure(tmp_path, mode='assist')
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['blocker'] == 'assist_uncalibrated',
            stalled=True)
    assert coordinator.status()['mode'] == 'off'
    assert make.loads == []


def _assist(tmp_path: Path, make: Any) -> tuple[Any, Scans]:
    grant(tmp_path)
    configure(tmp_path, mode='assist')
    coordinator = make()
    scans = Scans()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'provider', scans, stalled=True)
    return coordinator, scans


def test_assist_valid_fresh_candidate_dispatches_once_and_needs_postcondition(
        tmp_path: Path, make: Any) -> None:
    coordinator, scans = _assist(tmp_path, make)
    FakeService.instances[-1].release()
    taps: list[bool] = []
    advance(coordinator, lambda: coordinator.status()['phase'] == 'postcondition', scans,
            stalled=True, dispatch=lambda c, guard: scans.tap(taps, guard), live_context=scans)
    for _ in range(10):
        coordinator.step(scans(), stalled=True, dispatch=lambda *a: pytest.fail('twice'),
                         live_context=scans)
    assert taps == [True]
    assert coordinator.status()['phase'] == 'postcondition'  # Tap alone proves nothing.
    coordinator.step(scans(screen='MAIN_MENU', screen_generation=2, candidates=()), stalled=True,
                     semantic_postcondition=True)
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'recovered', scans,
            stalled=False)


@pytest.mark.parametrize('late', ['account', 'lease', 'strategy', 'pause', 'deadline', 'stop',
                                  'settings'])
def test_late_reply_never_inputs(tmp_path: Path, make: Any, late: str) -> None:
    coordinator, scans = _assist(tmp_path, make)
    service = FakeService.instances[-1]
    moved = {'account': Scans(scope=dict(account_id='account-b')),
             'lease': Scans(scope=dict(lease_id='lease-b')),
             'strategy': Scans(scope=dict(strategy_revision=3)),
             'pause': Scans(paused=True)}.get(late, scans)
    if late == 'deadline':
        coordinator.request = coordinator.request.model_copy(
            update={'deadline_at_monotonic': time.monotonic() + .01})
        time.sleep(.02)
    elif late == 'stop':
        coordinator.close()
    elif late == 'settings':
        configure(tmp_path, mode='assist', max_actions=2)
    else:
        coordinator.step(moved(), stalled=True)
    service.release()
    for _ in range(40):
        coordinator.step(moved(), stalled=True, dispatch=lambda *a: pytest.fail('late input'),
                         live_context=moved)
        time.sleep(.01)
    if late != 'stop':
        assert service.invalidated or late == 'deadline'
        assert coordinator.status()['phase'] in {'idle', 'paused', 'closing'}


def test_settings_change_retains_stuck_service_and_surfaces_failed_cleanup(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    advance(coordinator, lambda: coordinator.service is not None, stalled=False)
    old = FakeService.instances[-1]
    old.stuck = True
    configure(tmp_path, max_actions=2)
    advance(coordinator, lambda: coordinator.status()['blocker'] == 'shutdown_failed',
            stalled=False)
    assert old.closed and len(FakeService.instances) == 1
    old.stuck = False
    advance(coordinator, lambda: len(FakeService.instances) == 2, stalled=False)


def test_pending_purchase_blocks_recovery_without_mutation(tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['mode'] == 'shadow', stalled=False)
    for _ in range(20):
        assert coordinator.step(context(pending_transaction=True), stalled=True,
                                deterministic=True, dispatch=lambda *a: pytest.fail('input')) is False
        time.sleep(.005)
    assert coordinator.status()['incident_id'] is None
    import sqlite3
    path = tmp_path / 'recovery-episodes.sqlite3'
    if path.exists():
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT COUNT(*) FROM recovery_episodes').fetchone()[0] == 0


def test_close_before_input_releases_reservation_as_not_dispatched(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'dispatch', stalled=True,
            deterministic=True)
    incident = coordinator.status()['incident_id']
    coordinator.close()
    deadline = time.monotonic() + 2
    while not coordinator.storage.shutdown_complete and time.monotonic() < deadline:
        time.sleep(.01)
    stored = episode(tmp_path, incident)
    assert stored.actions_used == 1 and not stored.unresolved_action_ids


def test_overview_reader_requires_matching_scope_and_fresh_observation(
        tmp_path: Path, make: Any) -> None:
    from recovery_status import recovery_overview
    configure(tmp_path)
    worker_root = tmp_path / 'workers' / 'worker-a'
    coordinator = make(status_path=worker_root / 'recovery-status.json')
    advance(coordinator, lambda: (worker_root / 'recovery-status.json').exists(), stalled=False)
    scope = dict(account_id='account-a', lease_id='lease-a', attempt_id='attempt-a',
                 generation='generation-a')
    view = recovery_overview(worker_root, now=time.time(), **scope)
    assert view is not None and view['mode'] == 'shadow' and view['open_incidents'] is None
    assert view['calls_remaining'] is None  # Unknown until an authoritative read.
    assert recovery_overview(worker_root, now=time.time(), **(scope | dict(lease_id='x'))) is None
    assert recovery_overview(worker_root, now=time.time() + 600, **scope) is None
    assert recovery_overview(tmp_path / 'workers' / 'worker-b', now=time.time(), **scope) is None


def test_state_sink_keeps_redacted_recovery_status() -> None:
    import events
    from sinks.state import BotState
    state = BotState()
    state.apply(events.RecoveryStatusChanged(status=dict(mode='shadow', phase='provider',
        api_key=SECRET, last_proposal=dict(action_id='close_overlay', explanation=SECRET),
        calls_remaining=True)))
    snapshot = state.snapshot()
    assert snapshot['recovery']['mode'] == 'shadow'
    assert snapshot['recovery']['calls_remaining'] is None
    assert SECRET not in json.dumps(snapshot)
    assert snapshot['tail'] == []


# -- Fix round 1 regressions ---------------------------------------------------

@pytest.mark.parametrize('scope', [dict(account_id='account-other'), dict(model='other/model')])
def test_assist_grant_for_other_account_or_model_makes_zero_provider_requests(
        tmp_path: Path, make: Any, scope: dict[str, Any]) -> None:
    grant(tmp_path, **scope)
    if 'model' in scope:
        grant(tmp_path, worker='worker-other')  # Lets assist be saved for the default model.
    configure(tmp_path, mode='assist')
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['blocker'] == 'assist_uncalibrated',
            stalled=True)
    for _ in range(20):
        coordinator.step(context(), stalled=True, dispatch=lambda *a: pytest.fail('input'))
    assert coordinator.status()['mode'] == 'off'
    assert make.loads == []
    assert sum(len(s.requests) for s in FakeService.instances) == 0


def test_assist_account_switch_retires_calibrated_service(tmp_path: Path, make: Any) -> None:
    grant(tmp_path)
    configure(tmp_path, mode='assist')
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['mode'] == 'assist', stalled=False)
    other = Scans(scope=dict(account_id='account-b'))
    advance(coordinator, lambda: coordinator.status()['mode'] == 'off', other, stalled=True,
            dispatch=lambda *a: pytest.fail('input'))
    for _ in range(20):
        coordinator.step(other(), stalled=True)
    assert FakeService.instances[0].closed
    assert sum(len(s.requests) for s in FakeService.instances) == 0


@pytest.mark.parametrize('restart', [False, True])
def test_exhausted_incident_whose_close_never_committed_is_closed_on_reopen(
        tmp_path: Path, make: Any, restart: bool) -> None:
    from recovery_coordinator import scope_token, semantic_fingerprint
    from recovery_episodes import EpisodeNamespace, RecoveryEpisodes
    configure(tmp_path)
    episodes = RecoveryEpisodes(tmp_path)
    namespace = EpisodeNamespace('worker-a', 'account-a')
    before = context()
    opened = episodes.get_or_open(namespace=namespace, fingerprint=semantic_fingerprint(before),
                                  scope_token=scope_token(before), now=time.time()).episode
    for index in range(3):  # Three known outcomes, then a crash before close.
        current = episodes.get(namespace=namespace, incident_id=opened.incident_id)
        assert episodes.reserve_action(namespace=namespace, incident_id=opened.incident_id,
            expected_revision=current.revision, scope_token=current.scope_token,
            action_id=f'action-{index}', source='deterministic')
        episodes.record_outcome(namespace=namespace, incident_id=opened.incident_id,
                                action_id=f'action-{index}', outcome='no_effect')
    stuck = episodes.get(namespace=namespace, incident_id=opened.incident_id)
    assert stuck.status == 'exhausted' and stuck.closed_at is None
    scans = Scans(scope=dict(boot_id='boot-b', lease_id='lease-b')) if restart else Scans()
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['blocker'] == 'cooldown', scans,
            stalled=True, deterministic=True, dispatch=lambda *a: pytest.fail('input'))
    closed = episodes.get(namespace=namespace, incident_id=opened.incident_id)
    assert closed.outcome == 'exhausted' and closed.closed_at is not None
    assert closed.cooldown_until >= closed.closed_at + 600


def _assist_at_dispatch(tmp_path: Path, make: Any) -> tuple[Any, Scans]:
    coordinator, scans = _assist(tmp_path, make)
    FakeService.instances[-1].release()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'dispatch', scans, stalled=True)
    deadline = time.monotonic() + 2
    while coordinator.storage.snapshot()[2] <= coordinator._reserved_at:
        assert time.monotonic() < deadline
        time.sleep(.01)
    return coordinator, scans


def test_mode_off_saved_after_post_reservation_snapshot_blocks_model_input(
        tmp_path: Path, make: Any) -> None:
    coordinator, scans = _assist_at_dispatch(tmp_path, make)
    configure(tmp_path, mode='off')  # Committed after the snapshot the dispatch waited for.
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated', scans,
            stalled=True, dispatch=lambda *a: pytest.fail('model input after mode off'),
            live_context=scans)


@pytest.mark.parametrize('change', ['mode_off', 'policy', 'revoke'])
def test_authority_change_between_checkpoint_and_input_refuses_in_guard(
        tmp_path: Path, make: Any, change: str) -> None:
    from supervisor import RecoveryPreflightBlocked
    coordinator, scans = _assist_at_dispatch(tmp_path, make)
    sent: list[bool] = []

    def dispatch(candidate: Any, guard: Callable[[], bool]) -> None:
        # Simulates the durable checkpoint, then a concurrent commit, then the
        # serial supervisor's guard immediately before the ADB click.
        if change == 'mode_off':
            configure(tmp_path, mode='off')
        elif change == 'policy':
            from recovery_budget import RecoveryBudget
            budget = RecoveryBudget(tmp_path)
            _revision, _raw, policy = budget.settings_snapshot()
            budget.update_policy(expected_revision=policy.revision, incident_limit_microusd=1,
                                 daily_limit_microusd=1, max_calls=1)
        else:
            from recovery_capabilities import CapabilityRegistry
            registry = CapabilityRegistry(tmp_path)
            identifier = registry.audit()[0][0]
            registry.revoke(identifier, operator='test-operator', now=time.time())
        if not guard():
            raise RecoveryPreflightBlocked('recovery_scope_changed')
        sent.append(True)
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated', scans,
            stalled=True, dispatch=dispatch, live_context=scans)
    assert sent == []


def test_writer_bypassing_epoch_is_bounded_by_snapshot_age(tmp_path: Path, make: Any) -> None:
    import recovery_coordinator
    coordinator, scans = _assist_at_dispatch(tmp_path, make)
    gate = threading.Event()
    assert coordinator.storage.submit('blocked', lambda stores: gate.wait(3))  # Refresh stalls.
    try:
        time.sleep(recovery_coordinator.MODEL_INPUT_SNAPSHOT_SECONDS + .1)
        for _ in range(5):
            coordinator.step(scans(), stalled=True, live_context=scans,
                             dispatch=lambda *a: pytest.fail('input on an aged snapshot'))
    finally:
        gate.set()
    assert recovery_coordinator.MODEL_INPUT_SNAPSHOT_SECONDS == .5


def test_raising_live_context_refuses_input_without_unresolved_action(
        tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    scans = Scans()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'dispatch', scans,
            stalled=True, deterministic=True)
    def broken() -> RecoveryContext:
        raise RuntimeError('live read failed')
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated', scans,
            stalled=True, dispatch=lambda *a: pytest.fail('input'), live_context=broken)
    stored = episode(tmp_path, _latest_incident(tmp_path))
    assert stored.actions_used == 1 and not stored.unresolved_action_ids


def test_stall_clearing_by_itself_releases_lane_without_pause(tmp_path: Path, make: Any) -> None:
    configure(tmp_path)
    coordinator = make()
    paused: list[int] = []
    scans = Scans()
    advance(coordinator, lambda: coordinator.status()['phase'] == 'provider', scans,
            stalled=True, pause=lambda: paused.append(1))
    advance(coordinator, lambda: coordinator.status()['last_outcome'] == 'invalidated', scans,
            stalled=False, pause=lambda: paused.append(1))
    assert coordinator.step(scans(), stalled=False, pause=lambda: paused.append(1)) is False
    assert paused == []
    stored = episode(tmp_path, _latest_incident(tmp_path))
    assert stored.closed_at is not None  # Cooldown still applies.


def test_assist_without_candidates_never_calls_the_provider(tmp_path: Path, make: Any) -> None:
    grant(tmp_path)
    configure(tmp_path, mode='assist')
    coordinator = make()
    advance(coordinator, lambda: coordinator.status()['blocker'] == 'no_candidates',
            Scans(candidates=()), stalled=True)
    assert sum(len(s.requests) for s in FakeService.instances) == 0
