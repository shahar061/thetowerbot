"""Card decisions are advisory and fail closed on missing execution evidence."""
from typing import Any

import pytest

from card_models import (AcquireGoal, CardBudget, CardCapabilities, CardCommand,
    CardFieldEvidence, CardItem, CardLoadout, CardPlanContext, CardProgram, CardSnapshot,
    CardTarget, SlotGoal)
from evidence_scope import BalanceInterval, FactScope
from strategy import CardPolicy

SCOPE = FactScope('a', 'lease', 'generation', 1)


def item(card_id: str = 'cards.damage', ownership: str = 'unowned',
         level: int | None = None, **changes: Any) -> CardItem:
    evidence = CardFieldEvidence(scope=SCOPE, visit_id='v', observed_at=100.,
        evidence_ref='tile', confidence=.99)
    return CardItem(card_id=card_id, ownership=ownership, level=level,
        observed_at=100., evidence_ref='tile', field_evidence={
            'ownership': evidence, **({'level': evidence} if level is not None else {})}, **changes)


def snapshot(*items: CardItem, **changes: Any) -> CardSnapshot:
    values: dict[str, Any] = dict(scope=SCOPE, revision=1, observed_at=100., visit_id='v',
        items=items, capacity=1, equipped=(), collection_complete=False,
        equipment_complete=True, confidence=.99, equipment_evidence=CardFieldEvidence(
            scope=SCOPE, visit_id='v', observed_at=100., evidence_ref='equipment', confidence=.99))
    values.update(changes)
    return CardSnapshot(**values)


def goal(card_id: str = 'cards.damage', min_level: int | None = None, id: str = 'g') -> AcquireGoal:
    return AcquireGoal(id=id, kind='acquire', targets=(CardTarget(card_id=card_id, min_level=min_level),))


def quote(quantity: int = 1, kind: str = 'buy', **changes: Any) -> Any:
    from card_models import CardQuote
    values = dict(kind=kind, quantity=quantity, price=20 * quantity,
        scope=SCOPE, visit_id='v', observed_at=100., evidence_ref='price', layout_id='fixture')
    values.update(changes)
    return CardQuote(**values)


def context(**changes: Any) -> CardPlanContext:
    values: dict[str, Any] = dict(scope=SCOPE, visit_id='v', now=101., program_revision='r1',
        program=CardProgram(version=1, gem_cap=500, goals=(goal(),)),
        policy=CardPolicy(enabled=True, gem_floor=40, max_per_visit=10),
        snapshot=snapshot(item()), budget=CardBudget(cycle_id='cycle', cap=500, spent=0, pending=0),
        balance=BalanceInterval('gems', 500, 500, SCOPE, 100., 'wallet'), committed_gems=0,
        eligible_goal_ids=None, capabilities=CardCapabilities(inventory=True, buy_one=True,
            buy_ten=True, buy_slot=True, assign=True, layout_id='fixture'),
        armed=True, paused=False, in_run=False, unresolved_operation=None,
        cards_bought_this_visit=0, quotes=(quote(),))
    values.update(changes)
    return CardPlanContext(**values)


def command(kind: str = 'buy', **changes: Any) -> CardCommand:
    values = dict(idempotency_key='cmd', scope=SCOPE, program_revision='r1', kind=kind,
        quantity=1, source='manual', budget_cycle_id='cycle')
    values.update(changes)
    return CardCommand(**values)


def test_unknown_target_is_not_completed() -> None:
    from card_plan import goal_met
    assert goal_met(goal(), snapshot()) is None
    assert goal_met(goal(min_level=3), snapshot(item(ownership='owned'))) is None


def test_route_order_overrides_program_order() -> None:
    from card_plan import plan_cards
    program = CardProgram(version=1, gem_cap=500,
        goals=(goal(id='first'), goal('cards.health', id='second')))
    decision = plan_cards(context(program=program, eligible_goal_ids=('second',),
        snapshot=snapshot(item(), item('cards.health'))))
    assert (decision.kind, decision.goal_id) == ('buy', 'second')


