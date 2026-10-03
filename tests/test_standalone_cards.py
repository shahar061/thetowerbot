"""Private standalone startup proves identity before exposing Cards authority."""
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

import db
from fleet.identity import Attempt, IdentityEvidence
from runner import RunnerError
from tower_bot import parse_args, resolve_worker_runtime
from tests.test_runner import runner_parts  # noqa: F401


def arguments(root: Path, *extra: str) -> list[str]:
    return ['--web', '--idle', '--web-port', '18882', '--standalone-root', str(root),
            '--expected-account', 'ACCOUNT-A', '--game-package', 'com.TechTreeGames.TheTower', *extra]


def test_private_standalone_runtime_uses_isolated_paths(tmp_path: Path) -> None:
    runtime = resolve_worker_runtime(parse_args(arguments(tmp_path)))
    assert runtime.root == tmp_path / 'standalone'
    assert runtime.db_path == runtime.root / 'tower_bot.db'
    assert runtime.strategy_root == runtime.root / 'strategies'


@pytest.mark.parametrize('extra', [['--no-store'], ['--once'], ['--web-host', '0.0.0.0'],
                                  ['--worker-id', 'foreign'], ['--fleet-root', '/tmp/fleet']])
def test_standalone_rejects_mixed_or_unfenced_modes(tmp_path: Path, extra: list[str]) -> None:
    with pytest.raises(ValueError, match='standalone'):
        resolve_worker_runtime(parse_args(arguments(tmp_path, *extra)))


def setup_runner(parts: tuple, root: Path, monkeypatch: pytest.MonkeyPatch,
                 observed: str = 'ACCOUNT-A') -> tuple:
    runner, made, _, _, _ = parts
    runner._attempt = Attempt.new('standalone', '127.0.0.1:5555', 'private-lease', 'private-attempt')
    runner._binding_path = root / 'checkpoints' / f'{runner._attempt.generation}.json'
    runner._supervisor_path = root / 'checkpoints' / 'supervisor.json'
    runner._unknown_dir = root / 'evidence'
    runner._standalone_expected_account = 'ACCOUNT-A'
    runner._game_package = 'com.TechTreeGames.TheTower'
    runner.account_state.attach_safety_storage(root / 'tower_bot.db')
    raw = SimpleNamespace(serial='127.0.0.1:5555', app_info=lambda _: SimpleNamespace(version_name='29.0.3'))
    runner._device_factory = lambda: raw
    walks = []

    def walk(**kwargs: object) -> IdentityEvidence:
        walks.append(kwargs['expected_account'])
        kwargs['supervisor'].verify_account(observed, observed_at=time.time())
        return IdentityEvidence(observed, time.time(), 'recorded-account-walk')

    monkeypatch.setattr('fleet.restart_account.verify_restart_account', walk)
    return runner, made, walks


def test_standalone_fresh_walk_binds_empty_db_and_reverifies_every_start(
        runner_parts: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, made, walks = setup_runner(runner_parts, tmp_path, monkeypatch)
    assert runner.account_state.verified_scope is None
    runner.start()
    try:
        first = runner.account_state.verified_scope
        assert first is not None and first.account_id == 'ACCOUNT-A'
        assert db.bound_account(tmp_path / 'tower_bot.db') == 'ACCOUNT-A'
        assert runner._binding_path.exists()
        assert made[-1].kwargs['identity_reverifier'] == runner.reverify_identity
    finally:
        runner.stop()
    runner.start()
    try:
        assert runner.account_state.verified_scope.generation != first.generation
        assert walks == ['ACCOUNT-A', 'ACCOUNT-A']
    finally:
        runner.stop()


@pytest.mark.parametrize('refusal', ['mismatch', 'foreign_db', 'unattributed_db'])
def test_standalone_failed_verification_never_binds_scope(
        runner_parts: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str) -> None:
    runner, made, _ = setup_runner(runner_parts, tmp_path, monkeypatch,
                                  observed='ACCOUNT-B' if refusal == 'mismatch' else 'ACCOUNT-A')
    path = tmp_path / 'tower_bot.db'
    if refusal == 'foreign_db':
        db.bind_account(path, 'ACCOUNT-B')
    elif refusal == 'unattributed_db':
        with db.connect(path) as conn:
            db.start_run(conn, 1, started_at=1.)
    try:
        with pytest.raises(RunnerError):
            runner.start()
        assert runner.account_state.verified_scope is None
        assert made == []
    finally:
        runner.stop()


def test_standalone_reconnect_drops_authority_until_fresh_matching_walk(
        runner_parts: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _, walks = setup_runner(runner_parts, tmp_path, monkeypatch)
    runner.start()
    try:
        first = runner.account_state.verified_scope
        runner._supervisor.disconnected()
        assert runner.account_state.verified_scope is None
        assert runner.account_state.persisted_epoch > first.epoch
        runner._supervisor.recover()
        runner.reverify_identity()
        restored = runner.account_state.verified_scope
        assert restored.account_id == first.account_id and restored.epoch > first.epoch
        assert walks == ['ACCOUNT-A', 'ACCOUNT-A']
    finally:
        runner.stop()


def test_actual_idle_standalone_main_never_connects_or_constructs_fleet(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tower_bot
    import config
    from control import Controls
    args = parse_args(arguments(tmp_path, '--no-telegram'))
    runtime = resolve_worker_runtime(args)
    runtime.ensure_directories()
    monkeypatch.setattr(config, 'STREAM_ENABLED', False)
    monkeypatch.setattr(tower_bot, 'build_checks_and_controls',
                        lambda loaded: ({'brightness': None}, Controls(loaded)))
    monkeypatch.setattr('fleet.setup.FleetSetupService',
                        lambda *a, **k: pytest.fail('standalone constructed a fleet controller'))
    monkeypatch.setattr(tower_bot, 'connect_device',
                        lambda *a, **k: pytest.fail('idle startup performed device I/O'))
    seen = []

    def serve(runner: object, app: object, **kwargs: object) -> None:
        seen.append(runner)
        assert kwargs['start_immediately'] is False
        assert runner._standalone_expected_account == 'ACCOUNT-A'
        assert runner._binding_path.parent == runtime.checkpoint_root
        assert runner.account_state.safety_path == runtime.db_path
        assert runner.account_state.verified_scope is None
        assert db.bound_account(runtime.db_path) is None

    monkeypatch.setattr(tower_bot, 'serve_web', serve)
    with runtime.reserve('127.0.0.1:65532'):
        assert tower_bot._main(args, runtime) == 0
    assert len(seen) == 1
    assert list(runtime.strategy_root.glob('*.json'))
