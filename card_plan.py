"""Pure Cards planning. Prices and affordability are advisory until journal prepare."""
from __future__ import annotations

from card_models import (AcquireGoal, CardDecision, CardFieldEvidence, CardItem,
    CardLoadout, CardObservationField, CardPlanContext, CardQuote, CardSnapshot,
    LoadoutResolution, SlotGoal)

EVIDENCE_MAX_AGE = 30.
MIN_CONFIDENCE = .90
_UNRESOLVED = frozenset({'preflight', 'dispatched', 'verifying', 'reconciliation_required'})


def _fresh(observed_at: float, now: float) -> bool:
    return 0 <= now - observed_at <= EVIDENCE_MAX_AGE


def _evidence_current(evidence: CardFieldEvidence | None, snapshot: CardSnapshot) -> bool:
    return (evidence is not None and evidence.scope == snapshot.scope
            and evidence.visit_id == snapshot.visit_id
            and evidence.confidence is not None and evidence.confidence >= MIN_CONFIDENCE
            and _fresh(evidence.observed_at, snapshot.observed_at))


def _field_current(item: CardItem, field: CardObservationField, snapshot: CardSnapshot) -> bool:
    return _evidence_current(item.field_evidence.get(field), snapshot)


def goal_met(goal: AcquireGoal | SlotGoal, snapshot: CardSnapshot) -> bool | None:
    """Three-valued completion; retained historical fields cannot complete goals."""
    if isinstance(goal, SlotGoal):
        if snapshot.capacity is None or snapshot.confidence is None or snapshot.confidence < MIN_CONFIDENCE:
            return None
        return snapshot.capacity >= goal.capacity
    items = {item.card_id: item for item in snapshot.items}
    answers: list[bool | None] = []
    for target in goal.targets:
        item = items.get(target.card_id)
        if item is None or not _field_current(item, 'ownership', snapshot) or item.ownership == 'unknown':
            answers.append(None)
        elif item.ownership != 'owned':
            answers.append(False)
        elif target.min_level is None:
            answers.append(True)
        elif item.level is None or not _field_current(item, 'level', snapshot):
            answers.append(None)
        else:
            answers.append(item.level >= target.min_level)
    # Unknown even alongside a known deficit requires observation before buying.
    return None if None in answers else all(answers)


def resolve_loadout(loadout: CardLoadout, snapshot: CardSnapshot) -> LoadoutResolution:
    """First capacity-count observed-owned entries, never an implicit empty set."""
    if snapshot.capacity is None:
        return LoadoutResolution(reason='capacity_unknown')
    if snapshot.capacity == 0 or not loadout.priority:
        return LoadoutResolution(reason='no_usable_cards')
    items = {item.card_id: item for item in snapshot.items}
    desired: list[str] = []
    missing: list[str] = []
    for card_id in loadout.priority:
        item = items.get(card_id)
        if item is None or not _field_current(item, 'ownership', snapshot) or item.ownership == 'unknown':
            return LoadoutResolution(missing=tuple(missing), reason='inventory_unknown')
        if item.ownership == 'owned':
            desired.append(card_id)
            if len(desired) == snapshot.capacity:
                break
        else:
            missing.append(card_id)
    if not desired:
        return LoadoutResolution(missing=tuple(missing), reason='no_usable_cards')
    return LoadoutResolution(desired=tuple(desired), missing=tuple(missing))


def _refresh(context: CardPlanContext, reason: str, goal_id: str | None = None) -> CardDecision:
    return CardDecision(kind='refresh' if context.capabilities.inventory else 'wait',
        reason=reason if context.capabilities.inventory else 'unsupported_screen', goal_id=goal_id)


def current_snapshot(context: CardPlanContext) -> CardSnapshot | None:
    snapshot = context.snapshot
    if (snapshot is None or snapshot.scope != context.scope or snapshot.visit_id != context.visit_id
            or not _fresh(snapshot.observed_at, context.now)
            or snapshot.confidence is None or snapshot.confidence < MIN_CONFIDENCE):
        return None
    if context.evidence_after is not None:
        boundary = context.evidence_after
        if snapshot.observed_at <= boundary:
            return None
        items = tuple(item.model_copy(update={'field_evidence': {
            name: proof for name, proof in item.field_evidence.items() if proof.observed_at > boundary
        }}) for item in snapshot.items)
        equipment = snapshot.equipment_evidence
        snapshot = snapshot.model_copy(update={'items': items,
            'equipment_complete': snapshot.equipment_complete and equipment is not None
                                  and equipment.observed_at > boundary})
    # Advance the evaluation clock without lending its freshness to item fields.
    return snapshot.model_copy(update={'observed_at': context.now})