@pytest.mark.parametrize('confidence', [None, .1])
def test_slot_goal_requires_confident_capacity(confidence: float | None) -> None:
    from card_plan import goal_met
    assert goal_met(SlotGoal(id='slots', kind='slots', capacity=2),
                    snapshot(capacity=2, confidence=confidence)) is None


def test_group_requires_all_targets_and_current_per_field_evidence() -> None:
    from card_plan import goal_met
    group = AcquireGoal(id='g', kind='acquire', targets=(CardTarget(card_id='cards.damage'),
        CardTarget(card_id='cards.health', min_level=3)))
    assert goal_met(group, snapshot(item(ownership='owned'), item('cards.health', 'owned', 2))) is False
    assert goal_met(group, snapshot(item(ownership='owned'), item('cards.health', 'owned', 3))) is True
    stale = item(ownership='owned', level=3).model_copy(update={'field_evidence': {}})
    assert goal_met(goal(min_level=3), snapshot(stale)) is None


def test_loadout_fallback_partial_ownership_and_unknown_before_cutoff() -> None:
    from card_plan import resolve_loadout
    loadout = CardLoadout(id='l', name='Farm', priority=('cards.damage', 'cards.health', 'cards.cash'))
    known = snapshot(item(ownership='unavailable'), item('cards.health', 'owned'))
    assert resolve_loadout(loadout, known).desired == ('cards.health',)
    assert resolve_loadout(loadout, known).missing == ('cards.damage',)
    assert resolve_loadout(loadout, snapshot(item('cards.health', 'owned'))).desired is None
    assert resolve_loadout(loadout, snapshot(item(ownership='owned'))).desired == ('cards.damage',)


def test_empty_or_unusable_loadout_never_implies_clear() -> None:
    from card_plan import resolve_loadout
    assert resolve_loadout(CardLoadout(id='l', name='Empty'), snapshot()).desired is None
    assert resolve_loadout(CardLoadout(id='l', name='Locked', priority=('cards.damage',)),
        snapshot(item(ownership='unavailable'))).reason == 'no_usable_cards'


def test_ordered_groups_skip_only_completed_goals_and_always_buy_one() -> None:
    from card_plan import plan_cards
    program = CardProgram(version=1, gem_cap=500,
        goals=(goal(), goal('cards.health', id='next')))
    decision = plan_cards(context(program=program, policy=CardPolicy(enabled=True, batch='x10', max_per_visit=10),
        snapshot=snapshot(item(ownership='owned'), item('cards.health'))))
    assert (decision.kind, decision.goal_id, decision.quantity) == ('buy', 'next', 1)


@pytest.mark.parametrize(('changes', 'kind', 'reason'), [
    ({'armed': False}, 'wait', 'not_armed'),
    ({'paused': True}, 'wait', 'paused'),
    ({'in_run': True}, 'wait', 'waiting_for_run_end'),
    ({'snapshot': None}, 'refresh', 'inventory_unknown'),
    ({'eligible_goal_ids': ()}, 'wait', 'route_waiting'),
    ({'quotes': ()}, 'refresh', 'quote_unknown'),
    ({'committed_gems': 441}, 'wait', 'reserve_protected'),
    ({'budget': CardBudget(cycle_id='cycle', cap=20, spent=1, pending=0)}, 'wait', 'budget_reached'),
    ({'cards_bought_this_visit': 10}, 'wait', 'visit_limit'),
])
def test_execution_gates(changes: dict[str, Any], kind: str, reason: str) -> None:
    from card_plan import plan_cards
    decision = plan_cards(context(**changes))
    assert (decision.kind, decision.reason) == (kind, reason)


