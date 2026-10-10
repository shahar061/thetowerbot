from dataclasses import replace
from pathlib import Path

import pytest

import db
from evidence_scope import FactScope


def test_private_target_verifies_scope_and_database(tmp_path: Path) -> None:
    from strategy_target import StrategyTargetContext, StrategyTargetError
    scope = FactScope('ACCOUNT-A', 'lease', 'g1', 0)
    live = [scope]
    path = tmp_path / 'private.db'
    db.bind_account(path, scope.account_id)
    target = StrategyTargetContext('standalone', scope, tmp_path, path,
                                   tmp_path / 'strategies', tmp_path / 'strategies',
                                   frozenset({'battle'}), lambda: live[0])
    target.verify_current()
    assert target.account_id == 'ACCOUNT-A'
    live[0] = replace(scope, generation='g2')
    with pytest.raises(StrategyTargetError, match='scope'):
        target.verify_current()


def test_target_never_accepts_foreign_database(tmp_path: Path) -> None:
    from strategy_target import StrategyTargetContext, StrategyTargetError
    scope = FactScope('ACCOUNT-A', 'lease', 'g1', 0)
    path = tmp_path / 'private.db'
    db.bind_account(path, 'ACCOUNT-B')
    target = StrategyTargetContext('standalone', scope, tmp_path, path,
                                   tmp_path, tmp_path, frozenset(), lambda: scope)
    with pytest.raises(StrategyTargetError, match='database'):
        target.verify_current()