def _loadout(context: CardPlanContext, loadout_id: str | None) -> CardLoadout | None:
    if context.program is None or loadout_id is None:
        return None
    return next((row for row in context.program.loadouts if row.id == loadout_id), None)


def _apply(context: CardPlanContext, snapshot: CardSnapshot, loadout_id: str | None,
           *, clear: bool = False) -> CardDecision:
    if not snapshot.equipment_complete or snapshot.equipped is None or not _evidence_current(
            snapshot.equipment_evidence, snapshot):
        return _refresh(context, 'equipment_unknown')
    desired: tuple[str, ...] = ()
    if not clear:
        loadout = _loadout(context, loadout_id)
        if loadout is None:
            return CardDecision(kind='wait', reason='loadout_unknown', loadout_id=loadout_id)
        resolution = resolve_loadout(loadout, snapshot)
        if resolution.desired is None:
            if resolution.reason in {'inventory_unknown', 'capacity_unknown'}:
                return _refresh(context, resolution.reason)
            return CardDecision(kind='wait', reason=resolution.reason or 'no_usable_cards', loadout_id=loadout_id)
        desired = resolution.desired
    if set(desired) == set(snapshot.equipped):
        return CardDecision(kind='done', reason='equipment_satisfied', loadout_id=loadout_id, desired=desired)
    if not context.capabilities.assign:
        return CardDecision(kind='wait', reason='unsupported_screen', loadout_id=loadout_id)
    return CardDecision(kind='apply', reason='equipment_pending', loadout_id=loadout_id, desired=desired)


def current_quote(context: CardPlanContext, kind: str, quantity: int) -> CardQuote | None:
    """A current screen quote; catalog prices are never spending authority."""
    matches = [quote for quote in context.quotes if quote.kind == kind and quote.quantity == quantity
        and quote.scope == context.scope and quote.visit_id == context.visit_id
        and quote.layout_id == context.capabilities.layout_id and _fresh(quote.observed_at, context.now)
        and (context.evidence_after is None or quote.observed_at > context.evidence_after)
        and (kind != 'slot' or context.snapshot is not None and quote.capacity == context.snapshot.capacity)]
    if not matches:
        return None
    newest = max(quote.observed_at for quote in matches)
    latest = [quote for quote in matches if quote.observed_at == newest]
    return latest[0] if len({quote.price for quote in latest}) == 1 else None


def _paid(context: CardPlanContext, kind: str, quantity: int, goal_id: str | None) -> CardDecision:
    capability = (context.capabilities.buy_slot if kind == 'slot' else
                  context.capabilities.buy_ten if quantity == 10 else context.capabilities.buy_one)
    if not capability:
        return CardDecision(kind='wait', reason='unsupported_screen', goal_id=goal_id)
    if kind == 'buy' and context.cards_bought_this_visit + quantity > context.policy.max_per_visit:
        return CardDecision(kind='wait', reason='visit_limit', goal_id=goal_id)
    quote = current_quote(context, kind, quantity)
    if quote is None:
        return _refresh(context, 'quote_unknown', goal_id)
    balance = context.balance
    if (balance is None or balance.currency != 'gems' or balance.scope != context.scope
            or balance.source != 'observed' or balance.lower is None
            or not _fresh(balance.observed_at, context.now)):
        return _refresh(context, 'wallet_unknown', goal_id)
    assert context.program is not None
    remaining = min(context.budget.cap, context.program.gem_cap) - context.budget.spent - context.budget.pending
    if quote.price > remaining:
        return CardDecision(kind='wait', reason='budget_reached', goal_id=goal_id)
    available = balance.lower - context.committed_gems - context.policy.gem_floor
    if quote.price > available:
        return CardDecision(kind='wait', reason='reserve_protected', goal_id=goal_id)
    return CardDecision(kind=kind, reason='ready', goal_id=goal_id, quantity=quantity)