def test_unavailable_target_and_unknown_level_prevent_buy() -> None:
    from card_plan import plan_cards
    assert plan_cards(context(snapshot=snapshot(item(ownership='unavailable')))).reason == 'target_unavailable'
    ctx = context(program=CardProgram(version=1, gem_cap=500, goals=(goal(min_level=3),)),
        snapshot=snapshot(item(ownership='owned')))
    assert plan_cards(ctx).kind == 'refresh'


def test_budget_edits_cannot_raise_cycle_or_erase_consumption() -> None:
    from card_plan import plan_cards
    budget = CardBudget(cycle_id='cycle', cap=100, spent=70, pending=20)
    assert plan_cards(context(budget=budget)).reason == 'budget_reached'
    assert plan_cards(context(budget=budget, program=CardProgram(version=1, gem_cap=60,
        goals=(goal(),)))).reason == 'budget_reached'


def test_manual_bypasses_only_auto_enabled_and_preserves_quantity() -> None:
    from card_plan import plan_cards
    ctx = context(command=command(quantity=10), quotes=(quote(10),), policy=CardPolicy(max_per_visit=10))
    assert (plan_cards(ctx).kind, plan_cards(ctx).quantity) == ('buy', 10)
    assert plan_cards(ctx.model_copy(update={'cards_bought_this_visit': 1})).reason == 'visit_limit'
    assert plan_cards(ctx.model_copy(update={'armed': False})).reason == 'not_armed'
    assert plan_cards(ctx.model_copy(update={'eligible_goal_ids': ()})).reason == 'route_waiting'
    assert plan_cards(ctx.model_copy(update={'command': None})).reason == 'automation_disabled'
    assert plan_cards(ctx.model_copy(update={'capabilities': ctx.capabilities.model_copy(update={'buy_ten': False})})).reason == 'unsupported_screen'


@pytest.mark.parametrize('field', ['scope', 'program_revision', 'budget_cycle_id'])
def test_manual_stale_command_identity_never_spends(field: str) -> None:
    from card_plan import plan_cards
    change = FactScope('b', 'lease', 'g', 1) if field == 'scope' else 'old'
    assert plan_cards(context(command=command(**{field: change}))).kind == 'wait'


@pytest.mark.parametrize('age,expected', [(30., 'buy'), (30.01, 'refresh'), (-.01, 'refresh')])
def test_quote_and_wallet_age_boundaries(age: float, expected: str) -> None:
    from card_plan import plan_cards
    ctx = context(quotes=(quote(observed_at=101. - age),))
    assert plan_cards(ctx).kind == expected
    ctx = context(balance=BalanceInterval('gems', 500, 500, SCOPE, 101. - age, 'wallet'))
    assert plan_cards(ctx).kind == expected


def test_historical_field_and_wrong_visit_quote_cannot_gain_current_authority() -> None:
    from card_plan import plan_cards
    stale = item().model_copy(update={'field_evidence': {'ownership': CardFieldEvidence(
        scope=SCOPE, visit_id='old', observed_at=100., evidence_ref='old', confidence=.99)}})
    assert plan_cards(context(snapshot=snapshot(stale))).kind == 'refresh'
    assert plan_cards(context(quotes=(quote(visit_id='old'),))).kind == 'refresh'


def test_slot_prerequisite_requires_next_usable_selected_card_and_capacity_quote() -> None:
    from card_plan import plan_cards
    loadout = CardLoadout(id='l', name='Farm', priority=('cards.damage', 'cards.health'))
    program = CardProgram(version=1, gem_cap=500, goals=(SlotGoal(id='slots', kind='slots',
        capacity=2, when_usable_card=True),), loadouts=(loadout,), selected_loadout_id='l')
    ctx = context(program=program, snapshot=snapshot(item(ownership='owned'), item('cards.health', 'unowned')),
        quotes=(quote(kind='slot', capacity=1),))
    assert plan_cards(ctx).reason == 'no_usable_slot_card'
    ready = ctx.model_copy(update={'snapshot': snapshot(item(ownership='owned'), item('cards.health', 'owned'))})
    assert plan_cards(ready).kind == 'slot'
    assert plan_cards(ready.model_copy(update={'quotes': (quote(kind='slot', capacity=2),)})).kind == 'refresh'


