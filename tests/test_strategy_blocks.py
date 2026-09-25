"""Executable strategy blocks share the worker and preview decision path."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from typing import Any
import pytest

from fleet.build_route_eval import RouteFacts
from fleet import strategy_blocks as blocks


def route(program: list[dict[str, Any]], *, lane: str='workshop', bans: tuple[str, ...]=()) -> SimpleNamespace:
    workshop = SimpleNamespace(id='workshop.default', mode='blocks', blocks=tuple(program),
        banned_upgrade_ids=frozenset(bans), coin_spend_limit_pct=100)
    battle = SimpleNamespace(mode='blocks', blocks=tuple(program))
    return SimpleNamespace(revision=1, workshop=workshop, battle=battle)


def facts() -> RouteFacts:
    return RouteFacts('account', 'Air_38', 'workshop', 100, 101,
        best_tier_1_wave=25, wallet_coins=100,
        prices={'damage':80, 'attack_speed':81, 'thorns':100},
        purchases={'unlock_defense_upgrades':1, 'unlock_thorns':1},
        confirmed_purchases={}, utility_spent_coins=400, visit_id='visit',
        price_evidence={uid:{'source':'observed','observed_at':99} for uid in ('damage','attack_speed','thorns')})


def pool(**extra: Any) -> dict[str, Any]:
    return {'id':'cheap', 'type':'pool', 'upgrade_ids':['damage','attack_speed'],
            'selection':'priority', **extra}


def test_program_rejects_unknown_unbounded_and_wrong_scope() -> None:
    with pytest.raises(ValueError):
        blocks.validate_program([{'id':'x','type':'repeat'}], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([pool(count_scope='run')], 'workshop')
    nested = [{'id':'leaf','type':'wait'}]
    for index in range(12):
        nested = [{'id':f'f{index}','type':'fallback','blocks':nested}]
    with pytest.raises(ValueError):
        blocks.validate_program(nested,'workshop')


def test_cheap_pool_inclusive_threshold_and_unknown_reference() -> None:
    program = [pool(discount_pct=20, reference_upgrade_id='thorns')]
    result = blocks.evaluate_program(route(program), facts(), None, 'workshop')
    assert result.decision.upgrade_id == 'damage'
    unknown = replace(facts(), prices={'damage':1})
    assert blocks.evaluate_program(route(program), unknown, None, 'workshop').decision.state == 'observe_price'


def test_caps_and_decay_use_confirmed_purchases_and_keep_pending() -> None:
    program = [pool(selection='weighted', weights={'damage':8,'attack_speed':4},
                    decay_pct=50, weight_floor=1, max_purchases=3)]
    first = blocks.evaluate_program(route(program), facts(), None, 'workshop')
    assert first.trace.eligible_odds == {'damage':2/3,'attack_speed':1/3}
    repeated = blocks.evaluate_program(route(program), replace(facts(), wallet_coins=99),first.pending,'workshop')
    assert repeated.pending == first.pending
    confirmed = replace(facts(), confirmed_purchases={'damage':1}, decision_sequence=1)
    changed = blocks.evaluate_program(route(program), confirmed,None,'workshop')
    assert changed.trace.eligible_odds == {'damage':.5,'attack_speed':.5}
    capped = replace(facts(), confirmed_purchases={'damage':3})
    assert blocks.evaluate_program(route(program),capped,None,'workshop').decision.upgrade_id == 'attack_speed'
    unknown = replace(facts(),confirmed_purchases=None)
    assert blocks.evaluate_program(route(program),unknown,None,'workshop').status == 'blocked'


def test_condition_unknown_does_not_take_else_and_bans_cover_children() -> None:
    program = [{'id':'when','type':'condition','field':'best_tier_1_wave','op':'gte','value':50,
                'then':[{'id':'buy','type':'buy','upgrade_id':'damage'}],
                'else':[{'id':'alt','type':'buy','upgrade_id':'attack_speed'}]}]
    assert blocks.evaluate_program(route(program),replace(facts(),best_tier_1_wave=None),None,'workshop').status == 'blocked'
    banned = [{'id':'buy','type':'buy','upgrade_id':'thorns'}]
    assert blocks.evaluate_program(route(banned,bans=['unlock_defense_upgrades']),facts(),None,'workshop').status == 'blocked'


def test_native_policy_uses_explicit_template_and_wave_sixty_guard() -> None:
    program = blocks.native_template_program('turtle','workshop')
    sample = replace(facts(), best_tier_1_wave=1, purchases={'unlock_defense_upgrades':1,'unlock_thorns':1},
                     prices={'defense_absolute':10,'thorns':50},values={'thorns':11})
    result = blocks.evaluate_program(route(program),sample,None,'workshop')
    assert result.decision.stage == 'turtle'
    assert blocks.evaluate_program(route(program),replace(sample,best_tier_1_wave=60),None,'workshop').decision.state == 'needs_operator'


def test_battle_counts_are_run_scoped_and_prices_must_be_fresh() -> None:
    program = [pool(selection='weighted',decay_pct=50,max_purchases=2,count_scope='run')]
    sample = replace(facts(),screen='battle',run_id=2,wave=5,battle_cash=100,
        run_purchases={'damage':2},upgrade_rows={uid:{'status':'available','value':1,'price':5,'observed_at':100}
                                                for uid in ('damage','attack_speed')})
    result = blocks.evaluate_program(route(program,lane='battle'),sample,None,'battle')
    assert result.decision.upgrade_id == 'attack_speed'
    assert blocks.evaluate_program(route(program,lane='battle'),replace(sample,now=103),None,'battle').status == 'unknown'


def test_purchase_counters_ignore_taps_failed_and_observed_unlocks(tmp_path: Path) -> None:
    import json
    import db
    from fleet.build_route_runtime import BuildRouteRuntime
    root = tmp_path / 'workers' / 'Air_38'
    root.mkdir(parents=True)
    db.bind_account(root/'tower_bot.db','account')
    with db.connect(root/'tower_bot.db') as conn:
        for verdict in ('bought','free','uncertain'):
            conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,detail) VALUES(1,'WORKSHOP_BUY','Damage','ATTACK','coins',0,?)",(json.dumps({'verdict':verdict}),))
        conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(1,2,1,'BattlePurchased',?)",(json.dumps({'upgrade_id':'damage'}),))
        conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(2,1,1,'BattlePurchased',?)",(json.dumps({'upgrade_id':'damage'}),))
        conn.execute("INSERT INTO events(seq,run_id,ts,type,action) VALUES(3,2,1,'Tapped','Damage')")
    runtime = BuildRouteRuntime(tmp_path,'Air_38','account')
    assert runtime.purchase_counts('workshop') == {'damage':2}
    assert runtime.purchase_counts('battle',2) == {'damage':1}


def test_dynamic_priority_reference_uses_unaffordable_first_target() -> None:
    program = [{'id':'top','type':'buy','upgrade_id':'thorns'}, pool(
        discount_pct=20, reference_upgrade_id='priority')]
    sample = replace(facts(),wallet_coins=90)
    assert blocks.evaluate_program(route(program),sample,None,'workshop').decision.upgrade_id == 'damage'


def test_pending_weighted_draw_cannot_skip_to_another_block_on_missing_price() -> None:
    program = [pool(selection='weighted'), {'id':'later','type':'buy','upgrade_id':'thorns'}]
    first = blocks.evaluate_program(route(program),facts(),None,'workshop')
    missing = replace(facts(),prices={'thorns':50})
    result = blocks.evaluate_program(route(program),missing,first.pending,'workshop')
    assert result.status == 'blocked'
    assert result.decision is None


def test_assigned_battle_blocks_reach_live_policy_and_count_purchase(tmp_path: Path) -> None:
    import json
    import time
    import db
    from account_state import AccountState
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.reroll_progress import RerollProgress
    from policy import AutopilotPolicy
    from tests.test_build_route_integration import _registered
    root = _registered(tmp_path,'Air_38','account')
    raw = RouteDocument.compatibility().to_dict()
    baseline = RouteDocument.compatibility().to_dict()['baseline']
    baseline['battle'].update(mode='blocks',blocks=[pool(max_purchases=1,count_scope='run')])
    raw['assignments']={'Air_38':{'account_id':'account','strategy_id':'test','strategy_name':'Test',
        'strategy_version':1,'baseline':baseline}}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw),0,'operator')
    progress = RerollProgress(root,'account',AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path,'Air_38','account')
    rows={uid:{'status':'available','value':1,'price':5,'observed_at':time.time()}
          for uid in ('damage','attack_speed')}
    first=progress.battle_policy(AutopilotPolicy(enabled=True),rows,run_id=2,wave=2,cash=100)
    assert first.rules[0].upgrade_id == 'damage'
    assert first.single_purchase and first.max_purchase_price == 5
    with db.connect(root/'tower_bot.db') as conn:
        conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(1,2,1,'BattlePurchased',?)",(json.dumps({'upgrade_id':'damage'}),))
    second=progress.battle_policy(AutopilotPolicy(enabled=True),rows,run_id=2,wave=2,cash=95)
    assert second.rules[0].upgrade_id == 'attack_speed'
    assert second.decision_token != first.decision_token


def test_block_workshop_price_bounds_live_shopping_policy(tmp_path: Path) -> None:
    import time
    from account_state import AccountState
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.reroll_progress import RerollProgress
    from strategy import Strategy
    from tests.test_build_route_integration import _registered
    root = _registered(tmp_path,'Air_38','account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['workshop'].update(mode='blocks',blocks=[pool(discount_pct=20,reference_upgrade_id='thorns')])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw),0,'operator')
    progress = RerollProgress(root,'account',AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path,'Air_38','account')
    progress._publish = lambda decision: None
    moment=time.time()
    sample=replace(facts(),now=moment,observed_at=moment)
    progress.route_facts=lambda: sample
    policy=progress.shopping_policy(replace(Strategy.from_config().shopping,enabled=True,armed=True,coin_budget=None))
    assert policy.workshop[0].name == 'Damage'
    assert policy.coin_budget == 80


def test_native_battle_preserves_target_for_live_value_recheck() -> None:
    program = blocks.native_template_program('turtle','battle')
    sample = replace(facts(),screen='battle',run_id=2,wave=2,battle_cash=100,run_purchases={},
        upgrade_rows={'damage':{'status':'available','value':5,'price':5,'observed_at':90}})
    result = blocks.evaluate_program(route(program,lane='battle'),sample,None,'battle')
    assert result.decision.upgrade_id == 'damage'
    assert result.decision.target == 12
    assert 'cached' in result.trace.price_source
    expired = replace(sample,upgrade_rows={'damage':{'status':'available','value':5,'price':5,'observed_at':40}})
    assert blocks.evaluate_program(route(program,lane='battle'),expired,None,'battle').status == 'blocked'


def test_native_groups_can_be_removed_and_reordered() -> None:
    native = list(blocks.native_template_program('opening','workshop'))
    sample = replace(facts(),best_tier_1_wave=1,purchases={},utility_spent_coins=0,
        prices={'damage':10,'attack_speed':10,'unlock_cash_bonuses':10,'cash_bonus':10,
                'cash_per_wave':10,'unlock_coin_bonuses':10,'coins_per_kill_bonus':10})
    full = blocks.evaluate_program(route(native),sample,None,'workshop')
    assert full.decision.starter
    economy_first = blocks.evaluate_program(route([native[1],native[0],native[2],native[3]]),sample,None,'workshop')
    assert not economy_first.decision.starter
    assert economy_first.decision.reason.startswith('Early utility allocation:')
    objectives_only = blocks.evaluate_program(route([native[2]]),sample,None,'workshop')
    assert objectives_only.decision.upgrade_id is not None
    assert not objectives_only.decision.starter
    assert not objectives_only.decision.reason.startswith('Early utility allocation:')


def test_native_phase_trace_marks_completion_and_next_phase() -> None:
    starter, economy, objectives, _fallback = blocks.native_template_program('opening', 'workshop')
    sample = replace(facts(), best_tier_1_wave=25, utility_spent_coins=0, wallet_coins=1000)
    result = blocks.evaluate_program(route([starter, economy, objectives]), sample, None, 'workshop')

    assert result.trace.phase_id == 'economy'
    assert result.trace.phase_state == 'waiting'
    assert result.trace.next_phase_id == 'opening.objectives'
    assert 'Survival Starter complete' in result.trace.transition_reason


def test_unaffordable_native_candidate_waits_without_handoff() -> None:
    _starter, economy, _objectives, _fallback = blocks.native_template_program('opening', 'workshop')
    sample = replace(facts(), best_tier_1_wave=25, utility_spent_coins=0,
                     wallet_coins=0, prices={'cash_per_wave': 80})
    result = blocks.evaluate_program(route([economy]), sample, None, 'workshop')

    assert result.trace.phase_id == 'economy'
    assert result.trace.phase_state == 'waiting'
    assert result.trace.next_phase_id is None


def test_missing_native_phase_evidence_waits() -> None:
    starter, economy, _objectives, _fallback = blocks.native_template_program('opening', 'workshop')
    sample = replace(facts(), best_tier_1_wave=1, utility_spent_coins=None, purchases={}, prices={})
    result = blocks.evaluate_program(route([starter, economy]), sample, None, 'workshop')

    assert result.trace.phase_id == 'starter'
    assert result.trace.phase_state == 'waiting'
    assert result.trace.next_phase_id == 'opening.economy'
    assert 'utility spend' in result.trace.transition_reason.lower()


def test_unknown_condition_halts_following_sibling_buy() -> None:
    program = [{'id':'guard','type':'condition','field':'best_tier_1_wave','op':'gte','value':50,
                'then':[],'else':[]}, {'id':'buy','type':'buy','upgrade_id':'damage'}]
    result=blocks.evaluate_program(route(program),replace(facts(),best_tier_1_wave=None),None,'workshop')
    assert result.status == 'blocked' and result.decision is None


def test_weight_decay_preserves_exact_fraction() -> None:
    program=[pool(selection='weighted',weights={'damage':8,'attack_speed':4},decay_pct=20)]
    result=blocks.evaluate_program(route(program),replace(facts(),confirmed_purchases={'damage':1}),None,'workshop')
    assert result.trace.eligible_odds['damage'] == pytest.approx(6.4/10.4)


def test_battle_pool_can_compare_to_native_unaffordable_priority() -> None:
    program=[pool(discount_pct=20,reference_upgrade_id='priority',count_scope='run'),
             *blocks.native_template_program('turtle','battle')]
    sample=replace(facts(),screen='battle',run_id=2,wave=2,battle_cash=90,run_purchases={},
        upgrade_rows={'defense_absolute':{'status':'unaffordable','value':0,'price':100,'observed_at':100},
                      'damage':{'status':'available','value':5,'price':80,'observed_at':100}})
    result=blocks.evaluate_program(route(program,lane='battle'),sample,None,'battle')
    assert result.trace.matched_rule_id == 'cheap'
    assert result.decision.upgrade_id == 'damage'


@pytest.mark.parametrize('policy,changes', [
    ('opening',{'best_tier_1_wave':1,'purchases':{},'utility_spent_coins':0,'prices':{'damage':10}}),
    ('opening',{'best_tier_1_wave':1,'purchases':{'damage':1,'attack_speed':1,'health':1,'unlock_defense_upgrades':1,'defense_absolute':1},'utility_spent_coins':0,'prices':{'unlock_cash_bonuses':40}}),
    ('opening',{'best_tier_1_wave':1,'purchases':{'damage':1,'attack_speed':1,'health':1,'unlock_defense_upgrades':1,'defense_absolute':1},'utility_spent_coins':0,'wallet_coins':20,'prices':{'unlock_cash_bonuses':40,'damage':3}}),
    ('opening',{'best_tier_1_wave':1,'purchases':{},'utility_spent_coins':400,'prices':{'damage':10,'attack_speed':12}}),
    ('turtle',{'utility_spent_coins':350,'purchases':{'unlock_defense_upgrades':1,'unlock_thorns':1,'defense_absolute':5,'thorns':7},'values':{'thorns':7.},'wallet_coins':300,'prices':{'defense_absolute':254,'thorns':409}}),
])
def test_complete_template_matches_native_decision(policy: str, changes: dict[str, Any]) -> None:
    from fleet.reroll_planner import RerollFacts, choose_next
    sample=replace(facts(),**changes)
    expected=choose_next(RerollFacts(sample.account_id,sample.best_tier_1_wave,sample.purchases,
        sample.values,sample.wallet_coins,sample.lifetime_coins,sample.prices,
        spend_fraction=1,variant=sample.variant,utility_spent_coins=sample.utility_spent_coins,policy=policy))
    actual=blocks.evaluate_program(route(blocks.native_template_program(policy,'workshop')),sample,None,'workshop')
    assert actual.decision == expected


def test_discount_requires_observed_quote_but_not_wall_clock_freshness() -> None:
    program=[pool(discount_pct=20,reference_upgrade_id='thorns')]
    sample=replace(facts(),price_evidence={uid:{'source':'observed','observed_at':1}
        for uid in ('damage','attack_speed','thorns')})
    result=blocks.evaluate_program(route(program),sample,None,'workshop')
    assert result.decision.state == 'buy'
    estimate=replace(sample,price_evidence={**sample.price_evidence,'thorns':{'source':'catalog_estimate','observed_at':1}})
    needed=blocks.evaluate_program(route(program),estimate,None,'workshop')
    assert needed.decision.state == 'observe_price'
    assert needed.decision.stage == 'strategy_observe'
    assert 'thorns' in needed.trace.observation_ids


def test_nested_discount_uses_nearest_sibling_priority() -> None:
    program=[{'id':'group','type':'fallback','blocks':[
        {'id':'top','type':'buy','upgrade_id':'thorns'},pool(discount_pct=20,reference_upgrade_id='priority')]}]
    result=blocks.evaluate_program(route(program),replace(facts(),wallet_coins=90),None,'workshop')
    assert result.decision.upgrade_id == 'damage'


def test_discount_observation_opens_workshop_with_zero_spend_budget(tmp_path: Path) -> None:
    import time
    from account_state import AccountState
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.reroll_progress import RerollProgress
    from strategy import Strategy
    from tests.test_build_route_integration import _registered
    root = _registered(tmp_path,'Air_38','account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['workshop'].update(mode='blocks',blocks=[pool(discount_pct=20,reference_upgrade_id='thorns')])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw),0,'operator')
    progress = RerollProgress(root,'account',AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path,'Air_38','account')
    progress._publish = lambda decision: None
    moment=time.time()
    sample=replace(facts(),now=moment,observed_at=moment,price_evidence={})
    progress.route_facts=lambda: sample
    base = Strategy.from_config().shopping
    progress.lab_cadence.slot2_owned = lambda: True
    policy=progress.shopping_policy(replace(base,enabled=True,armed=True,coin_budget=None,
        cards=replace(base.cards,enabled=True)))
    assert policy.enabled and policy.coin_budget == 0 and not policy.allow_unlocks
    assert not policy.cards.enabled
    assert {row.name for row in policy.workshop} == {'Thorns','Damage','Attack Speed'}


def battle_facts(**rows: dict[str, Any]) -> RouteFacts:
    base = {uid: {'status': 'available', 'value': 1.0, 'price': 10, 'observed_at': 100}
            for uid in ('defense_absolute', 'defense_percent', 'thorns', 'health')}
    base.update(rows)
    return RouteFacts('account', 'Air_38', 'battle', 100, 101, run_id=7, wave=30,
        battle_cash=100, enemy_damage=100.0, upgrade_rows=base, run_purchases={}, visit_id='visit')


def when(field: str, op: str, value: float, **extra: Any) -> dict[str, Any]:
    return {'id': 'when', 'type': 'condition', 'field': field, 'op': op, 'value': value, **extra,
            'then': [{'id': 'yes', 'type': 'buy', 'upgrade_id': 'defense_absolute'}],
            'else': [{'id': 'no', 'type': 'buy', 'upgrade_id': 'health'}]}


def test_condition_validates_new_fields_and_lanes() -> None:
    blocks.validate_program([when('def_abs_coverage', 'lt', 1.2)], 'battle')
    blocks.validate_program([when('upgrade_value', 'gt', 11, upgrade_id='thorns')], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([when('def_abs_coverage', 'lt', 1.2)], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([when('upgrade_value', 'gt', 11)], 'battle')
    with pytest.raises(ValueError):
        blocks.validate_program([when('wallet', 'gte', 5, upgrade_id='thorns')], 'battle')
    with pytest.raises(ValueError):
        blocks.validate_program([when('wallet', 'eq', 5)], 'battle')
    with pytest.raises(ValueError):
        blocks.validate_program([when('wallet', 'gte', float('nan'))], 'battle')


def test_def_abs_coverage_uses_post_mitigation_damage() -> None:
    program = [when('def_abs_coverage', 'lt', 1.2)]
    # 100 damage × (1 - 50%) = 50 remaining; 55 / 50 = 1.1 < 1.2 → then
    low = battle_facts(defense_absolute={'status': 'available', 'value': 55.0, 'price': 10, 'observed_at': 100},
                       defense_percent={'status': 'available', 'value': 50.0, 'price': 10, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), low, None, 'battle').decision.upgrade_id == 'defense_absolute'
    high = replace(low, enemy_damage=10.0)
    assert blocks.evaluate_program(route(program, lane='battle'), high, None, 'battle').decision.upgrade_id == 'health'
    immune = battle_facts(defense_percent={'status': 'available', 'value': 100.0, 'price': 10, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), immune, None, 'battle').decision.upgrade_id == 'health'


def test_def_abs_coverage_unknown_waits() -> None:
    program = [when('def_abs_coverage', 'lt', 1.2)]
    unknown = replace(battle_facts(), enemy_damage=None)
    assert blocks.evaluate_program(route(program, lane='battle'), unknown, None, 'battle').status == 'blocked'


def test_upgrade_value_condition_and_strict_ops() -> None:
    program = [when('upgrade_value', 'lt', 11, upgrade_id='thorns')]
    at_eleven = battle_facts(thorns={'status': 'available', 'value': 11.0, 'price': 10, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), at_eleven, None, 'battle').decision.upgrade_id == 'health'
    below = battle_facts(thorns={'status': 'available', 'value': 10.0, 'price': 10, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), below, None, 'battle').decision.upgrade_id == 'defense_absolute'


def test_program_upgrade_ids_include_condition_inputs() -> None:
    program = blocks.validate_program([when('def_abs_coverage', 'lt', 1.2),
        {**when('upgrade_value', 'lt', 11, upgrade_id='thorns'), 'id': 'when2',
         'then': [{'id': 'w', 'type': 'wait'}], 'else': []}], 'battle')
    ids = blocks.program_upgrade_ids(program)
    assert {'defense_absolute', 'defense_percent', 'thorns'} <= set(ids)


def test_pool_new_options_validate() -> None:
    blocks.validate_program([pool(targets={'damage': 12.5}, level_caps={'damage': {'base': 2, 'per_level_of': 'coins_per_wave'}},
                                  price_cap=75, wallet_share_pct=20)], 'workshop')
    for bad in ({'targets': {'thorns': 1}}, {'targets': {'damage': float('inf')}},
                {'level_caps': {'damage': {'base': 2, 'step': 2}}}, {'level_caps': {'damage': {'base': 2, 'x': 1}}},
                {'price_cap': 0}, {'wallet_share_pct': 101}):
        with pytest.raises(ValueError):
            blocks.validate_program([pool(**bad)], 'workshop')


def test_pool_target_skips_reached_and_unknown_values() -> None:
    reached = replace(facts(), values={'damage': 12.0})
    program = [pool(targets={'damage': 12})]
    assert blocks.evaluate_program(route(program), reached, None, 'workshop').decision.upgrade_id == 'attack_speed'
    unknown = replace(facts(), values={})
    result = blocks.evaluate_program(route(program), unknown, None, 'workshop')
    assert result.decision.upgrade_id == 'attack_speed'
    assert any('target value unknown' in item for item in result.trace.rejected)


def test_pool_linked_level_cap() -> None:
    program = [pool(level_caps={'damage': {'base': 2, 'per_level_of': 'coins_per_wave'}})]
    capped = replace(facts(), confirmed_purchases={'damage': 2})
    assert blocks.evaluate_program(route(program), capped, None, 'workshop').decision.upgrade_id == 'attack_speed'
    raised = replace(facts(), confirmed_purchases={'damage': 2, 'coins_per_wave': 1})
    assert blocks.evaluate_program(route(program), raised, None, 'workshop').decision.upgrade_id == 'damage'


def test_pool_price_cap_and_wallet_share() -> None:
    assert blocks.evaluate_program(route([pool(price_cap=80)]), facts(), None, 'workshop').decision.upgrade_id == 'damage'
    assert blocks.evaluate_program(route([pool(price_cap=79)]), facts(), None, 'workshop').status == 'blocked'
    rich = replace(facts(), wallet_coins=400)  # 20% of 400 = 80
    assert blocks.evaluate_program(route([pool(wallet_share_pct=20)]), rich, None, 'workshop').decision.upgrade_id == 'damage'
    poorer = replace(facts(), wallet_coins=399)
    assert blocks.evaluate_program(route([pool(wallet_share_pct=20)]), poorer, None, 'workshop').status == 'blocked'


def test_battle_pool_target_is_passed_to_decision() -> None:
    program = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['thorns'], 'selection': 'priority', 'targets': {'thorns': 21}}]
    result = blocks.evaluate_program(route(program, lane='battle'), battle_facts(), None, 'battle')
    assert result.decision.upgrade_id == 'thorns' and result.decision.target == 21


def budget(**extra: Any) -> dict[str, Any]:
    return {'id': 'econ', 'type': 'budget', 'metric': 'utility_spent', 'target': 350, 'ceiling': 400,
            'blocks': [pool()], **extra}


def test_budget_validation() -> None:
    blocks.validate_program([budget()], 'workshop')
    for bad in ({'metric': 'gems'}, {'target': 500}, {'blocks': []}, {'target': 0}):
        with pytest.raises(ValueError):
            blocks.validate_program([budget(**bad)], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([budget()], 'battle')


def test_budget_runs_children_until_target() -> None:
    running = replace(facts(), utility_spent_coins=300)
    assert blocks.evaluate_program(route([budget()]), running, None, 'workshop').decision.upgrade_id == 'damage'
    done = replace(facts(), utility_spent_coins=350)
    after = [budget(), {'id': 'next', 'type': 'buy', 'upgrade_id': 'attack_speed'}]
    assert blocks.evaluate_program(route(after), done, None, 'workshop').decision.upgrade_id == 'attack_speed'


def test_budget_rejects_items_over_ceiling() -> None:
    near = replace(facts(), utility_spent_coins=321)  # room 79: damage 80 rejected, attack_speed 81 rejected
    result = blocks.evaluate_program(route([budget()]), near, None, 'workshop')
    assert result.status == 'blocked'
    assert any('exceeds budget ceiling' in item for item in result.trace.rejected)


def test_budget_unknown_spend_waits() -> None:
    unknown = replace(facts(), utility_spent_coins=None)
    after = [budget(), {'id': 'next', 'type': 'buy', 'upgrade_id': 'attack_speed'}]
    result = blocks.evaluate_program(route(after), unknown, None, 'workshop')
    assert result.status == 'blocked' and result.trace.matched_rule_id == 'econ'


def save_goal(ids: list[str], **extra: Any) -> dict[str, Any]:
    return {'id': 'goal', 'type': 'save_for',
            'goal': [{'id': 'goal.pool', 'type': 'pool', 'upgrade_ids': ids, 'selection': 'priority', **extra}]}


def test_save_for_validation() -> None:
    blocks.validate_program([save_goal(['thorns'])], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([{'id': 'g', 'type': 'save_for', 'goal': []}], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([{'id': 'g', 'type': 'save_for', 'goal': [{'id': 'w', 'type': 'wait'}]}], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([save_goal(['thorns'], discount_pct=20)], 'workshop')
    with pytest.raises(ValueError):
        blocks.validate_program([{'id': 'ws', 'type': 'while_saving', 'blocks': []}], 'workshop')


def test_save_for_buys_goal_when_affordable() -> None:
    rich = replace(facts(), wallet_coins=200)
    assert blocks.evaluate_program(route([save_goal(['thorns'])]), rich, None, 'workshop').decision.upgrade_id == 'thorns'


def test_save_for_records_intent_and_lower_block_buys() -> None:
    program = [save_goal(['thorns']), {'id': 'cheap', 'type': 'buy', 'upgrade_id': 'damage'}]
    result = blocks.evaluate_program(route(program), facts(), None, 'workshop')  # wallet 100, thorns 100 → affordable
    assert result.decision.upgrade_id == 'thorns'
    poor = replace(facts(), wallet_coins=90)
    result = blocks.evaluate_program(route(program), poor, None, 'workshop')
    assert result.decision.upgrade_id == 'damage'


def test_save_for_result_when_nothing_else_buys() -> None:
    poor = replace(facts(), wallet_coins=50)
    result = blocks.evaluate_program(route([save_goal(['thorns'])]), poor, None, 'workshop')
    assert result.status == 'blocked'
    assert result.decision.state == 'save_coins' and result.decision.upgrade_id == 'thorns'
    assert result.decision.price == 100 and 'Saving for' in result.decision.reason


def test_only_one_goal_saves_at_a_time() -> None:
    poor = replace(facts(), wallet_coins=90)
    program = [save_goal(['thorns']), {**save_goal(['damage']), 'id': 'goal2',
               'goal': [{'id': 'goal2.pool', 'type': 'pool', 'upgrade_ids': ['damage'], 'selection': 'priority'}]}]
    result = blocks.evaluate_program(route(program), poor, None, 'workshop')
    assert result.decision.state == 'save_coins' and result.decision.upgrade_id == 'thorns'


def test_satisfied_goal_passes_silently() -> None:
    done = replace(facts(), values={'thorns': 51.0})
    program = [save_goal(['thorns'], targets={'thorns': 51}), {'id': 'next', 'type': 'buy', 'upgrade_id': 'damage'}]
    assert blocks.evaluate_program(route(program), done, None, 'workshop').decision.upgrade_id == 'damage'


def test_save_for_ignores_items_over_budget_ceiling() -> None:
    program = [{'id': 'econ', 'type': 'budget', 'metric': 'utility_spent', 'target': 350, 'ceiling': 400,
                'blocks': [save_goal(['thorns'])]}, {'id': 'next', 'type': 'buy', 'upgrade_id': 'damage'}]
    near = replace(facts(), utility_spent_coins=340, wallet_coins=90)  # room 60 < thorns 100
    result = blocks.evaluate_program(route(program), near, None, 'workshop')
    assert result.decision.state == 'buy' and result.decision.upgrade_id == 'damage'


def test_while_saving_only_runs_during_matching_goal() -> None:
    poor = replace(facts(), wallet_coins=90)
    filler = {'id': 'ws', 'type': 'while_saving', 'upgrade_id': 'thorns',
              'blocks': [{'id': 'fill', 'type': 'buy', 'upgrade_id': 'damage'}]}
    assert blocks.evaluate_program(route([save_goal(['thorns']), filler]), poor, None, 'workshop').decision.upgrade_id == 'damage'
    other = [save_goal(['attack_speed']), filler]
    poorer = replace(facts(), wallet_coins=50)
    assert blocks.evaluate_program(route(other), poorer, None, 'workshop').decision.state == 'save_coins'
    rich = replace(facts(), wallet_coins=200)
    assert blocks.evaluate_program(route([filler]), rich, None, 'workshop').status == 'blocked'


def test_save_for_buy_goal_saves_when_unaffordable() -> None:
    poor = replace(facts(), wallet_coins=50)
    program = [{'id': 'g', 'type': 'save_for', 'goal': [{'id': 'g.buy', 'type': 'buy', 'upgrade_id': 'thorns'}]}]
    result = blocks.evaluate_program(route(program), poor, None, 'workshop')
    assert result.status == 'blocked'
    assert result.decision.state == 'save_coins' and result.decision.upgrade_id == 'thorns'
    assert result.decision.price == 100


def test_while_saving_without_upgrade_id_matches_any_goal() -> None:
    poor = replace(facts(), wallet_coins=90)
    filler = {'id': 'ws', 'type': 'while_saving', 'blocks': [{'id': 'fill', 'type': 'buy', 'upgrade_id': 'damage'}]}
    result = blocks.evaluate_program(route([save_goal(['thorns']), filler]), poor, None, 'workshop')
    assert result.decision.upgrade_id == 'damage'


def test_save_for_records_intent_in_battle_when_unaffordable() -> None:
    unaffordable = {'thorns': {'status': 'unaffordable', 'value': 1.0, 'price': 500, 'observed_at': 100}}
    program = [save_goal(['thorns']), {'id': 'ws', 'type': 'while_saving', 'blocks': [{'id': 'fill', 'type': 'buy', 'upgrade_id': 'health'}]}]
    result = blocks.evaluate_program(route(program, lane='battle'), battle_facts(**unaffordable), None, 'battle')
    assert result.decision.upgrade_id == 'health'
    without_filler = blocks.evaluate_program(route([save_goal(['thorns'])], lane='battle'), battle_facts(**unaffordable), None, 'battle')
    assert without_filler.status == 'blocked'
    assert without_filler.trace.matched_rule_id == 'goal'
    assert 'Saving for' in without_filler.trace.reason
