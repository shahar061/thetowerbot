from dataclasses import replace

import pytest

import config
from fleet import strategy_blocks as blocks
from tests.test_strategy_blocks import battle_facts, pool, route


def mixed_pool(**extra: object) -> dict:
    return pool(upgrade_ids=['health', 'package_chance'], selection='cheapest',
                price_source='model', observed_upgrade_ids=['package_chance'],
                batch_size=5, max_price_premium_pct=25, **extra)


@pytest.mark.parametrize('package_price,chosen,source', [(5, 'package_chance', 'observed'), (20, 'health', 'model')])
def test_mixed_pool_compares_live_recovery_price_with_modeled_core(
        monkeypatch: pytest.MonkeyPatch, package_price: int, chosen: str, source: str) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    program = blocks.validate_program([mixed_pool()], 'battle')
    facts = replace(battle_facts(package_chance=dict(status='available', value=6, price=package_price,
                                                    observed_at=100)),
        battle_price_quotes={'health': dict(account_id='account', run_id=7, status='available',
                                            source='model', verified=True, price=10, value=1)},
        battle_cash=100000, battle_tab='UTILITY', visible_upgrade_ids=('package_chance',))
    result = blocks.evaluate_program(route(list(program), lane='battle'), facts, None, 'battle')
    assert result.decision.upgrade_id == chosen
    assert result.decision.price_source == source
    if source == 'observed':
        assert result.decision.burst_price_ceiling is None


@pytest.mark.parametrize('status', ['locked', 'maxed'])
def test_mixed_pool_skips_unavailable_recovery_upgrades(status: str) -> None:
    program = blocks.validate_program([mixed_pool()], 'battle')
    facts = replace(battle_facts(package_chance=dict(status=status, value=None, price=None, observed_at=100)),
        battle_price_quotes={'health': dict(account_id='account', run_id=7, status='available',
                                            source='model', verified=True, price=10, value=1)})
    result = blocks.evaluate_program(route(list(program), lane='battle'), facts, None, 'battle')
    assert result.decision.upgrade_id == 'health'
    assert result.decision.price_source == 'model'


def test_mixed_pool_reads_stale_recovery_price_before_comparing() -> None:
    program = blocks.validate_program([mixed_pool()], 'battle')
    facts = replace(battle_facts(package_chance=dict(status='available', value=6, price=1, observed_at=1)),
        battle_price_quotes={'health': dict(account_id='account', run_id=7, status='available',
                                            source='model', verified=True, price=10, value=1)})
    result = blocks.evaluate_program(route(list(program), lane='battle'), facts, None, 'battle')
    assert result.trace.observation_ids == ('package_chance',)
    assert result.status == 'projected'


@pytest.mark.parametrize('fallback', [['orbs'], ['package_chance', 'package_chance']])
def test_mixed_pool_rejects_invalid_observed_subset(fallback: list[str]) -> None:
    block = mixed_pool()
    block['observed_upgrade_ids'] = fallback
    with pytest.raises(ValueError, match='observed'):
        blocks.validate_program([block], 'battle')
