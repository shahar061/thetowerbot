from pathlib import Path

import db
from evidence_scope import FactScope
from strategy_target import StrategyTargetContext


def target_at(root: Path) -> StrategyTargetContext:
    scope = FactScope('ACCOUNT-A', 'lease', 'g1', 0)
    path = root / 'private.db'
    db.bind_account(path, scope.account_id)
    return StrategyTargetContext('standalone', scope, root, path, root / 'strategies',
                                 root / 'strategies', frozenset({'battle'}), lambda: scope)


def test_save_assign_and_ack_are_separate(tmp_path: Path) -> None:
    from strategy_service import StrategyService
    from fleet.build_route_runtime import BuildRouteRuntime
    target = target_at(tmp_path)
    service = StrategyService(library_root=target.library_root,
        assignment_root=target.assignment_root, resolve_target=lambda _: target)
    baseline = service.library()['templates'][0]['baseline']
    library = service.save_version(expected_revision=0, name='Test plan',
        source_template='opening', baseline=baseline)
    assert service.assignment_store.read().revision == 0
    saved = library['strategies'][0]
    response = service.assign(expected_revision=0, strategy_id=saved['id'],
                             strategy_version=1, targets=[target], actor='operator')
    assert response['status'] == 'pending'
    assert service.status(target)['state'] == 'pending'
    runtime = BuildRouteRuntime.for_target(target)
    runtime.acknowledge(response['route']['revision'], target.account_id)
    assert service.status(target)['state'] == 'applied'
    service.save_version(expected_revision=1, name='Test plan', source_template='opening',
                         baseline=baseline, strategy_id=saved['id'])
    assert service.status(target)['strategy_version'] == 1
    from dataclasses import replace
    restarted_scope = replace(target.scope, generation='g2')
    restarted = replace(target, scope=restarted_scope, read_scope=lambda: restarted_scope)
    assert service.status(restarted)['state'] == 'pending'
    BuildRouteRuntime.for_target(restarted).acknowledge(1, restarted.account_id)
    assert service.status(restarted)['state'] == 'applied'
    assert service.status(restarted)['strategy_version'] == 1


def test_runtime_uses_private_paths_and_rechecks_identity(tmp_path: Path) -> None:
    from fleet.build_route_runtime import BuildRouteRuntime
    target = target_at(tmp_path)
    runtime = BuildRouteRuntime.for_target(target)
    assert runtime.ack_path.parent == tmp_path
    assert runtime.db_path == target.db_path
    assert not (tmp_path / 'workers').exists()


def test_decision_choice_is_not_reused_after_session_change(tmp_path: Path) -> None:
    from dataclasses import replace
    from fleet.build_route_runtime import BuildRouteRuntime
    target = target_at(tmp_path)
    runtime = BuildRouteRuntime.for_target(target)
    runtime._write_choice({'account_id': target.account_id, 'sequence': 3, 'pending': {'upgrade_id': 'damage'}})
    assert runtime._choice_state()['sequence'] == 3
    next_scope = replace(target.scope, generation='g2')
    next_target = replace(target, scope=next_scope, read_scope=lambda: next_scope)
    assert BuildRouteRuntime.for_target(next_target)._choice_state() == {}
