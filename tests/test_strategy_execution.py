import pytest
from pathlib import Path
from dataclasses import replace

from tests.test_strategy_service import target_at
from strategy import Strategy
from strategy_service import StrategyService
from account_state import AccountState


def test_standalone_assignment_executes_without_pool(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    template = service.library()['templates'][0]
    service.assign(expected_revision=0, strategy_id=template['id'], strategy_version=1,
                   targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    source = Strategy.from_config('local')
    resolved = execution.resolve(source, pending_verification=False)
    assert resolved.source == 'studio_native'
    assert resolved.strategy.autopilot.enabled
    assert execution.progress is not None
    assert execution.progress.worker_id == 'standalone'
    assert execution.progress.unlock_rollout() is None
    assert not (tmp_path / 'reroll-target.json').exists()
    assert not (tmp_path / 'workers').exists()
    policy = execution.progress.battle_policy(resolved.strategy.autopilot,
        {'cash_per_wave': {'status': 'available', 'price': 10, 'value': 0}},
        run_id=1, wave=1, cash=100, visible_upgrade_ids=('cash_per_wave',))
    assert policy.enabled
    assert policy.rules


def test_imported_policy_waits_for_confirmation_and_keeps_local_timing(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    from strategy_compat import capture_legacy
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    local = Strategy.from_config('local')
    source = replace(local, interval=local.interval + 2)
    row = service.library_store.import_legacy(capture_legacy(source, 'etag'), source_key='local')['strategies'][0]
    service.assign(expected_revision=0, strategy_id=row['id'], strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    held = execution.resolve(local, pending_verification=True)
    assert held.source == 'blocked'
    resolved = execution.resolve(local, pending_verification=False)
    assert resolved.source == 'studio_legacy'
    assert resolved.strategy.interval == local.interval
    assert service.status(target)['state'] == 'applied'


def test_corrupt_assignment_blocks_every_spending_path(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    target.assignment_root.mkdir()
    (target.assignment_root / 'build-route.json').write_text('{broken')
    execution = StrategyExecution(lambda: target, AccountState())
    result = execution.resolve(Strategy.from_config('local'), pending_verification=False)
    assert result.source == 'blocked'
    assert not result.strategy.shopping.enabled
    assert result.strategy.autopilot.observe_only


def test_deleted_studio_store_does_not_reenable_legacy_purchases(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    service.assign(expected_revision=0, strategy_id='opening', strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    source = Strategy.from_config('local')
    execution.resolve(source, pending_verification=False)
    service.assignment_store.path.unlink()
    for entry in service.assignment_store.history.iterdir():
        entry.unlink()
    result = execution.resolve(source, pending_verification=False)
    assert result.source == 'blocked'


def test_native_plan_owns_tier_progression(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    baseline = service.library()['templates'][0]['baseline']
    baseline['tier_promotion'] = {'1': 100}
    library = service.save_version(expected_revision=0, name='Promote', source_template='opening', baseline=baseline)
    service.assign(expected_revision=0, strategy_id=library['strategies'][0]['id'], strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    result = execution.resolve(Strategy.from_config('local'), pending_verification=False)
    assert result.strategy.tier_promotion.to_dict() == {'1': 100}


def test_lost_identity_and_deleted_store_keep_assigned_bot_blocked(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    service.assign(expected_revision=0, strategy_id='opening', strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    local = Strategy.from_config('local')
    execution.resolve(local, pending_verification=False)
    execution.target = lambda: None
    assert execution.resolve(local, pending_verification=False).source == 'blocked'


def test_native_tournament_config_comes_from_assigned_plan(tmp_path: Path) -> None:
    from tournament_policy import TournamentConfig
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    baseline = service.library()['templates'][0]['baseline']
    baseline['tournament'] = TournamentConfig(enabled=True).to_dict()
    library = service.save_version(expected_revision=0, name='Tournament plan',
        source_template='opening', baseline=baseline)
    service.assign(expected_revision=0, strategy_id=library['strategies'][0]['id'],
        strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    local = replace(Strategy.from_config('local'), tournament=TournamentConfig(enabled=False))
    resolved = execution.resolve(local, pending_verification=False)
    assert resolved.strategy.tournament.enabled
    assert not local.tournament.enabled
    assert not execution.blocked(local, 'identity_changed').strategy.tournament.enabled


def test_purchase_authority_expires_when_assignment_changes(tmp_path: Path) -> None:
    from strategy_execution import StrategyExecution
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    service.assign(expected_revision=0, strategy_id='opening', strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    execution.resolve(Strategy.from_config('local'), pending_verification=False)
    assert execution.authority_current()
    service.assign(expected_revision=1, strategy_id='opening', strategy_version=1, targets=[target])
    assert not execution.authority_current()


def test_imported_and_blocked_plans_do_not_schedule_native_labs(tmp_path: Path) -> None:
    from tower_bot import TowerBot
    from strategy_execution import StrategyExecution
    from strategy_compat import capture_legacy
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    local = Strategy.from_config('local')
    row = service.library_store.import_legacy(capture_legacy(local, 'etag'), source_key='local')['strategies'][0]
    service.assign(expected_revision=0, strategy_id=row['id'], strategy_version=1, targets=[target])
    bot = TowerBot.__new__(TowerBot)
    bot.reroll_progress = object()
    bot.lab_visit = object()
    bot.strategy_execution = StrategyExecution(lambda: target, AccountState())
    bot.strategy_execution.resolve(local, pending_verification=False)
    assert bot.plan_coordinator is None
    assert not bot._request_planned_lab_visit(0, True)
    bot.strategy_execution.blocked(local, 'identity_changed')
    assert not bot._request_planned_lab_visit(0, True)


def test_native_workshop_can_buy_with_coins_above_lab_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from fleet.build_route_eval import RouteFacts
    from strategy_execution import StrategyExecution

    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    baseline = service.library()['templates'][0]['baseline']
    baseline['workshop'] = {'mode': 'priorities', 'priority_ids': ['attack_speed']}
    baseline['rules']['coins']['lab_share'] = {'mode': 'save_pct', 'pct': 10}
    library = service.save_version(expected_revision=0, name='Workshop spending',
        source_template='opening', baseline=baseline)
    service.assign(expected_revision=0, strategy_id=library['strategies'][0]['id'],
        strategy_version=1, targets=[target])
    execution = StrategyExecution(lambda: target, AccountState())
    local = Strategy.from_config('local')
    resolved = execution.resolve(local, pending_verification=False)
    progress = execution.progress
    assert progress is not None
    now = time.time()
    facts = RouteFacts(target.account_id, target.target_id, 'main_menu', now, now,
        wallet_coins=591510, prices={'attack_speed': 38000}, visit_id='after-run:310')
    monkeypatch.setattr(progress, 'route_facts', lambda: facts)
    monkeypatch.setattr(progress, '_settle_jar', lambda *args: 150000)

    policy = progress.shopping_policy(resolved.strategy.shopping)

    assert policy.enabled and policy.armed
    assert policy.workshop[0].name == 'Attack Speed'
    assert policy.coin_budget == 38000
    assert policy.coin_budget_pct is None