def test_equipment_application_requires_complete_current_evidence_and_clear_is_explicit() -> None:
    from card_plan import plan_cards
    loadout = CardLoadout(id='l', name='Farm', priority=('cards.damage',))
    ctx = context(program=CardProgram(version=1, gem_cap=500, loadouts=(loadout,), selected_loadout_id='l'),
        snapshot=snapshot(item(ownership='owned')))
    assert plan_cards(ctx).desired == ('cards.damage',)
    stale = ctx.snapshot.model_copy(update={'equipment_evidence': None})
    assert plan_cards(ctx.model_copy(update={'snapshot': stale})).kind == 'refresh'
    cleared = plan_cards(context(command=command('clear'), snapshot=snapshot(item(), equipped=('cards.damage',))))
    assert (cleared.kind, cleared.desired) == ('apply', ())
    done = plan_cards(ctx.model_copy(update={'snapshot': snapshot(item(ownership='owned'), equipped=('cards.damage',))}))
    assert done.kind == 'done'


def test_satisfied_program_and_unresolved_operation_fence() -> None:
    from card_models import CardOperation
    from card_plan import plan_cards
    assert plan_cards(context(snapshot=snapshot(item(ownership='owned')))).kind == 'done'
    unresolved = CardOperation(operation_id='op', command=command(), status='reconciliation_required',
        created_at=90., updated_at=100.)
    assert plan_cards(context(unresolved_operation=unresolved, paused=True)).reason == 'reconciliation_required'


def test_objective_unlock_observation_distinguishes_owned_unowned_and_missing() -> None:
    from account_state import AccountRevision, Evidence, Fact
    from objectives import GRAPH
    objective = next(row for row in GRAPH if row.id == 'cards.unlock.health')
    evidence = Evidence(100., .99, 'Health', 'owned', (0, 0, 1, 1), 1, 1, 'frame')
    for ownership, expected in [('owned', True), ('unowned', False), ('unknown', None)]:
        revision = AccountRevision(account_id='a', cards=(Fact('cards.health', ownership,
            'observed', evidence, SCOPE),))
        assert objective.satisfied_by(revision) is expected
    assert objective.satisfied_by(AccountRevision(cards=())) is None
    assert not objective.never_satisfiable


def test_card_quote_rejects_unsupported_quantities_and_unbound_slot_prices() -> None:
    from pydantic import ValidationError
    for changes in ({'quantity': 2}, {'kind': 'slot'}, {'capacity': 1}, {'price': 0}):
        with pytest.raises(ValidationError):
            quote(**changes)


@pytest.mark.parametrize('confidence,expected', [(.90, 'buy'), (.899, 'refresh'), (None, 'refresh')])
def test_field_confidence_boundary(confidence: float | None, expected: str) -> None:
    from card_plan import plan_cards
    row = item()
    evidence = row.field_evidence['ownership'].model_copy(update={'confidence': confidence})
    row = row.model_copy(update={'field_evidence': {'ownership': evidence}})
    assert plan_cards(context(snapshot=snapshot(row))).kind == expected


def test_quote_layout_and_exact_affordability_boundaries() -> None:
    from card_plan import plan_cards
    assert plan_cards(context(quotes=(quote(layout_id='foreign-layout'),))).kind == 'refresh'
    ctx = context(budget=CardBudget(cycle_id='cycle', cap=20, spent=0, pending=0),
        balance=BalanceInterval('gems', 60, 100, SCOPE, 100., 'wallet'))
    assert plan_cards(ctx).kind == 'buy'
    assert plan_cards(ctx.model_copy(update={'committed_gems': 1})).reason == 'reserve_protected'