def _progress(context: CardPlanContext) -> CardDecision:
    """Return one bounded next action, preserving all execution and spending gates."""
    if context.unresolved_operation is not None and context.unresolved_operation.status in _UNRESOLVED:
        return CardDecision(kind='wait', reason='reconciliation_required')
    command = context.command
    if command is not None:
        if command.scope != context.scope:
            return CardDecision(kind='wait', reason='scope_mismatch')
        if command.program_revision != context.program_revision:
            return CardDecision(kind='wait', reason='program_changed')
        if command.kind in {'buy', 'slot'} and command.budget_cycle_id != context.budget.cycle_id:
            return CardDecision(kind='wait', reason='budget_cycle_changed')
        if command.source == 'automatic' and command.goal_id is not None and command.quantity != 1:
            return CardDecision(kind='wait', reason='automatic_goal_requires_single')
    if context.paused:
        return CardDecision(kind='wait', reason='paused')
    if context.in_run:
        return CardDecision(kind='wait', reason='waiting_for_run_end')
    if command is not None and command.kind == 'cancel':
        return CardDecision(kind='wait', reason='cancel_requires_store')
    if not context.armed:
        return CardDecision(kind='wait', reason='not_armed')
    if command is not None and command.kind == 'refresh':
        return _refresh(context, 'refresh_requested')
    if (command is None or command.source != 'manual') and not context.policy.enabled:
        return CardDecision(kind='wait', reason='automation_disabled')
    if context.program is None:
        return CardDecision(kind='wait', reason='program_missing')
    snapshot = current_snapshot(context)
    if snapshot is None:
        return _refresh(context, 'inventory_unknown')
    if command is not None and command.kind in {'apply', 'clear'}:
        return _apply(context, snapshot, command.loadout_id, clear=command.kind == 'clear')
    # A route supplies an explicit allow-list; () means another gem lane owns spending.
    eligible = context.eligible_goal_ids
    if command is not None and command.kind in {'buy', 'slot'} and command.goal_id is None:
        if eligible is not None:
            return CardDecision(kind='wait', reason='route_waiting')
        if command.kind == 'slot' and snapshot.capacity is None:
            return _refresh(context, 'capacity_unknown')
        return _paid(context, command.kind, command.quantity, None)
    goals = context.program.goals
    if command is not None and command.goal_id is not None:
        goals = tuple(goal for goal in goals if goal.id == command.goal_id)
        if not goals:
            return CardDecision(kind='wait', reason='goal_unknown')
    elif eligible is not None:
        by_id = {goal.id: goal for goal in goals}
        goals = tuple(by_id[goal_id] for goal_id in eligible if goal_id in by_id)
        if not goals:
            return CardDecision(kind='wait', reason='route_waiting')
    for goal in goals:
        met = goal_met(goal, snapshot)
        if met is True:
            continue
        if eligible is not None and goal.id not in eligible:
            return CardDecision(kind='wait', reason='route_waiting', goal_id=goal.id)
        if isinstance(goal, AcquireGoal):
            items = {item.card_id: item for item in snapshot.items}
            if any(item.card_id in {target.card_id for target in goal.targets}
                   and _field_current(item, 'ownership', snapshot)
                   and item.ownership in {'unavailable', 'locked'} for item in items.values()):
                return CardDecision(kind='wait', reason='target_unavailable', goal_id=goal.id)
        if met is None:
            return _refresh(context, 'inventory_unknown' if isinstance(goal, AcquireGoal) else 'capacity_unknown', goal.id)
        kind = 'buy' if isinstance(goal, AcquireGoal) else 'slot'
        if command is not None and command.kind != kind:
            return CardDecision(kind='wait', reason='command_goal_mismatch', goal_id=goal.id)
        if isinstance(goal, SlotGoal) and goal.when_usable_card:
            loadout = _loadout(context, context.program.selected_loadout_id)
            if loadout is None:
                return CardDecision(kind='wait', reason='no_usable_slot_card', goal_id=goal.id)
            assert snapshot.capacity is not None
            extended = resolve_loadout(loadout, snapshot.model_copy(update={'capacity': snapshot.capacity + 1}))
            if extended.reason in {'inventory_unknown', 'capacity_unknown'}:
                return _refresh(context, extended.reason, goal.id)
            if extended.desired is None or len(extended.desired) <= snapshot.capacity:
                return CardDecision(kind='wait', reason='no_usable_slot_card', goal_id=goal.id)
        quantity = command.quantity if command is not None else 1
        return _paid(context, kind, quantity, goal.id)
    if command is None and context.program.selected_loadout_id is not None:
        return _apply(context, snapshot, context.program.selected_loadout_id)
    return CardDecision(kind='done', reason='goals_satisfied')


def plan_cards(context: CardPlanContext) -> CardDecision:
    """Progress goals, then equip before returning to a run when spending must wait."""
    decision = _progress(context)
    if (decision.kind == 'wait' and decision.reason in {
            'budget_reached', 'reserve_protected', 'visit_limit', 'route_waiting', 'unsupported_screen'}
            and context.command is None and context.program is not None
            and context.program.selected_loadout_id is not None):
        snapshot = current_snapshot(context)
        if snapshot is not None:
            equipment = _apply(context, snapshot, context.program.selected_loadout_id)
            if equipment.kind == 'apply':
                return equipment
    return decision
