"""Built-in templates are readable block routes; these scenarios pin which block acts."""
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from fleet.build_route_eval import RouteFacts
from fleet import strategy_blocks as blocks

OBSERVED = {'source': 'observed', 'observed_at': 99}


def run(policy: str, lane: str, sample: RouteFacts) -> Any:
    program = blocks.template_program(policy, lane)
    workshop = SimpleNamespace(id='workshop.default', mode='blocks', blocks=program,
                               banned_upgrade_ids=frozenset(), coin_spend_limit_pct=100)
    battle = SimpleNamespace(mode='blocks', blocks=program)
    return blocks.evaluate_program(SimpleNamespace(revision=1, workshop=workshop, battle=battle), sample, None, lane)


def workshop(**changes: Any) -> RouteFacts:
    prices = changes.pop('prices', {})
    base = RouteFacts('account', 'Air_38', 'workshop', 100, 101, best_tier_1_wave=5, wallet_coins=100,
        prices=prices, purchases={}, values={}, confirmed_purchases={}, utility_spent_coins=0,
        visit_id='visit', price_evidence={uid: OBSERVED for uid in prices})
    return replace(base, **changes)


def battle(wave: int, **rows: dict[str, Any]) -> RouteFacts:
    def row(value: float, price: int = 10) -> dict[str, Any]:
        return {'status': 'available', 'value': value, 'price': price, 'observed_at': 100}
    base = {'defense_absolute': row(100.), 'defense_percent': row(50.), 'thorns': row(51.), 'health': row(100.),
            'damage': row(100.), 'attack_speed': row(2.), 'cash_per_wave': row(10.),
            'coins_per_kill_bonus': row(1.25), 'cash_bonus': row(1.25), 'coins_per_wave': row(10.)}
    base.update({uid: row(*value) for uid, value in rows.items()})
    return RouteFacts('account', 'Air_38', 'battle', 100, 101, run_id=7, wave=wave, battle_cash=100,
        enemy_damage=10.0, upgrade_rows=base, run_purchases={}, visit_id='visit')


@pytest.mark.parametrize('policy', ['opening', 'turtle'])
@pytest.mark.parametrize('lane', ['workshop', 'battle'])
def test_templates_validate_without_native_blocks(policy: str, lane: str) -> None:
    program = blocks.template_program(policy, lane)
    def walk(items: Any) -> None:
        for block in items:
            assert block['type'] != 'native'
            for children in blocks.child_lists(block):
                walk(children)
    walk(program)
    assert blocks.validate_program(list(program), lane) == program


def test_opening_fresh_account_buys_starter_damage() -> None:
    result = run('opening', 'workshop', workshop(prices={'damage': 10, 'attack_speed': 12}))
    assert result.trace.matched_rule_id == 'opening.starter' and result.decision.upgrade_id == 'damage'


def test_opening_economy_after_starters() -> None:
    done = {uid: 1 for uid in ('damage', 'attack_speed', 'health', 'unlock_defense_upgrades', 'defense_absolute')}
    sample = workshop(confirmed_purchases=done, purchases=done, utility_spent_coins=200,
                      prices={'unlock_cash_bonuses': 40})
    result = run('opening', 'workshop', sample)
    assert result.trace.matched_rule_id == 'opening.economy.pool' and result.decision.upgrade_id == 'unlock_cash_bonuses'


def test_turtle_objectives_after_budget() -> None:
    sample = workshop(utility_spent_coins=350, purchases={'unlock_defense_upgrades': 1},
                      prices={'defense_absolute': 50})
    result = run('turtle', 'workshop', sample)
    assert result.trace.matched_rule_id == 'turtle.objectives.pool' and result.decision.upgrade_id == 'defense_absolute'


def test_turtle_cheap_defense_only_while_saving_for_thorns() -> None:
    owned = {'unlock_defense_upgrades': 1, 'unlock_thorns': 1}
    saving = workshop(utility_spent_coins=350, wallet_coins=300, purchases=owned,
                      confirmed_purchases={'defense_absolute': 5}, values={'thorns': 7.},
                      prices={'defense_absolute': 254, 'thorns': 409})
    result = run('turtle', 'workshop', saving)
    assert result.trace.matched_rule_id == 'turtle.cheap_defense.pool' and result.decision.upgrade_id == 'defense_absolute'
    pricey = replace(saving, prices={'defense_absolute': 330, 'thorns': 409})  # 330 > 80% of 409
    assert run('turtle', 'workshop', pricey).decision.state == 'save_coins'


def test_turtle_battle_emergency_defense() -> None:
    result = run('turtle', 'battle', replace(battle(50), enemy_damage=200.0))  # 100 / (200 × 0.5) = 1.0 < 1.2
    assert result.trace.matched_rule_id == 'turtle.battle.emergency.buy' and result.decision.upgrade_id == 'defense_absolute'


def test_turtle_battle_emergency_waits_when_unaffordable() -> None:
    sample = replace(battle(50, defense_absolute=(100., 500)), enemy_damage=200.0)
    result = run('turtle', 'battle', sample)
    assert result.status == 'blocked' and result.trace.matched_rule_id == 'turtle.battle.emergency.wait'


def test_turtle_battle_waits_when_defense_percent_unknown() -> None:
    sample = battle(50)
    rows = dict(sample.upgrade_rows)
    rows.pop('defense_percent')
    assert run('turtle', 'battle', replace(sample, upgrade_rows=rows)).status == 'blocked'


def test_turtle_battle_economy_early() -> None:
    result = run('turtle', 'battle', battle(15, cash_per_wave=(5.,)))
    assert result.decision.upgrade_id == 'cash_per_wave'


def test_turtle_battle_thorns_step_for_wave() -> None:
    result = run('turtle', 'battle', battle(50, thorns=(15.,)))
    assert result.trace.matched_rule_id == 'turtle.battle.thorns21' and result.decision.target == 21


def test_opening_battle_promotes_survival_starters() -> None:
    result = run('opening', 'battle', battle(5, defense_absolute=(4.,)))
    assert result.decision.upgrade_id == 'defense_absolute' and result.decision.target == 10


def test_template_blocks_carry_readable_labels() -> None:
    assert blocks.template_program('turtle', 'workshop')[2]['label'] == 'Cheap defense'
    assert blocks.template_program('opening', 'workshop')[0]['label'] == 'Survival starter'