@pytest.mark.parametrize('block', ['budget', 'reserve', 'visit', 'route', 'capability'])
def test_auto_loadout_applies_before_run_when_paid_progress_is_blocked(block: str) -> None:
    from card_plan import plan_cards
    loadout = CardLoadout(id='l', name='Farm', priority=('cards.health',))
    ctx = context(program=CardProgram(version=1, gem_cap=500, goals=(goal(),),
        loadouts=(loadout,), selected_loadout_id='l'),
        snapshot=snapshot(item(), item('cards.health', 'owned')))
    changes: dict[str, Any] = {
        'budget': {'budget': CardBudget(cycle_id='cycle', cap=0, spent=0, pending=0)},
        'reserve': {'committed_gems': 500}, 'visit': {'cards_bought_this_visit': 10},
        'route': {'eligible_goal_ids': ()},
        'capability': {'capabilities': ctx.capabilities.model_copy(update={'buy_one': False})},
    }[block]
    ctx = ctx.model_copy(update=changes)
    decision = plan_cards(ctx)
    assert (decision.kind, decision.desired) == ('apply', ('cards.health',))
    assert plan_cards(ctx.model_copy(update={'command': command()})).kind == 'wait'
    assert plan_cards(ctx.model_copy(update={'policy': CardPolicy()})).reason == 'automation_disabled'
    already_equipped = ctx.snapshot.model_copy(update={'equipped': ('cards.health',)})
    assert plan_cards(ctx.model_copy(update={'snapshot': already_equipped})).kind == 'wait'


def test_explicit_unavailable_target_blocks_group_even_with_other_unknown_target() -> None:
    from card_plan import plan_cards
    group = AcquireGoal(id='g', kind='acquire', targets=(CardTarget(card_id='cards.damage'),
        CardTarget(card_id='cards.health')))
    ctx = context(program=CardProgram(version=1, gem_cap=500, goals=(group,)),
        snapshot=snapshot(item(ownership='unavailable')))
    assert plan_cards(ctx).reason == 'target_unavailable'


def test_automatic_goal_command_cannot_buy_ten() -> None:
    from card_plan import plan_cards
    decision = plan_cards(context(command=command(source='automatic', quantity=10, goal_id='g'),
        quotes=(quote(10),)))
    assert decision.kind == 'wait'


@pytest.mark.parametrize('field', ['scope', 'visit_id', 'observed_at', 'confidence'])
def test_equipment_provenance_must_be_current_for_apply(field: str) -> None:
    from card_plan import plan_cards
    ctx = context(command=command('clear'), snapshot=snapshot(item(), equipped=('cards.damage',)))
    values = {'scope': FactScope('other', 'lease', 'generation', 1), 'visit_id': 'previous',
              'observed_at': 50., 'confidence': .8}
    evidence = ctx.snapshot.equipment_evidence.model_copy(update={field: values[field]})
    ctx = ctx.model_copy(update={'snapshot': ctx.snapshot.model_copy(update={'equipment_evidence': evidence})})
    assert plan_cards(ctx).kind == 'refresh'


def test_post_mutation_floor_rejects_retained_level_and_equipment_authority() -> None:
    from card_plan import current_snapshot, plan_cards
    old = item(ownership='owned', level=2)
    snap = snapshot(old).model_copy(update={'observed_at': 102.})
    ctx = context(now=103., snapshot=snap, evidence_after=101.,
        program=CardProgram(version=1, gem_cap=500, goals=(goal(min_level=3),)))
    current = current_snapshot(ctx)
    assert current is not None and current.items[0].level == 2
    assert 'level' not in current.items[0].field_evidence
    assert not current.equipment_complete
    assert plan_cards(ctx).kind == 'refresh'
    assert current_snapshot(ctx.model_copy(update={'snapshot': snapshot(old)})) is None


def test_post_mutation_floor_rejects_old_quote_even_on_new_snapshot() -> None:
    from card_plan import current_quote
    assert current_quote(context(evidence_after=100., now=102.), 'buy', 1) is None
