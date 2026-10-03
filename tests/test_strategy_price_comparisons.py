"""Price comparisons protect continuing upgrades in both spending lanes."""
from types import SimpleNamespace
from typing import Any

import pytest

from fleet import strategy_blocks as blocks
from fleet.build_route_eval import RouteFacts


def evaluate(lane: str, price: int = 40, absolute: int = 50,
             thorns: int = 60, jar: int = 0,
             missing: str | None = None) -> Any:
    program = blocks.validate_program([{
        'id': 'speed', 'type': 'pool', 'upgrade_ids': ['attack_speed'],
        'selection': 'priority', 'wallet_share_pct': 40,
        'wallet_share_basis': 'spendable',
        'cheaper_than_upgrade_ids': ['defense_absolute', 'thorns'],
    }, {'id': 'fallback', 'type': 'buy', 'upgrade_id': 'damage'}], lane)
    route = SimpleNamespace(revision=1,
        workshop=SimpleNamespace(blocks=program, banned_upgrade_ids=frozenset(), coin_spend_limit_pct=100),
        battle=SimpleNamespace(blocks=program))
    prices = {'attack_speed': price, 'defense_absolute': absolute, 'thorns': thorns, 'damage': 1}
    if missing:
        prices.pop(missing)
    sample = RouteFacts('account', 'worker', lane, 100, 101, best_tier_1_wave=100,
        wallet_coins=100, battle_cash=100, lab_coin_jar=jar, run_id=1, wave=11,
        confirmed_purchases={}, run_purchases={},
        purchases={'unlock_defense_upgrades': 1, 'unlock_thorns': 1},
        prices=prices, price_evidence={uid: {'source': 'observed', 'observed_at': 100} for uid in prices},
        upgrade_rows={uid: {'status': 'available', 'value': 1, 'price': value, 'observed_at': 100}
                      for uid, value in prices.items()})
    return blocks.evaluate_program(route, sample, None, lane)


@pytest.mark.parametrize('lane', ['workshop', 'battle'])
@pytest.mark.parametrize(('price', 'absolute', 'thorns', 'expected'), [
    (40, 50, 60, 'attack_speed'), (41, 50, 60, 'damage'),
    (40, 40, 60, 'damage'), (40, 50, 40, 'damage'), (40, 50, 39, 'damage'),
])
def test_both_price_references_and_wallet_boundary(lane: str, price: int, absolute: int,
                                                  thorns: int, expected: str) -> None:
    assert evaluate(lane, price, absolute, thorns).decision.upgrade_id == expected


@pytest.mark.parametrize('lane', ['workshop', 'battle'])
@pytest.mark.parametrize('missing', ['defense_absolute', 'thorns'])
def test_missing_reference_requests_observation(lane: str, missing: str) -> None:
    result = evaluate(lane, missing=missing)
    assert missing in result.trace.observation_ids


def test_spendable_share_excludes_reserved_lab_coins() -> None:
    assert evaluate('workshop', price=16, jar=60).decision.upgrade_id == 'attack_speed'
    assert evaluate('workshop', price=17, jar=60).decision.upgrade_id == 'damage'


def test_reference_rows_are_discovered_and_validated() -> None:
    pool = {'id': 'speed', 'type': 'pool', 'upgrade_ids': ['attack_speed'],
            'cheaper_than_upgrade_ids': ['defense_absolute', 'thorns']}
    program = blocks.validate_program([pool], 'battle')
    assert set(blocks.program_upgrade_ids(program)) == {'attack_speed', 'defense_absolute', 'thorns'}
    for refs in [[], ['bogus'], ['thorns', 'thorns']]:
        with pytest.raises(ValueError):
            blocks.validate_program([{**pool, 'cheaper_than_upgrade_ids': refs}], 'battle')
    with pytest.raises(ValueError):
        blocks.validate_program([{'id': 'goal', 'type': 'save_for', 'goal': [pool]}], 'workshop')


def test_comparison_references_follow_early_coin_substitution() -> None:
    program = ({'id': 'speed', 'type': 'pool', 'upgrade_ids': ['attack_speed'],
                'cheaper_than_upgrade_ids': ['coins_per_kill_bonus', 'coins_per_wave']},)
    assert blocks.swap_kill_bonus(program)[0]['cheaper_than_upgrade_ids'] == ['coins_per_wave']
