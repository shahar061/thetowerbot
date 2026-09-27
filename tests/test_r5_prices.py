from pathlib import Path

from fleet.workshop_prices import WorkshopPrices, catalog_price
import workshop_levels


def test_full_ladder_adapter_and_typed_coin_domain(tmp_path: Path) -> None:
    level = 7
    price = workshop_levels.ladders()['damage'].next_coins[level]
    assert catalog_price('damage', level) == price
    memory = WorkshopPrices(tmp_path, 'a')
    memory.observe('damage', 383, 0, now=100, discount_signature='none')
    quote = memory.quotes({'damage': 1}, discount_signature='none')['damage']
    assert quote.price == 472
    assert quote.lower == quote.upper == 472
    assert quote.catalog_revision and quote.modifier_signature == 'none'
    assert quote.level_confidence == 'exact'
    assert quote.for_execution(context='workshop', currency='coins') == 472
    assert quote.for_execution(context='battle', currency='cash') is None
    assert memory.quotes({}, discount_signature='none')['damage'].price == 383
    assert memory.quotes({'damage': 1}, discount_signature='none')['damage'] == quote


def test_unknown_modifiers_do_not_authorize_catalog_fast_path(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path, 'a')
    memory.observe('damage', 383, 0, now=100)
    quote = memory.quotes({'damage': 1})['damage']
    assert quote.for_execution(context='workshop', currency='coins') is None


def test_planner_exposes_cards_calibration_gap_without_disabling_planning() -> None:
    from fleet.resource_blocks import _gem_plan, LabFacts
    from fleet.build_route import RouteRules
    plan = _gem_plan([{"id":"cards", "type":"buy_cards", "purpose":"card_missions"}],
                     LabFacts(now=1.,wallet_gems=1000),RouteRules())
    assert plan.next is not None and plan.price == 20
    assert not plan.automated
    assert any('independent card' in reason.lower() for reason in plan.why)


def test_battle_executor_rejects_workshop_coin_quote() -> None:
    from dataclasses import replace
    from fleet.workshop_prices import PriceQuote
    from tests.test_autopilot import parts
    bot, device, screen, observation, policy = parts()
    quote = PriceQuote(30,0,'catalog_estimate',level_confidence='exact',modifier_signature='none')
    observation = replace(observation,rows=tuple(replace(row,price=30,price_quote=quote)
        if row.upgrade_id=='damage' else row for row in observation.rows))
    bot.step(screen,device,policy,cash=100,observation=observation)
    assert device.actions == []
    assert 'price domain' in bot.state.snapshot()['reason'].lower()


def test_current_zero_discount_facts_enable_exact_quotes_but_history_does_not(tmp_path, monkeypatch):
    from dataclasses import asdict
    from account_state import AccountState
    from evidence_scope import FactScope
    from fleet.identity import IdentityEvidence
    from fleet.reroll_progress import RerollProgress
    import time
    state = AccountState()
    scope = FactScope('acct','lease','generation',0)
    state.bind_scope(scope,identity=IdentityEvidence('acct',time.time(),'id'))
    facts = [dict(concept_id=f'labs.workshop-{c}-discount',status='verified',value=0,
                  scope=asdict(scope),evidence={'observed_at':time.time()})
             for c in ('attack','defense','utility')]
    monkeypatch.setattr(state,'snapshot',lambda:{'revision':{'lab_levels':facts}})
    planner = RerollProgress(tmp_path,'acct',state)
    assert planner._discount_signature() == 'none'
    facts[0]['scope'] = None
    assert planner._discount_signature() != 'none'


def test_rounded_price_anchor_does_not_infer_an_exact_next_level(tmp_path):
    memory = WorkshopPrices(tmp_path,'acct')
    price = workshop_levels.ladders()['damage'].next_coins[30]
    assert catalog_price('damage',30) is None
    memory.observe('damage',price,0,now=100,discount_signature='none')
    observed = memory.quotes({},discount_signature='none')['damage']
    assert observed.lower < observed.upper
    assert observed.for_execution(context='workshop',currency='coins') is None
    assert 'damage' not in memory.quotes({'damage':1},discount_signature='none')


def test_full_planning_ladder_does_not_expand_historical_spend_authority() -> None:
    from fleet.workshop_prices import lists_price
    assert not lists_price('thorns',2470)


def test_duplicate_delayed_verified_receipt_advances_catalog_only_once(tmp_path) -> None:
    import db
    import events
    from ledger import LedgerWriter
    from tests.test_reroll_price_planning import worker
    planner = worker(tmp_path)
    planner.observe_prices({'damage':30},100)
    at = planner.price_memory.entries['damage']['observed_at'] + 1
    receipt = events.Purchased(item='Damage',category='ATTACK',price=30,coins_before=100,
        dry_run=False,verdict='bought',spent=30,transaction_key='same-receipt',ts=at)
    with db.connect(tmp_path/'tower_bot.db') as conn:
        writer = LedgerWriter(conn)
        for _ in range(2):
            for line in writer.lines_for(receipt):
                db.insert_ledger(conn,line.as_row())
    wallet, quotes = planner._pricing({'damage':1})
    assert wallet == 70
    assert quotes['damage'].price == 55
    assert planner._pricing({'damage':1}) == (wallet,quotes)


def test_exact_anchor_does_not_authorize_rounded_next_catalog_rung(tmp_path: Path) -> None:
    memory = WorkshopPrices(tmp_path,'acct')
    memory.observe('thorns',961,0,now=100,discount_signature='none')
    assert memory.quotes({},discount_signature='none')['thorns'].for_execution(
        context='workshop',currency='coins') == 961
    assert catalog_price('thorns',13) is None
    assert 'thorns' not in memory.quotes({'thorns':1},discount_signature='none')


def test_persisted_price_revision_covers_precision_interpretation(tmp_path: Path) -> None:
    import json
    from fleet.workshop_prices import CATALOG
    memory = WorkshopPrices(tmp_path,'acct')
    memory.observe('thorns',961,0,now=100,discount_signature='none')
    memory.save()
    payload = json.loads(memory.path.read_text())
    assert 'precision' in payload['catalog_revision']
    payload['catalog_revision'] = CATALOG['source_revision']
    memory.path.write_text(json.dumps(payload))
    assert WorkshopPrices(tmp_path,'acct').quotes({},discount_signature='none') == {}
