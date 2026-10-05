"""Executable strategy blocks share the worker and preview decision path."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from typing import Any
import pytest
import config

from fleet.build_route import CoinRules, RouteRules
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


@pytest.mark.parametrize('prices', [{'wall_health': 1}, {}])
def test_late_locked_workshop_skill_is_neither_bought_nor_requested_for_observation(prices: dict[str, int]) -> None:
    program = [pool(upgrade_ids=['wall_health'])]
    result = blocks.evaluate_program(route(program), replace(facts(), prices=prices), None, 'workshop')
    assert result.decision is None
    assert 'wall_health' not in result.trace.observation_ids


def test_late_group_purchase_proof_enables_sibling_in_workshop_block() -> None:
    program = [pool(upgrade_ids=['wall_health'])]
    sample = replace(facts(), prices={'wall_health': 1}, purchases={'wall_rebuild': 1})
    result = blocks.evaluate_program(route(program), sample, None, 'workshop')
    assert result.decision.upgrade_id == 'wall_health'


def test_a_future_unlock_block_cannot_seek_past_the_next_unlock_group() -> None:
    program = [pool(upgrade_ids=['unlock_orbs'])]
    result = blocks.evaluate_program(route(program), replace(facts(), prices={'unlock_orbs': 1}), None, 'workshop')
    assert result.decision is None


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


def test_native_policy_uses_explicit_template_and_plans_past_wave_sixty() -> None:
    program = blocks.native_template_program('turtle','workshop')
    sample = replace(facts(), best_tier_1_wave=1, purchases={'unlock_defense_upgrades':1,'unlock_thorns':1},
                     prices={'defense_absolute':10,'thorns':50},values={'thorns':11})
    result = blocks.evaluate_program(route(program),sample,None,'workshop')
    assert result.decision.stage == 'turtle'
    # Wave 60 unlocks Ultimate Weapons but does not pay the 5 stones the
    # first one costs, so the Workshop keeps its plan.
    past = blocks.evaluate_program(route(program),replace(sample,best_tier_1_wave=60),None,'workshop')
    assert past.decision.stage == 'turtle' and past.decision.state != 'needs_operator'


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
    from tests.test_reroll_progress import complete_starter
    root = _registered(tmp_path,'Air_38','account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['workshop'].update(mode='blocks',blocks=[pool(discount_pct=20,reference_upgrade_id='thorns')])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw),0,'operator')
    progress = RerollProgress(root,'account',AccountState())
    complete_starter(progress)
    assert not progress.initial_workshop_due()
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
                      'damage':{'status':'available','value':5,'price':80,'observed_at':100},
                      'attack_speed':{'status':'unaffordable','value':1,'price':95,'observed_at':100}})
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
    progress.lab_cadence.slot_owned = lambda slot: True
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
    assert result.decision.upgrade_id == 'damage'
    assert any('treated as not reached' in item for item in result.trace.rejected)


def test_battle_pool_target_unknown_value_is_skipped() -> None:
    program = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['thorns', 'health'], 'selection': 'priority',
                'targets': {'thorns': 21}}]
    rows = battle_facts(thorns={'status': 'available', 'value': None, 'price': 10, 'observed_at': 100})
    result = blocks.evaluate_program(route(program, lane='battle'), rows, None, 'battle')
    assert result.decision.upgrade_id == 'health'
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


def test_saved_priority_goal_buys_affordable_lower_item() -> None:
    sample = replace(facts(), wallet_coins=100,
                     prices={'damage': 120, 'attack_speed': 40})
    result = blocks.evaluate_program(route([save_goal(['damage', 'attack_speed'])]),
                                     sample, None, 'workshop')
    assert result.decision is not None
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'attack_speed')


def test_saved_priority_goal_can_buy_after_an_earlier_saving_goal() -> None:
    first = save_goal(['thorns'])
    second = {**save_goal(['damage', 'attack_speed']), 'id': 'later'}
    sample = replace(facts(), wallet_coins=90)
    result = blocks.evaluate_program(route([first, second]), sample, None, 'workshop')
    assert result.decision is not None
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'damage')


def test_saved_builtin_fallback_spends_without_a_saving_goal() -> None:
    filler_ids = ['cash_per_wave', 'coins_per_kill_bonus', 'cash_bonus',
                  'damage', 'attack_speed']
    legacy_fallback = {'id': 'opening.filler', 'type': 'while_saving',
        'label': 'Filler while saving', 'blocks': [
            {'id': 'opening.filler.pool', 'type': 'pool', 'selection': 'priority',
             'upgrade_ids': filler_ids, 'wallet_share_pct': 20,
             'level_caps': {uid: {'base': cap} for uid, cap in zip(
                 filler_ids, [5, 5, 5, 3, 3])}}]}
    sample = replace(facts(), wallet_coins=100,
                     purchases={**facts().purchases, 'unlock_cash_bonuses': 1},
                     prices={'cash_per_wave': 120, 'coins_per_kill_bonus': 120,
                             'cash_bonus': 40})
    result = blocks.evaluate_program(route([legacy_fallback]), sample, None, 'workshop')
    assert result.decision is not None
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'cash_bonus')

    held_sample = replace(sample, prices={**sample.prices, 'coins_per_wave': 120})
    during_hold = blocks.evaluate_program(held([legacy_fallback]), held_sample, None, 'workshop')
    assert during_hold.decision is not None
    assert (during_hold.decision.state, during_hold.decision.upgrade_id) == ('buy', 'cash_bonus')

    edited = {**legacy_fallback, 'blocks': [{**legacy_fallback['blocks'][0],
                                            'wallet_share_pct': 30}]}
    restricted = blocks.evaluate_program(route([edited]), sample, None, 'workshop')
    assert restricted.decision is None


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


def test_builtin_turtle_buys_affordable_lower_priority_attack() -> None:
    sample = replace(facts(), wallet_coins=100,
                     prices={'damage': 120, 'attack_speed': 40, 'thorns': 200})
    program = blocks.template_program('turtle', 'workshop')
    result = blocks.evaluate_program(route(list(program)), sample, None, 'workshop')
    assert result.decision is not None
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'attack_speed')


def test_builtin_opening_spends_on_affordable_fallback_after_objectives() -> None:
    purchases = {'damage': 5, 'attack_speed': 5, 'health': 1,
                 'unlock_defense_upgrades': 1, 'defense_absolute': 5,
                 'unlock_thorns': 1, 'unlock_cash_bonuses': 1,
                 'unlock_coin_bonuses': 1, 'coins_per_wave': 3}
    sample = replace(facts(), wallet_coins=100, utility_spent_coins=350,
                     purchases=purchases, confirmed_purchases=purchases,
                     values={'thorns': 51},
                     prices={'cash_per_wave': 120, 'coins_per_kill_bonus': 120,
                             'cash_bonus': 40})
    program = blocks.template_program('opening', 'workshop')
    result = blocks.evaluate_program(route(list(program)), sample, None, 'workshop')
    assert result.decision is not None
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'cash_bonus')


@pytest.mark.parametrize('policy', ['opening', 'turtle'])
def test_wave_450_switches_workshop_from_def_abs_to_blender_unlocks(policy: str) -> None:
    sample = replace(facts(), best_tier_1_wave=449, utility_spent_coins=400,
                     prices={'damage': 200, 'attack_speed': 200, 'health': 200,
                             'defense_absolute': 1, 'unlock_lifesteal': 40},
                     purchases={'unlock_defense_upgrades': 1, 'unlock_thorns': 1})
    program = blocks.template_program(policy, 'workshop')
    before = blocks.evaluate_program(route(list(program)), sample, None, 'workshop')
    after = blocks.evaluate_program(route(list(program)),
                                    replace(sample, best_tier_1_wave=450), None, 'workshop')
    assert before.decision.upgrade_id == 'defense_absolute'
    assert (after.decision.state, after.decision.upgrade_id) == ('buy', 'unlock_lifesteal')


@pytest.mark.parametrize('policy', ['opening', 'turtle'])
@pytest.mark.parametrize('owned,expected', [
    (('unlock_defense_upgrades', 'unlock_thorns', 'unlock_lifesteal'), 'unlock_knockback'),
    (('unlock_defense_upgrades', 'unlock_thorns', 'unlock_lifesteal', 'unlock_knockback'), 'unlock_orbs'),
])
def test_blender_unlocks_the_defense_path_in_order(policy: str, owned: tuple[str, ...], expected: str) -> None:
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     purchases={uid: 1 for uid in owned},
                     prices={'attack_speed': 200, 'unlock_knockback': 40, 'unlock_orbs': 40,
                             'defense_absolute': 1})
    result = blocks.evaluate_program(route(list(blocks.template_program(policy, 'workshop'))),
                                     sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', expected)


def test_blender_uses_lower_affordable_priority_then_reaches_bounce_unlock() -> None:
    owned = {uid: 1 for uid in ('unlock_defense_upgrades', 'unlock_thorns',
             'unlock_lifesteal', 'unlock_knockback', 'unlock_orbs',
             'unlock_range_upgrades', 'unlock_multishot', 'unlock_rapid_fire')}
    prices = {'attack_speed': 200, 'knockback_force': 200,
              'multishot_chance': 200, 'multishot_targets': 200,
              'knockback_chance': 200, 'orbs': 200, 'unlock_bounce_shot': 40,
              'defense_absolute': 1}
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     purchases=owned, prices=prices)
    result = blocks.evaluate_program(route(list(blocks.template_program('turtle', 'workshop'))),
                                     sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_bounce_shot')


def test_blender_prefers_attack_speed_and_never_falls_back_to_def_abs() -> None:
    program = blocks.template_program('turtle', 'workshop')
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     prices={'attack_speed': 20, 'defense_absolute': 1})
    first = blocks.evaluate_program(route(list(program)), sample, None, 'workshop')
    assert (first.decision.state, first.decision.upgrade_id) == ('buy', 'attack_speed')

    no_attack = blocks.evaluate_program(route(list(program)),
        replace(sample, prices={'attack_speed': 200, 'defense_absolute': 1}), None, 'workshop')
    assert no_attack.decision is None or no_attack.decision.upgrade_id != 'defense_absolute'


def test_blender_prefers_knockback_force_then_multishot_unlock() -> None:
    owned = {uid: 1 for uid in ('unlock_defense_upgrades', 'unlock_thorns',
             'unlock_lifesteal', 'unlock_knockback', 'unlock_orbs',
             'unlock_range_upgrades')}
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     purchases=owned, prices={'attack_speed': 200,
                         'knockback_force': 20, 'unlock_multishot': 30,
                         'defense_absolute': 1})
    program = blocks.template_program('opening', 'workshop')
    first = blocks.evaluate_program(route(list(program)), sample, None, 'workshop')
    assert (first.decision.state, first.decision.upgrade_id) == ('buy', 'knockback_force')
    later = blocks.evaluate_program(route(list(program)),
        replace(sample, prices={**sample.prices, 'knockback_force': 200}), None, 'workshop')
    assert (later.decision.state, later.decision.upgrade_id) == ('buy', 'unlock_multishot')


@pytest.mark.parametrize('policy', ['opening', 'turtle'])
def test_pinned_legacy_builtin_uses_blender_after_wave_450(policy: str) -> None:
    legacy = blocks.validate_program(blocks._workshop_template(policy), 'workshop')
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     purchases={'unlock_defense_upgrades': 1, 'unlock_thorns': 1},
                     prices={'attack_speed': 200, 'unlock_lifesteal': 40, 'defense_absolute': 1})
    result = blocks.evaluate_program(route(list(legacy)), sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_lifesteal')


@pytest.mark.parametrize('policy', ['opening', 'turtle'])
def test_older_saved_builtin_shape_uses_blender_after_wave_450(policy: str) -> None:
    old = ([{'id': 'opening.starter', 'type': 'buy', 'upgrade_id': 'damage'}]
           if policy == 'opening' else [])
    old += [
        {'id': f'{policy}.economy', 'type': 'budget', 'metric': 'utility_spent',
         'target': 350, 'ceiling': 400, 'blocks': [
             {'id': f'{policy}.economy.goal', 'type': 'buy', 'upgrade_id': 'cash_bonus'}]},
    ]
    if policy == 'turtle':
        old.append({'id': 'turtle.attack', 'type': 'save_for', 'goal': [
            {'id': 'turtle.attack.pool', 'type': 'pool', 'upgrade_ids': ['damage', 'attack_speed']}]})
    old.append({'id': f'{policy}.objectives', 'type': 'save_for', 'goal': [
        {'id': f'{policy}.objectives.pool', 'type': 'pool', 'upgrade_ids': ['defense_absolute']}]})
    if policy == 'turtle':
        old.extend([
            {'id': 'turtle.unlock_filler', 'type': 'while_saving', 'blocks': [
                {'id': 'turtle.unlock_filler.pool', 'type': 'pool', 'upgrade_ids': ['damage']}]},
            {'id': 'turtle.cheap_defense', 'type': 'while_saving', 'blocks': [
                {'id': 'turtle.cheap_defense.pool', 'type': 'pool', 'upgrade_ids': ['defense_absolute']}]},
        ])
    old.append({'id': f'{policy}.filler', 'type': 'while_saving', 'blocks': [
        {'id': f'{policy}.filler.pool', 'type': 'pool', 'upgrade_ids': ['damage']}]})
    legacy = blocks.validate_program(old, 'workshop')
    sample = replace(facts(), best_tier_1_wave=450, utility_spent_coins=400,
                     purchases={'unlock_defense_upgrades': 1, 'unlock_thorns': 1},
                     prices={'damage': 200, 'attack_speed': 200, 'unlock_lifesteal': 40,
                             'defense_absolute': 1})
    result = blocks.evaluate_program(route(list(legacy)), sample, None, 'workshop')
    assert (result.decision.state, result.decision.upgrade_id) == ('buy', 'unlock_lifesteal')


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


def test_block_label_validates_and_has_no_evaluation_effect() -> None:
    labeled = pool(label='Survival starter')
    blocks.validate_program([labeled], 'workshop')
    for bad in ('', '   ', 'x' * 61, 5):
        with pytest.raises(ValueError):
            blocks.validate_program([pool(label=bad)], 'workshop')
    unlabeled_result = blocks.evaluate_program(route([pool()]), facts(), None, 'workshop')
    labeled_result = blocks.evaluate_program(route([labeled]), facts(), None, 'workshop')
    assert unlabeled_result.decision.upgrade_id == labeled_result.decision.upgrade_id
    assert unlabeled_result.status == labeled_result.status


def test_pool_limit_skips_are_traced() -> None:
    result = blocks.evaluate_program(route([pool(price_cap=79)]), facts(), None, 'workshop')
    assert 'cheap: damage over price cap' in result.trace.rejected
    assert 'cheap: attack_speed over price cap' in result.trace.rejected
    poorer = replace(facts(), wallet_coins=399)
    result = blocks.evaluate_program(route([pool(wallet_share_pct=20)]), poorer, None, 'workshop')
    assert 'cheap: damage over wallet share' in result.trace.rejected
    capped = replace(facts(), confirmed_purchases={'damage': 1, 'attack_speed': 1})
    result = blocks.evaluate_program(route([pool(max_purchases=1)]), capped, None, 'workshop')
    assert 'cheap: damage max purchases reached' in result.trace.rejected
    result = blocks.evaluate_program(route([pool(level_caps={'damage': {'base': 1}, 'attack_speed': {'base': 1}})]),
                                     capped, None, 'workshop')
    assert 'cheap: damage level cap reached' in result.trace.rejected
    reached = replace(facts(), values={'damage': 12.0})
    result = blocks.evaluate_program(route([pool(targets={'damage': 12})]), reached, None, 'workshop')
    assert 'cheap: damage target reached' in result.trace.rejected
    result = blocks.evaluate_program(route([pool(discount_pct=20, reference_upgrade_id='thorns')]),
                                     replace(facts(), prices={'damage': 90, 'attack_speed': 81, 'thorns': 100}),
                                     None, 'workshop')
    assert 'cheap: damage over discount limit' in result.trace.rejected


def test_battle_goal_with_price_cap_saves_for_unaffordable_item() -> None:
    unaffordable = {'thorns': {'status': 'unaffordable', 'value': 1.0, 'price': 400, 'observed_at': 100}}
    program = [save_goal(['thorns'], price_cap=500)]
    result = blocks.evaluate_program(route(program, lane='battle'), battle_facts(**unaffordable), None, 'battle')
    assert result.status == 'blocked' and result.trace.matched_rule_id == 'goal'
    assert 'Saving for' in result.trace.reason


def test_goal_wallet_share_does_not_block_saving() -> None:
    poor = replace(facts(), wallet_coins=90)  # 20% of 90 = 18 < thorns 100
    result = blocks.evaluate_program(route([save_goal(['thorns'], wallet_share_pct=20)]), poor, None, 'workshop')
    assert result.decision.state == 'save_coins' and result.decision.upgrade_id == 'thorns'
    over_share = replace(facts(), wallet_coins=200)  # affordable, but 100 > 20% of 200
    result = blocks.evaluate_program(route([save_goal(['thorns'], wallet_share_pct=20)]), over_share, None, 'workshop')
    assert result.decision.state == 'save_coins' and result.decision.upgrade_id == 'thorns'
    within_share = replace(facts(), wallet_coins=500)
    result = blocks.evaluate_program(route([save_goal(['thorns'], wallet_share_pct=20)]), within_share, None, 'workshop')
    assert result.decision.state == 'buy' and result.decision.upgrade_id == 'thorns'


def test_def_abs_coverage_with_locked_defense_percent_uses_zero() -> None:
    program = [when('def_abs_coverage', 'lt', 1.2)]
    locked = battle_facts(defense_absolute={'status': 'available', 'value': 100.0, 'price': 10, 'observed_at': 100},
                          defense_percent={'status': 'locked', 'value': None, 'price': None, 'observed_at': 100})
    covered = replace(locked, enemy_damage=50.0)  # 100 / 50 = 2.0
    assert blocks.evaluate_program(route(program, lane='battle'), covered, None, 'battle').decision.upgrade_id == 'health'
    exposed = replace(locked, enemy_damage=100.0)  # 100 / 100 = 1.0
    assert blocks.evaluate_program(route(program, lane='battle'), exposed, None, 'battle').decision.upgrade_id == 'defense_absolute'


def test_a_goal_with_no_known_price_asks_to_observe_it() -> None:
    # Every saved price can be invalidated at once (an unexplained debit).
    # Without a price nothing is eligible, and without a Workshop visit no
    # price is ever read again: the worker stalled on "waiting for price".
    program=[{'id':'goal','type':'save_for','goal':[
        {'id':'goal.pool','type':'pool','upgrade_ids':['thorns'],'selection':'priority'}]}]
    result=blocks.evaluate_program(route(program),replace(facts(),prices={},price_evidence={}),None,'workshop')
    assert (result.decision.stage, result.decision.state) == ('strategy_observe', 'observe_price')
    assert result.trace.observation_ids == ('thorns',)


def test_the_turtle_program_recovers_from_losing_every_price() -> None:
    program=blocks.template_program('turtle','workshop')
    sample=replace(facts(),prices={},price_evidence={},wallet_coins=979,
        purchases={'unlock_defense_upgrades':1,'unlock_thorns':1,'defense_absolute':5,'thorns':4})
    result=blocks.evaluate_program(route(program),sample,None,'workshop')
    assert result.decision.stage == 'strategy_observe'
    # The first goal (Minimum attack) is read first rather than skipped for want of a price.
    assert set(result.trace.observation_ids) == {'damage', 'attack_speed'}
    # An unlock already bought has no tile left to read a price from.
    assert not {'unlock_defense_upgrades','unlock_thorns'} & set(result.trace.observation_ids)


def test_a_banned_goal_is_not_observed() -> None:
    program=[{'id':'goal','type':'save_for','goal':[
        {'id':'goal.pool','type':'pool','upgrade_ids':['thorns'],'selection':'priority'}]}]
    result=blocks.evaluate_program(route(program,bans=('thorns',)),replace(facts(),prices={},price_evidence={}),
        None,'workshop')
    assert result.decision is None or result.decision.stage != 'strategy_observe'


def owns_coin_unlock(**prices: int) -> RouteFacts:
    sample = facts()
    return replace(sample, purchases={**sample.purchases, 'unlock_cash_bonuses': 1, 'unlock_coin_bonuses': 1},
        prices={**sample.prices, **prices},
        price_evidence={**sample.price_evidence, **{uid: {'source': 'observed', 'observed_at': 99} for uid in prices}})


def test_an_owned_unlock_is_never_the_priority_reference() -> None:
    # An owned unlock has no tile left to price. As the reference it asked
    # for an unreadable price forever and sent the worker home every run.
    program = [{'id': 'econ', 'type': 'pool', 'upgrade_ids': ['unlock_coin_bonuses', 'coins_per_kill_bonus'],
                'selection': 'priority'}, pool(discount_pct=20, reference_upgrade_id='priority')]
    sample = replace(owns_coin_unlock(coins_per_kill_bonus=100), wallet_coins=90)
    result = blocks.evaluate_program(route(program), sample, None, 'workshop')
    assert result.decision.state == 'buy' and result.decision.upgrade_id == 'damage'


def test_an_owned_unlock_reference_is_not_observed() -> None:
    program = [pool(discount_pct=20, reference_upgrade_id='unlock_coin_bonuses')]
    result = blocks.evaluate_program(route(program), owns_coin_unlock(), None, 'workshop')
    assert result.decision is None or result.decision.stage != 'strategy_observe'
    assert 'unlock_coin_bonuses' not in result.trace.observation_ids


def test_a_single_level_unlock_catalog_price_is_trusted() -> None:
    # An unlock has one price, so the tracked catalog price is exact; it needs
    # no Workshop read before a cheap pool can compare against it.
    sample = facts()
    estimate = replace(sample, purchases={**sample.purchases, 'unlock_cash_bonuses': 1},
        prices={**sample.prices, 'unlock_coin_bonuses': 100},
        price_evidence={**sample.price_evidence, 'unlock_coin_bonuses': {'source': 'catalog_estimate',
                                                                          'observed_at': None}})
    program = [pool(discount_pct=20, reference_upgrade_id='unlock_coin_bonuses')]
    result = blocks.evaluate_program(route(program), estimate, None, 'workshop')
    assert result.decision.state == 'buy' and result.decision.upgrade_id == 'damage'


def relative_condition(**relative: int) -> dict[str, Any]:
    return {'id': 'econ', 'type': 'condition', 'field': 'wave', 'op': 'lte',
            'relative': {'pct': 50, 'floor': 5, 'cap': 30, **relative},
            'then': [{'id': 'yes', 'type': 'buy', 'upgrade_id': 'health'}],
            'else': [{'id': 'no', 'type': 'buy', 'upgrade_id': 'defense_absolute'}]}


@pytest.mark.parametrize('best,expected', [(None, 5), (1, 5), (8, 5), (24, 12), (40, 20), (90, 30), (500, 30)])
def test_relative_wave_limit_clamps_to_floor_and_cap(best: int | None, expected: int) -> None:
    assert blocks.relative_wave_limit({'pct': 50, 'floor': 5, 'cap': 30}, best) == expected


@pytest.mark.parametrize('best,wave,upgrade,note', [
    (None, 5, 'health', 'econ: wave 5 ≤ 5 → then (best wave unknown → floor 5)'),
    (None, 6, 'defense_absolute', 'econ: wave 6 ≤ 5 → else (best wave unknown → floor 5)'),
    (24, 12, 'health', 'econ: wave 12 ≤ 12 → then (50% of best 24, floor 5, cap 30)'),
    (24, 13, 'defense_absolute', 'econ: wave 13 ≤ 12 → else (50% of best 24, floor 5, cap 30)'),
    (90, 31, 'defense_absolute', 'econ: wave 31 ≤ 30 → else (50% of best 90, floor 5, cap 30)'),
])
def test_relative_wave_condition_picks_branch_and_explains_limit(best: int | None, wave: int, upgrade: str, note: str) -> None:
    program = [relative_condition()]
    blocks.validate_program(program, 'battle')
    sample = replace(battle_facts(defense_absolute={'status': 'available', 'value': 500.0, 'price': 10, 'observed_at': 100}),
                     best_tier_1_wave=best, wave=wave)
    result = blocks.evaluate_program(route(program, lane='battle'), sample, None, 'battle')
    assert result.decision.upgrade_id == upgrade
    assert note in result.trace.rejected


def test_fixed_wave_condition_trace_is_unchanged() -> None:
    program = [{**relative_condition(), 'value': 30}]
    del program[0]['relative']
    sample = replace(battle_facts(), wave=12)
    result = blocks.evaluate_program(route(program, lane='battle'), sample, None, 'battle')
    assert not any('wave 12' in note for note in result.trace.rejected)


@pytest.mark.parametrize('change,message', [
    ({'value': 30}, 'not both'),
    ({'field': 'best_tier_1_wave'}, 'only for the current wave'),
    ({'relative': {'pct': 50, 'floor': 5}}, 'exactly pct, floor and cap'),
    ({'relative': {'pct': 50, 'floor': 5, 'cap': 30, 'x': 1}}, 'exactly pct, floor and cap'),
    ({'relative': {'pct': 0, 'floor': 5, 'cap': 30}}, 'relative pct'),
    ({'relative': {'pct': 50.5, 'floor': 5, 'cap': 30}}, 'relative pct'),
    ({'relative': {'pct': 50, 'floor': 0, 'cap': 30}}, 'relative floor'),
    ({'relative': {'pct': 50, 'floor': 10, 'cap': 9}}, 'relative cap'),
    ({'relative': 'half'}, 'exactly pct, floor and cap'),
])
def test_relative_wave_validation_errors(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        blocks.validate_program([{**relative_condition(), **change}], 'battle')


def turtle_battle_facts(best: int | None, wave: int) -> RouteFacts:
    row = lambda value: {'status': 'available', 'value': value, 'price': 10, 'observed_at': 100}
    sample = battle_facts(defense_absolute=row(500.0), thorns=row(5.0), cash_per_wave=row(5.0),
                          health_regen=row(0.0), damage=row(100.0), attack_speed=row(2.0))
    return replace(sample, best_tier_1_wave=best, wave=wave)


@pytest.mark.parametrize('best,wave,upgrade', [
    (90, 11, 'cash_per_wave'),   # strong account: economy until wave 20 (cap)
    (20, 11, 'thorns'),          # weak account: economy ended at wave 10, Thorns 21 step
    (None, 6, 'thorns'),         # unknown best: economy ended at floor 5
])
def test_turtle_battle_template_scales_with_best_wave(best: int | None, wave: int, upgrade: str) -> None:
    program = list(blocks.template_program('turtle', 'battle'))
    result = blocks.evaluate_program(route(program, lane='battle'), turtle_battle_facts(best, wave), None, 'battle')
    assert result.decision.upgrade_id == upgrade


def test_turtle_battle_template_keeps_def_abs_ahead() -> None:
    program = list(blocks.template_program('turtle', 'battle'))
    thin = replace(turtle_battle_facts(90, 50), upgrade_rows={
        **turtle_battle_facts(90, 50).upgrade_rows,
        'defense_absolute': {'status': 'available', 'value': 150.0, 'price': 10, 'observed_at': 100}})
    assert blocks.evaluate_program(route(program, lane='battle'), thin, None, 'battle').decision.upgrade_id == 'defense_absolute'


def test_battle_stale_priority_row_is_observed_before_later_blocks() -> None:
    # A row not read for over 60s is unverified, not unaffordable: the bot
    # stayed on another tab. Falling through would let Health win forever.
    program = [{'id': 'eco', 'type': 'pool', 'upgrade_ids': ['cash_bonus'], 'selection': 'priority'},
               {'id': 'hp', 'type': 'pool', 'upgrade_ids': ['health'], 'selection': 'priority'}]
    stale = battle_facts(cash_bonus={'status': 'unknown', 'value': None, 'price': None, 'observed_at': 30})
    result = blocks.evaluate_program(route(program, lane='battle'), stale, None, 'battle')
    assert result.decision.state == 'observe_price'
    assert result.trace.observation_ids == ('cash_bonus',)
    fresh = battle_facts(cash_bonus={'status': 'available', 'value': 1.0, 'price': 10, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), fresh, None, 'battle').decision.upgrade_id == 'cash_bonus'
    poor = battle_facts(cash_bonus={'status': 'unaffordable', 'value': 1.0, 'price': 500, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), poor, None, 'battle').decision.upgrade_id == 'health'


def test_battle_stale_buy_block_is_observed() -> None:
    program = [{'id': 'eco', 'type': 'buy', 'upgrade_id': 'cash_bonus'},
               {'id': 'hp', 'type': 'buy', 'upgrade_id': 'health'}]
    result = blocks.evaluate_program(route(program, lane='battle'), battle_facts(), None, 'battle')
    assert result.trace.observation_ids == ('cash_bonus',)


def test_battle_priority_pool_observes_only_stale_rows_ranked_above_its_pick() -> None:
    stale = battle_facts(cash_bonus={'status': 'unknown', 'value': None, 'price': None, 'observed_at': 30})
    above = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['cash_bonus', 'health'], 'selection': 'priority'}]
    assert blocks.evaluate_program(route(above, lane='battle'), stale, None, 'battle').trace.observation_ids == ('cash_bonus',)
    below = [{'id': 'p', 'type': 'pool', 'upgrade_ids': ['health', 'cash_bonus'], 'selection': 'priority'}]
    assert blocks.evaluate_program(route(below, lane='battle'), stale, None, 'battle').decision.upgrade_id == 'health'


def test_an_unpriced_goal_is_observed_before_a_later_block_can_spend() -> None:
    # A goal whose price was never read was skipped, so the uncapped pool
    # below it spent every coin and the goal was never saved for.
    program = [{'id': 'goal', 'type': 'save_for', 'goal': [
                   {'id': 'goal.buy', 'type': 'buy', 'upgrade_id': 'unlock_cash_bonuses'}]},
               {'id': 'rest', 'type': 'pool', 'upgrade_ids': ['damage'], 'selection': 'priority'}]
    result = blocks.evaluate_program(route(program), facts(), None, 'workshop')
    assert result.decision.state == 'observe_price'
    assert result.trace.observation_ids == ('unlock_cash_bonuses',)


def test_a_battle_row_locked_this_run_is_not_observed_again() -> None:
    # Workshop unlocks cannot change mid-run: a locked row stays settled
    # after 60s instead of sending the bot back to an empty tab.
    program = [{'id': 'eco', 'type': 'pool', 'upgrade_ids': ['cash_bonus'], 'selection': 'priority'},
               {'id': 'hp', 'type': 'pool', 'upgrade_ids': ['health'], 'selection': 'priority'}]
    locked = battle_facts(cash_bonus={'status': 'locked', 'value': None, 'price': None, 'observed_at': 0})
    assert blocks.evaluate_program(route(program, lane='battle'), locked, None, 'battle').decision.upgrade_id == 'health'


def test_battle_cheapest_pool_uses_lowest_affordable_fresh_price() -> None:
    program = [{'id': 'combat', 'type': 'pool', 'selection': 'cheapest',
                'upgrade_ids': ['attack_speed', 'health', 'thorns']}]
    blocks.validate_program(program, 'battle')
    sample = battle_facts(
        attack_speed={'status': 'available', 'value': 1, 'price': 25, 'observed_at': 100},
        health={'status': 'available', 'value': 1, 'price': 8, 'observed_at': 100},
        thorns={'status': 'unaffordable', 'value': 1, 'price': 150, 'observed_at': 100})
    assert blocks.evaluate_program(route(program, lane='battle'), sample, None, 'battle').decision.upgrade_id == 'health'
    stale = replace(sample, upgrade_rows={**sample.upgrade_rows,
        'attack_speed': {'status': 'unknown', 'value': None, 'price': None, 'observed_at': 30}})
    assert blocks.evaluate_program(route(program, lane='battle'), stale, None, 'battle').decision.upgrade_id == 'health'
    with pytest.raises(ValueError, match='Battle price evidence'):
        blocks.validate_program(program, 'workshop')


def test_battle_economy_pool_holds_until_available_skills_have_twenty_buys() -> None:
    program = [
        {'id': 'economy', 'type': 'pool', 'selection': 'cheapest',
         'upgrade_ids': ['cash_bonus', 'cash_per_wave', 'coins_per_kill_bonus', 'coins_per_wave'],
         'level_caps': {uid: {'base': 20} for uid in
                        ('cash_bonus', 'cash_per_wave', 'coins_per_kill_bonus', 'coins_per_wave')},
         'hold_until_capped': True},
        {'id': 'combat', 'type': 'pool', 'selection': 'cheapest', 'upgrade_ids': ['health']}]
    blocks.validate_program(program, 'battle')
    sample = battle_facts(
        cash_bonus={'status': 'available', 'value': 1, 'price': 12, 'observed_at': 100},
        cash_per_wave={'status': 'available', 'value': 1, 'price': 10, 'observed_at': 100},
        coins_per_kill_bonus={'status': 'locked', 'value': None, 'price': None, 'observed_at': 100},
        coins_per_wave={'status': 'maxed', 'value': 1, 'price': None, 'observed_at': 100})
    chosen = blocks.evaluate_program(route(program, lane='battle'), sample, None, 'battle')
    assert chosen.decision.upgrade_id == 'cash_per_wave'
    waiting = replace(sample, upgrade_rows={**sample.upgrade_rows,
        'cash_bonus': {'status': 'unaffordable', 'value': 1, 'price': 120, 'observed_at': 100},
        'cash_per_wave': {'status': 'unaffordable', 'value': 1, 'price': 130, 'observed_at': 100}})
    assert blocks.evaluate_program(route(program, lane='battle'), waiting, None, 'battle').status == 'blocked'
    complete = replace(sample, run_purchases={'cash_bonus': 20, 'cash_per_wave': 20})
    assert blocks.evaluate_program(route(program, lane='battle'), complete, None, 'battle').decision.upgrade_id == 'health'
    capped_stale = replace(complete, upgrade_rows={**complete.upgrade_rows,
        'cash_bonus': {'status': 'unknown', 'value': None, 'price': None, 'observed_at': 30}})
    assert blocks.evaluate_program(route(program, lane='battle'), capped_stale, None, 'battle').decision.upgrade_id == 'health'


def test_battle_economy_with_unreadable_prices_uses_affordable_combat_fallback() -> None:
    program = [
        {'id': 'economy', 'type': 'pool', 'selection': 'cheapest',
         'upgrade_ids': ['cash_bonus'], 'level_caps': {'cash_bonus': {'base': 20}},
         'hold_until_capped': True},
        {'id': 'combat', 'type': 'pool', 'selection': 'cheapest', 'upgrade_ids': ['health']}]
    sample = battle_facts(cash_bonus={'status': 'unreadable', 'value': 1.2,
                                      'price': None, 'observed_at': 100})
    result = blocks.evaluate_program(route(program, lane='battle'), sample, None, 'battle')
    assert result.decision.upgrade_id == 'health'


def held(program: list[dict[str, Any]], min_wave: int = 60) -> SimpleNamespace:
    result = route(program)
    result.rules = RouteRules(coins=CoinRules(kill_bonus_min_best_wave=min_wave))
    return result


def test_swap_puts_coins_per_wave_in_coins_per_kill_place() -> None:
    alone = {'id':'eco','type':'pool','selection':'weighted',
             'upgrade_ids':['cash_bonus','coins_per_kill_bonus','damage'],
             'weights':{'coins_per_kill_bonus':150}, 'level_caps':{'coins_per_kill_bonus':{'base':3}},
             'targets':{'coins_per_kill_bonus':1.25}}
    both = {'id':'both','type':'pool','selection':'priority',
            'upgrade_ids':['coins_per_kill_bonus','cash_bonus','coins_per_wave'],
            'targets':{'coins_per_kill_bonus':1.5}}
    read = {'id':'read','type':'condition','field':'upgrade_value','upgrade_id':'coins_per_kill_bonus',
            'op':'gte','value':1.1,'then':[{'id':'buy','type':'buy','upgrade_id':'coins_per_kill_bonus'}],'else':[]}
    swapped_alone, swapped_both, swapped_read = blocks.swap_kill_bonus((alone, both, read))
    assert swapped_alone['upgrade_ids'] == ['cash_bonus','coins_per_wave','damage']
    assert swapped_alone['weights'] == {'coins_per_wave':150}
    assert swapped_alone['level_caps'] == {'coins_per_wave':{'base':3}}
    assert swapped_alone['targets'] == {'coins_per_wave':blocks.PER_WAVE_STAND_IN_TARGET}
    # Already listed: one entry, at the higher rank, with its own settings.
    assert swapped_both['upgrade_ids'] == ['coins_per_wave','cash_bonus']
    assert swapped_both['targets'] == {}
    assert swapped_read['upgrade_id'] == 'coins_per_kill_bonus'
    assert swapped_read['then'][0]['upgrade_id'] == 'coins_per_wave'
    assert alone['upgrade_ids'][1] == 'coins_per_kill_bonus'  # the route itself is untouched


def test_coins_per_kill_waits_for_the_best_wave_rule_in_the_workshop() -> None:
    program = [{'id':'eco','type':'pool','selection':'priority',
                'upgrade_ids':['coins_per_kill_bonus','damage']}]
    sample = replace(facts(), prices={'coins_per_kill_bonus':50,'coins_per_wave':60,'damage':80},
                     purchases={'unlock_coin_bonuses':1})
    def pick(best: int | None, min_wave: int = 60) -> str | None:
        result = blocks.evaluate_program(held(program, min_wave), replace(sample, best_tier_1_wave=best),
                                         None, 'workshop')
        return result.decision.upgrade_id
    assert pick(25) == 'coins_per_wave'
    assert pick(None) == 'coins_per_wave'
    assert pick(59) == 'coins_per_wave'
    assert pick(60) == 'coins_per_kill_bonus'
    assert pick(25, min_wave=0) == 'coins_per_kill_bonus'
    assert blocks.evaluate_program(route(program), sample, None, 'workshop').decision.upgrade_id == 'coins_per_kill_bonus'


def test_battle_cash_goes_to_coins_per_wave_below_the_best_wave_rule() -> None:
    rows = {uid: {'status':'available','value':value,'price':price,'observed_at':100}
            for uid, value, price in (('coins_per_kill_bonus',1.0,10),('coins_per_wave',0.0,12),('cash_bonus',1.0,15))}
    sample = RouteFacts('account','Air_38','battle',100,101, best_tier_1_wave=33, run_id=8, wave=7,
                        battle_cash=100, run_purchases={}, upgrade_rows=rows)
    program = [{'id':'econ','type':'pool','selection':'priority',
                'upgrade_ids':['coins_per_kill_bonus','cash_bonus'],'targets':{'coins_per_kill_bonus':1.25}}]
    swapped = blocks.evaluate_program(held(program), sample, None, 'battle')
    assert swapped.decision.upgrade_id == 'coins_per_wave'
    assert any('until best Tier 1 wave 60' in line for line in swapped.trace.rejected)
    native = blocks.native_template_program('turtle', 'battle')
    assert blocks.evaluate_program(held(list(native)), sample, None, 'battle').decision.upgrade_id != 'coins_per_kill_bonus'
    later = replace(sample, best_tier_1_wave=60)
    assert blocks.evaluate_program(held(program), later, None, 'battle').decision.upgrade_id == 'coins_per_kill_bonus'


def test_observed_rows_include_the_coins_per_wave_stand_in() -> None:
    program = blocks.validate_program([{'id':'econ','type':'pool','selection':'priority',
                                        'upgrade_ids':['coins_per_kill_bonus']}], 'battle')
    assert blocks.program_upgrade_ids(program) == ('coins_per_kill_bonus','coins_per_wave')


def test_a_wait_block_reads_prices_it_passed_over_before_holding() -> None:
    program = [pool(), {'id':'hold','type':'wait'}]
    unread = replace(facts(), prices={'thorns':100},
                     price_evidence={'thorns':{'source':'observed','observed_at':99}})
    result = blocks.evaluate_program(route(program), unread, None, 'workshop')
    assert result.decision.state == 'observe_price' and result.decision.upgrade_id == 'damage'
    poor = replace(facts(), wallet_coins=10)
    held = blocks.evaluate_program(route(program), poor, None, 'workshop')
    assert held.decision is None and held.trace.reason == 'Wait block reached'


def test_modeled_pool_uses_cash_curve_without_observed_price_ttl() -> None:
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model')
    program = blocks.validate_program([block], 'battle')
    quote = dict(account_id='account', run_id=7, status='available', source='model', verified=True, value=1)
    f = replace(battle_facts(), battle_price_quotes={
        'health': dict(quote, price=10), 'attack_speed': dict(quote, price=5)})
    result = blocks.evaluate_program(route(list(program), lane='battle'), f, None, 'battle')
    assert result.decision.upgrade_id == 'attack_speed'
    assert result.decision.price == 5
    assert result.decision.price_source == 'model'
    assert result.trace.price_source == 'model'


def test_model_pool_reconciles_cheapest_stale_wave_before_spending(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the wave reconcile trip
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model')
    quote = dict(account_id='account', run_id=7, status='available', source='model', value=1)
    f = replace(battle_facts(), battle_price_quotes={
        'health': dict(quote, price=10, verified=True),
        'attack_speed': dict(quote, price=5, verified=False)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.trace.observation_ids == ('attack_speed',)
    assert result.status == 'projected'


def test_model_quotes_do_not_change_observed_pools_or_cross_run() -> None:
    block = pool(upgrade_ids=['health'], selection='cheapest')
    f = replace(battle_facts(), battle_price_quotes={
        'health': dict(account_id='other', run_id=8, status='available', source='model', price=1, verified=True)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.decision.price == 10
    block['price_source'] = 'model'
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.decision.price != 1


def test_model_pool_rejects_wrong_lane_selection_and_missing_curve() -> None:
    for lane, extra in [('workshop', {}), ('battle', {'selection': 'priority'}),
                         ('battle', {'upgrade_ids': ['damage']})]:
        with pytest.raises(ValueError):
            blocks.validate_program([{**pool(upgrade_ids=['health'], selection='cheapest', price_source='model'), **extra}], lane)


def test_assigned_model_pool_advances_only_after_persisted_receipt(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the receipt fence
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
    root = _registered(tmp_path, 'Air_38', 'account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['battle'].update(mode='blocks', blocks=[pool(
        upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model',
        batch_size=5, max_price_premium_pct=25)])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, 'operator')
    progress = RerollProgress(root, 'account', AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, 'Air_38', 'account')
    rows = {uid: dict(status='available', value=1, price=price, observed_at=time.time())
            for uid, price in [('health', 10), ('attack_speed', 5)]}
    base = AutopilotPolicy(enabled=True)
    first = progress.battle_policy(base, rows, run_id=2, wave=2, cash=100)
    assert first.modeled_pool and first.battle_price_quote['price'] == 5
    rows['attack_speed'].update(price=7, value=1.05, observed_at=time.time())
    pending = progress.battle_policy(base, rows, run_id=2, wave=2, cash=95, pending_purchase=True)
    assert pending.battle_price_quote['price'] == 5
    awaiting = progress.battle_policy(base, rows, run_id=2, wave=2, cash=95,
                                     after_receipt_sequence=0)
    assert not awaiting.enabled
    assert progress.battle_prices.rows['attack_speed']['index'] == 0
    with db.connect(root / 'tower_bot.db') as conn:
        conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(1,2,1,'BattlePurchased',?)",
                     (json.dumps({'upgrade_id': 'attack_speed'}),))
    rows['attack_speed'].update(price=None, status='unreadable', observed_at=time.time())
    second = progress.battle_policy(base, rows, run_id=2, wave=2, cash=95)
    assert second.battle_price_quote['price'] == 7
    assert second.battle_price_quote['value'] == 1.05
    assert second.decision_token != first.decision_token
    # Preference belongs to the current route and pool, never a previous
    # revision's batch on an otherwise unchanged panel.
    rows['attack_speed'].update(price=10, status='available', value=1.1, observed_at=time.time())
    rows['health'].update(price=12, observed_at=time.time())
    batch = dict(run_id=2, wave=2, cash=95, visible_upgrade_ids=('health',),
                 battle_batch_purchases=1, batch_route_token='account:1:2', batch_rule_id='cheap')
    assert progress.battle_policy(base, rows, **batch).rules[0].upgrade_id == 'health'
    for stale_scope in (dict(batch_route_token='account:0:2'), dict(batch_rule_id='old_pool')):
        assert progress.battle_policy(base, rows, **{**batch, **stale_scope}).rules[0].upgrade_id == 'attack_speed'


@pytest.mark.parametrize('count,local_price,chosen', [
    (0, 12, 'attack_speed'), (1, 12, 'health'), (4, 12, 'health'),
    (5, 12, 'attack_speed'), (1, 13, 'attack_speed'),
])
def test_modeled_visible_batch_is_bounded_and_keeps_price_tolerance(
    count: int, local_price: int, chosen: str,
) -> None:
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest',
                 price_source='model', batch_size=5, max_price_premium_pct=25)
    blocks.validate_program([block], 'battle')
    quote = dict(account_id='account', run_id=7, status='available', source='model', value=1, verified=True)
    f = replace(battle_facts(), visible_upgrade_ids=('health',), battle_batch_purchases=count, battle_batch_rule_id='cheap',
        battle_price_quotes={'health': dict(quote, price=local_price), 'attack_speed': dict(quote, price=10)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.decision.upgrade_id == chosen


@pytest.mark.parametrize('extra', [dict(batch_size=0), dict(batch_size=6), dict(batch_size=True),
    dict(max_price_premium_pct=26), dict(max_price_premium_pct=-1), dict(price_source='observed')])
def test_batch_options_reject_unsafe_or_incompatible_values(extra: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        blocks.validate_program([pool(upgrade_ids=['health'], selection='cheapest',
            **{**dict(price_source='model', batch_size=5, max_price_premium_pct=25), **extra})], 'battle')


def test_visible_batch_cannot_skip_a_stale_cheaper_quote(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', False)  # pins the wave reconcile trip
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest',
                 price_source='model', batch_size=5, max_price_premium_pct=25)
    quote = dict(account_id='account', run_id=7, status='available', source='model', value=1)
    f = replace(battle_facts(), visible_upgrade_ids=('health',), battle_batch_purchases=2, battle_batch_rule_id='cheap',
        battle_price_quotes={'health': dict(quote, price=12, verified=True),
                             'attack_speed': dict(quote, price=10, verified=False)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.trace.observation_ids == ('attack_speed',)
    assert result.status == 'projected'


def test_burst_mode_buys_the_cheapest_quote_without_a_wave_reconcile_trip(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    block = pool(upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model')
    quote = dict(account_id='account', run_id=7, status='available', source='model', value=1)
    f = replace(battle_facts(), battle_price_quotes={
        'health': dict(quote, price=10, verified=True),
        'attack_speed': dict(quote, price=5, verified=False)})
    result = blocks.evaluate_program(route([block], lane='battle'), f, None, 'battle')
    assert result.trace.observation_ids == ()
    assert result.status == 'observed' and result.decision.upgrade_id == 'attack_speed'


def _assigned_model_progress(tmp_path: Path) -> tuple[Any, Path, dict[str, dict[str, Any]], Any]:
    """A registered worker whose assigned battle route is one modeled cheapest pool."""
    import time
    from account_state import AccountState
    from fleet.build_route import RouteDocument
    from fleet.build_route_store import BuildRouteStore
    from fleet.build_route_runtime import BuildRouteRuntime
    from fleet.reroll_progress import RerollProgress
    from policy import AutopilotPolicy
    from tests.test_build_route_integration import _registered
    root = _registered(tmp_path, 'Air_38', 'account')
    raw = RouteDocument.compatibility().to_dict()
    raw['baseline']['battle'].update(mode='blocks', blocks=[pool(
        upgrade_ids=['health', 'attack_speed'], selection='cheapest', price_source='model',
        batch_size=5, max_price_premium_pct=25)])
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, 'operator')
    progress = RerollProgress(root, 'account', AccountState())
    progress.route_runtime = BuildRouteRuntime(tmp_path, 'Air_38', 'account')
    rows = {uid: dict(status='available', value=1, price=price, observed_at=time.time())
            for uid, price in [('health', 10), ('attack_speed', 5)]}
    return progress, root, rows, AutopilotPolicy(enabled=True)


def _purchases(root: Path, run_id: int, *seqs: int) -> None:
    """What the async event sink commits: one BattlePurchased row per level."""
    import json
    import db
    with db.connect(root / 'tower_bot.db') as conn:
        for seq in seqs:
            conn.execute("INSERT INTO events(seq,run_id,ts,type,detail) VALUES(?,?,1,'BattlePurchased',?)",
                         (seq, run_id, json.dumps({'upgrade_id': 'attack_speed'})))


def test_the_in_memory_tally_replaces_the_receipt_fence(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'BATTLE_BURST_ENABLED', True)
    progress, root, rows, base = _assigned_model_progress(tmp_path)
    _purchases(root, 2, 1)  # bought before this process started

    def policy(run_id: int = 2, **extra: Any) -> Any:
        return progress.battle_policy(base, rows, run_id=run_id, wave=2, cash=100, **extra)

    assert policy().decision_token.endswith(':1')  # the tally starts from the database
    rows['attack_speed'].update(price=None, status='unreadable')
    progress.note_battle_levels(2, 'attack_speed', 3)
    confirmed = policy(after_receipt_sequence=3)
    assert confirmed.enabled  # no fence: the receipt never disables buying
    assert confirmed.decision_token.endswith(':4')
    _purchases(root, 2, 2, 3)  # the sink catches up partway
    assert policy().decision_token.endswith(':4')  # max(db, tally), never the sum
    _purchases(root, 2, 4, 5)
    assert policy().decision_token.endswith(':5')  # the database may lead too
    assert policy(run_id=3).decision_token.endswith(':0')  # a new run starts over
    progress.note_battle_levels(2, 'attack_speed', 9)  # a receipt for the old run
    assert policy(run_id=3).decision_token.endswith(':0')
