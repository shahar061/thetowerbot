"""Bounded strategy programs, evaluated identically by workers and previews."""
from __future__ import annotations

import hashlib
import math
import operator
from dataclasses import dataclass
from typing import Any, Mapping

import builds
import upgrades

MAX_BLOCKS = 80
MAX_DEPTH = 6

COMPARISONS = {'gte': operator.ge, 'lte': operator.le, 'gt': operator.gt, 'lt': operator.lt}


def validate_program(value: object, lane: str) -> tuple[dict[str, Any], ...]:
    if lane not in {'workshop', 'battle'}:
        raise ValueError('unknown strategy lane')
    seen: set[str] = set()

    def number(raw: object, name: str, low: int = 0, high: int = 100) -> int:
        if type(raw) is not int or not low <= raw <= high:
            raise ValueError(f'{name} must be an integer between {low} and {high}')
        return raw

    def uid(raw: object) -> str:
        if not isinstance(raw, str) or upgrades.by_id(raw) is None:
            raise ValueError('unknown block upgrade')
        if lane == 'battle' and upgrades.by_id(raw).unlock:
            raise ValueError('battle blocks cannot buy Workshop unlocks')
        return raw

    def walk(items: object, depth: int) -> tuple[dict[str, Any], ...]:
        if depth > MAX_DEPTH or not isinstance(items, (list, tuple)):
            raise ValueError('strategy program must be a bounded block list')
        result = []
        for raw in items:
            if not isinstance(raw, dict):
                raise ValueError('strategy block must be an object')
            block = dict(raw)
            identity = block.get('id')
            if (not isinstance(identity, str) or not identity or len(identity) > 100
                    or any(char.isspace() for char in identity) or identity in seen):
                raise ValueError('strategy block IDs must be unique nonempty tokens')
            seen.add(identity)
            if len(seen) > MAX_BLOCKS:
                raise ValueError('too many strategy blocks')
            kind = block.get('type')
            allowed = {'id', 'type'}
            if kind == 'native':
                allowed |= {'policy', 'phase'}
                if block.get('policy') not in {'opening', 'turtle'}:
                    raise ValueError('unknown native policy')
                phases = {'starter', 'economy', 'objectives', 'fallback'} if lane == 'workshop' else {'battle'}
                if block.get('phase') not in phases:
                    raise ValueError('unknown native policy phase')
            elif kind == 'buy':
                allowed.add('upgrade_id')
                uid(block.get('upgrade_id'))
            elif kind == 'pool':
                allowed |= {'upgrade_ids', 'selection', 'weights', 'discount_pct', 'reference_upgrade_id',
                            'max_purchases', 'count_scope', 'decay_pct', 'weight_floor',
                            'targets', 'level_caps', 'price_cap', 'wallet_share_pct'}
                ids = block.get('upgrade_ids')
                if not isinstance(ids, (tuple, list)) or not ids or len(ids) > 30:
                    raise ValueError('pool requires 1 to 30 upgrades')
                ids = [uid(item) for item in ids]
                if len(set(ids)) != len(ids):
                    raise ValueError('duplicate pool upgrade')
                block['upgrade_ids'] = ids
                if block.get('selection', 'priority') not in {'priority', 'weighted'}:
                    raise ValueError('unknown pool selection')
                expected_scope = 'account' if lane == 'workshop' else 'run'
                if block.get('count_scope', expected_scope) != expected_scope:
                    raise ValueError(f'{lane} purchase counts must use {expected_scope} scope')
                for field in ('discount_pct', 'decay_pct'):
                    if field in block:
                        number(block[field], field)
                if 'max_purchases' in block:
                    number(block['max_purchases'], 'max_purchases', 1, 100000)
                if 'weight_floor' in block:
                    number(block['weight_floor'], 'weight_floor', 1, 100000)
                weights = block.get('weights', {})
                if not isinstance(weights, dict) or set(weights) - set(ids):
                    raise ValueError('pool weights must name pool upgrades')
                for weight in weights.values():
                    number(weight, 'weight', 1, 100000)
                reference = block.get('reference_upgrade_id', 'priority')
                if reference != 'priority':
                    uid(reference)
                if 'reference_upgrade_id' in block and 'discount_pct' not in block:
                    raise ValueError('price reference requires a discount')
                targets = block.get('targets', {})
                if not isinstance(targets, dict) or set(targets) - set(ids):
                    raise ValueError('pool targets must name pool upgrades')
                for target in targets.values():
                    if (isinstance(target, bool) or not isinstance(target, (int, float))
                            or not math.isfinite(target) or target < 0):
                        raise ValueError('pool target must be a finite number')
                caps = block.get('level_caps', {})
                if not isinstance(caps, dict) or set(caps) - set(ids):
                    raise ValueError('pool level caps must name pool upgrades')
                for cap in caps.values():
                    if not isinstance(cap, dict) or set(cap) - {'base', 'per_level_of', 'step'}:
                        raise ValueError('unknown level cap field')
                    number(cap.get('base'), 'level cap base', 0, 100000)
                    if 'per_level_of' in cap:
                        if not isinstance(cap['per_level_of'], str) or upgrades.by_id(cap['per_level_of']) is None:
                            raise ValueError('unknown level cap upgrade')
                        number(cap.get('step', 1), 'level cap step', 1, 1000)
                    elif 'step' in cap:
                        raise ValueError('level cap step requires per_level_of')
                if 'price_cap' in block:
                    number(block['price_cap'], 'price_cap', 1, 1000000000000)
                if 'wallet_share_pct' in block:
                    number(block['wallet_share_pct'], 'wallet_share_pct', 1, 100)
            elif kind == 'condition':
                allowed |= {'field', 'op', 'value', 'then', 'else', 'upgrade_id'}
                field = block.get('field')
                if field not in {'best_tier_1_wave', 'wave', 'wallet', 'upgrade_value', 'def_abs_coverage'}:
                    raise ValueError('unknown condition fact')
                if lane == 'workshop' and field in {'wave', 'def_abs_coverage'}:
                    raise ValueError(f'{field} is only available in battle')
                if field == 'upgrade_value':
                    if not isinstance(block.get('upgrade_id'), str) or upgrades.by_id(block['upgrade_id']) is None:
                        raise ValueError('unknown condition upgrade')
                elif 'upgrade_id' in block:
                    raise ValueError('only upgrade value conditions name an upgrade')
                if block.get('op') not in COMPARISONS:
                    raise ValueError('unknown condition comparison')
                raw = block.get('value')
                if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw)
                        or not 0 <= raw <= 1000000000000):
                    raise ValueError('condition value must be a number between 0 and 1000000000000')
                block['then'] = list(walk(block.get('then', []), depth + 1))
                block['else'] = list(walk(block.get('else', []), depth + 1))
            elif kind == 'fallback':
                allowed.add('blocks')
                block['blocks'] = list(walk(block.get('blocks', []), depth + 1))
            elif kind == 'budget':
                allowed |= {'metric', 'target', 'ceiling', 'blocks'}
                if lane != 'workshop':
                    raise ValueError('budgets are only available in the Workshop')
                if block.get('metric') != 'utility_spent':
                    raise ValueError('unknown budget metric')
                target = number(block.get('target'), 'budget target', 1, 1000000000000)
                number(block.get('ceiling'), 'budget ceiling', target, 1000000000000)
                block['blocks'] = list(walk(block.get('blocks', []), depth + 1))
                if not block['blocks']:
                    raise ValueError('budget requires at least one block')
            elif kind == 'save_for':
                allowed.add('goal')
                block['goal'] = list(walk(block.get('goal', []), depth + 1))
                if len(block['goal']) != 1 or block['goal'][0]['type'] not in {'buy', 'pool'}:
                    raise ValueError('save for goal needs exactly one buy or pool block')
                if 'discount_pct' in block['goal'][0]:
                    raise ValueError('a saving goal cannot be a discount pool')
            elif kind == 'while_saving':
                allowed |= {'upgrade_id', 'blocks'}
                if 'upgrade_id' in block and (not isinstance(block['upgrade_id'], str)
                                              or upgrades.by_id(block['upgrade_id']) is None):
                    raise ValueError('unknown saving goal upgrade')
                block['blocks'] = list(walk(block.get('blocks', []), depth + 1))
                if not block['blocks']:
                    raise ValueError('while saving requires at least one block')
            elif kind != 'wait':
                raise ValueError('unknown strategy block type')
            if set(block) - allowed:
                raise ValueError('unknown strategy block field')
            result.append(block)
        return tuple(result)

    return walk(value, 0)


def template_program(policy: str, lane: str) -> tuple[dict[str, Any], ...]:
    phases = ('starter', 'economy', 'objectives', 'fallback') if lane == 'workshop' else ('battle',)
    return validate_program([{'id': f'{policy}.{phase}', 'type': 'native',
                              'policy': policy, 'phase': phase} for phase in phases], lane)


def child_lists(block: Mapping[str, Any]) -> tuple[list[dict[str, Any]], ...]:
    """Nested block lists, in evaluation order."""
    kind = block['type']
    if kind == 'condition':
        return (block['then'], block['else'])
    if kind in {'fallback', 'budget', 'while_saving'}:
        return (block['blocks'],)
    if kind == 'save_for':
        return (block['goal'],)
    return ()


def program_upgrade_ids(program: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    """Rows the observer may inspect even before a block can recommend a buy."""
    result: list[str] = []
    for block in program:
        kind = block["type"]
        if kind == "buy":
            result.append(block["upgrade_id"])
        elif kind == "pool":
            result.extend(block["upgrade_ids"])
            reference = block.get("reference_upgrade_id")
            if reference and reference != "priority":
                result.append(reference)
        elif kind == "native":
            result.extend(("cash_per_wave", "coins_per_kill_bonus", "cash_bonus",
                           "defense_absolute", "thorns", "health", "coins_per_wave",
                           "damage", "attack_speed"))
        elif kind == "condition":
            if block["field"] == "upgrade_value":
                result.append(block["upgrade_id"])
            elif block["field"] == "def_abs_coverage":
                result.extend(("defense_absolute", "defense_percent"))
        elif kind == "while_saving" and "upgrade_id" in block:
            result.append(block["upgrade_id"])
        for children in child_lists(block):
            result.extend(program_upgrade_ids(tuple(children)))
    return tuple(dict.fromkeys(result))


@dataclass
class _Choice:
    block_id: str
    upgrade_id: str | None = None
    reason: str = ''
    native: Any = None
    weights: dict[str, float] | None = None
    pending: Any = None
    wait: bool = False
    target: float | None = None
    observation_ids: tuple[str, ...] = ()
    phase_id: str | None = None
    phase_state: str | None = None
    next_phase_id: str | None = None
    transition_reason: str | None = None
    save_price: int | None = None


def evaluate_program(route: Any, facts: Any, pending: Any, lane: str) -> Any:
    """Evaluate one bounded program. No writes, devices, clocks or random state."""
    from fleet.build_route_eval import (BattleDecision, DecisionTrace, PendingDecision,
                                        RouteEvaluation)
    from fleet.reroll_planner import (RerollDecision, RerollFacts, _ban_closure,
                                      choose_native_phase, native_phase_progress)
    wallet = facts.wallet_coins if lane == 'workshop' else facts.battle_cash
    if (not facts.account_id or not facts.worker or type(wallet) is not int or wallet < 0
            or facts.observed_at is None or facts.now is None):
        return RouteEvaluation.unknown('Verified identity, wallet or evidence time is missing', facts)
    age = facts.now - facts.observed_at
    if age < 0 or age > (120 if lane == 'workshop' else 2):
        return RouteEvaluation.unknown('Account evidence is stale', facts)
    if lane == 'battle' and (facts.run_id is None or facts.wave is None or facts.wave < 1):
        return RouteEvaluation.unknown('Live run and wave are unverified', facts)
    program = route.workshop.blocks if lane == 'workshop' else route.battle.blocks
    ceiling = wallet * (route.workshop.coin_spend_limit_pct if lane == 'workshop' else 100) // 100
    excluded = _ban_closure(route.workshop.banned_upgrade_ids)
    counts = facts.confirmed_purchases if lane == 'workshop' else facts.run_purchases
    rejected: list[str] = []
    if lane == 'workshop' and facts.best_tier_1_wave is not None and facts.best_tier_1_wave >= 60:
        stopped = RerollDecision(facts.account_id, 'stones', 'Review Ultimate Weapon', 'needs_operator',
            None, None, None, None, wallet, facts.lifetime_coins,
            'Tier 1 Wave 60 was verified; Ultimate Weapon choice stays with the operator.')
        return RouteEvaluation(facts.account_id, route.revision, 'blocked', stopped,
            DecisionTrace('wave60', stopped.reason, age), facts.observed_at)

    def price_for(uid: str, *, reference: bool = False) -> int | None:
        if lane == 'workshop':
            price = facts.prices.get(uid)
        else:
            row = facts.upgrade_rows.get(uid, {})
            seen = row.get('observed_at')
            allowed_statuses = {'available', 'unaffordable'} if reference else {'available'}
            if (row.get('status') not in allowed_statuses or row.get('value') is None
                    or not isinstance(seen, (float, int)) or not 0 <= facts.now - seen <= 60):
                return None
            price = row.get('price')
        return price if type(price) is int and price >= 0 else None

    def eligible(uid: str, *, ignore_funds: bool = False) -> bool:
        if uid in excluded:
            rejected.append(f'{uid}: blocked by Never Buy')
            return False
        price = price_for(uid)
        if price is None or (not ignore_funds and price > ceiling):
            return False
        if budget_room is not None and price > budget_room:
            rejected.append(f'{uid}: exceeds budget ceiling')
            return False
        if lane == 'workshop':
            upgrade = upgrades.by_id(uid)
            if upgrade.unlock and facts.purchases.get(uid, 0):
                return False
            gates = {child: item.id for item in upgrades.CATALOG if item.unlock for child in item.unlocks}
            gate = gates.get(uid) or builds.prerequisites().get(uid)
            if gate and facts.purchases.get(gate, 0) <= 0:
                return False
        return True

    native_intents: dict[str, Any] = {}
    budget_room: int | None = None
    saving: _Choice | None = None
    def native_facts(policy: str) -> RerollFacts:
        return RerollFacts(
            facts.account_id, facts.best_tier_1_wave, facts.purchases, facts.values,
            wallet, facts.lifetime_coins, facts.prices,
            spend_fraction=route.workshop.coin_spend_limit_pct / 100,
            variant=facts.variant, utility_spent_coins=facts.utility_spent_coins,
            policy=policy)

    def native_decision(policy: str, phase: str) -> Any:
        return choose_native_phase(native_facts(policy), phase,
            banned_upgrade_ids=route.workshop.banned_upgrade_ids,
            reference=native_intents.get(policy))

    def priority_reference(scopes: tuple[Any, ...], current: str) -> str | None:
        for items in scopes:
            for item in items:
                if item['id'] == current:
                    continue
                if item['type'] == 'buy':
                    return item['upgrade_id']
                if item['type'] == 'pool':
                    return item['upgrade_ids'][0]
                if item['type'] == 'native':
                    if lane == 'workshop':
                        decision = native_intents.get(item['policy']) or native_decision(item['policy'], item['phase'])
                        if decision is not None:
                            return decision.upgrade_id
                    else:
                        for rule in native_battle_rules():
                            if rule.upgrade_id not in excluded and price_for(rule.upgrade_id, reference=True) is not None:
                                return rule.upgrade_id
                # Never enter an unrelated conditional branch to invent a reference.
        return None

    def observed_quote(uid: str | None) -> bool:
        if not uid or price_for(uid, reference=True) is None:
            return False
        if lane == 'battle':
            return True
        evidence = facts.price_evidence.get(uid, {})
        timestamp = evidence.get('observed_at')
        # An observed Workshop quote stays current only while the price memory
        # proves its account, purchase offset and discount signature unchanged.
        return (evidence.get('source') == 'observed' and type(timestamp) in {int, float}
                and math.isfinite(timestamp) and 0 <= timestamp <= facts.now)

    def observe_prices(identity: str, ids: list[str]) -> _Choice | None:
        if lane != 'workshop':
            return None
        gates = {child: item.id for item in upgrades.CATALOG if item.unlock for child in item.unlocks}
        needed = []
        for uid in dict.fromkeys(ids):
            gate = gates.get(uid) or builds.prerequisites().get(uid)
            if uid in excluded or (gate and facts.purchases.get(gate, 0) <= 0):
                continue
            if not observed_quote(uid):
                needed.append(uid)
        if not needed:
            return None
        return _Choice(identity, needed[0], 'Observe Workshop prices before comparing the cheap pool',
                       observation_ids=tuple(needed))

    def native_battle_rules() -> list[Any]:
        from fleet.reroll_survival import prioritize_survival
        from policy import UpgradeRule
        rules = (UpgradeRule('cash_per_wave', target=10),
                 UpgradeRule('coins_per_kill_bonus', target=1.25),
                 UpgradeRule('cash_bonus', target=1.25), UpgradeRule('defense_absolute'),
                 UpgradeRule('thorns', target=51), UpgradeRule('health'),
                 UpgradeRule('coins_per_wave', target=10), UpgradeRule('damage'), UpgradeRule('attack_speed'))
        remaining = []
        for rule in prioritize_survival(rules, facts.upgrade_rows):
            uid = rule.upgrade_id
            row = facts.upgrade_rows.get(uid, {})
            if (rule.target is not None and isinstance(row.get('value'), (float, int))
                    and upgrades.target_reached(uid, row['value'], rule.target)):
                continue
            remaining.append(rule)
        return remaining

    def native_battle(block: Mapping[str, Any]) -> _Choice | None:
        for rule in native_battle_rules():
            if eligible(rule.upgrade_id):
                return _Choice(block['id'], rule.upgrade_id, 'Native survival starters, then economy and combat priorities', target=rule.target)
        return None

    waiting_native: _Choice | None = None
    completed_native_phases: list[str] = []

    def phase_title(phase: str) -> str:
        return {"starter": "Survival Starter", "economy": "Early Economy",
                "objectives": "Upgrade objectives", "fallback": "Cheap fallback"}.get(phase, phase)

    def upgrade_value(uid: str) -> float | None:
        raw = facts.upgrade_rows.get(uid, {}).get('value') if lane == 'battle' else facts.values.get(uid)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
            return None
        return float(raw)

    def condition_value(block: Mapping[str, Any]) -> float | None:
        field = block['field']
        if field == 'wallet':
            return wallet
        if field == 'upgrade_value':
            return upgrade_value(block['upgrade_id'])
        if field == 'def_abs_coverage':
            absolute, percent = upgrade_value('defense_absolute'), upgrade_value('defense_percent')
            damage = facts.enemy_damage
            if absolute is None or percent is None or damage is None:
                return None
            remaining = damage * max(0.0, 1.0 - percent / 100.0)
            return math.inf if remaining <= 0 else absolute / remaining
        return getattr(facts, field)

    def pool_candidates(block: Mapping[str, Any], identity: str, reference_price: int | None,
                        *, ignore_funds: bool = False) -> dict[str, float]:
        candidates: dict[str, float] = {}
        for uid in block['upgrade_ids']:
            count = (counts or {}).get(uid, 0)
            if 'max_purchases' in block and count >= block['max_purchases']:
                continue
            cap = block.get('level_caps', {}).get(uid)
            if cap is not None:
                limit = cap['base'] + (cap.get('step', 1) * (counts or {}).get(cap['per_level_of'], 0)
                                       if 'per_level_of' in cap else 0)
                if count >= limit:
                    continue
            target = block.get('targets', {}).get(uid)
            if target is not None:
                value = upgrade_value(uid)
                if value is None:
                    rejected.append(f'{identity}: {uid} target value unknown')
                    continue
                if upgrades.target_reached(uid, value, float(target)):
                    continue
            if not eligible(uid, ignore_funds=ignore_funds):
                continue
            price = price_for(uid)
            if 'price_cap' in block and price > block['price_cap']:
                continue
            if 'wallet_share_pct' in block and price * 100 > wallet * block['wallet_share_pct']:
                continue
            if 'discount_pct' in block and not observed_quote(uid):
                continue
            if reference_price is not None and price * 100 > reference_price * (100 - block['discount_pct']):
                continue
            base = block.get('weights', {}).get(uid, 1)
            candidates[uid] = max(block.get('weight_floor', 1),
                                  base * ((100 - block.get('decay_pct', 0)) / 100) ** count)
        return candidates

    def unaffordable_pick(goal: Mapping[str, Any]) -> tuple[str, int] | None:
        if goal['type'] == 'buy':
            uid = goal['upgrade_id']
            return (uid, price_for(uid)) if eligible(uid, ignore_funds=True) else None
        needs_counts = 'max_purchases' in goal or 'level_caps' in goal or goal.get('decay_pct', 0) > 0
        if needs_counts and counts is None:
            return None
        uid = next(iter(pool_candidates(goal, goal['id'], None, ignore_funds=True)), None)
        return (uid, price_for(uid)) if uid else None

    def evaluate(items: Any, ancestors: tuple[Any, ...] = ()) -> _Choice | None:
        nonlocal waiting_native, budget_room, saving
        for index, block in enumerate(items):
            kind, identity = block['type'], block['id']
            if kind == 'wait':
                return _Choice(identity, reason='Wait block reached', wait=True)
            if kind == 'condition':
                value = condition_value(block)
                if value is None:
                    rejected.append(f'{identity}: condition evidence unknown')
                    return _Choice(identity, reason='Condition evidence unknown; decision paused', wait=True)
                matched = COMPARISONS[block['op']](value, block['value'])
                choice = evaluate(block['then'] if matched else block['else'], (items, *ancestors))
                if choice is not None:
                    return choice
            elif kind == 'save_for':
                if saving is not None:
                    rejected.append(f'{identity}: another goal is already saving')
                    continue
                choice = evaluate(block['goal'], (items, *ancestors))
                if choice is not None:
                    return choice
                pick = unaffordable_pick(block['goal'][0])
                if pick is None:
                    continue
                uid, price = pick
                name = upgrades.by_id(uid).name
                saving = _Choice(identity, uid, f'Saving for {name} ({wallet}/{price} coins)',
                                 wait=True, save_price=price)
            elif kind == 'while_saving':
                if saving is None or block.get('upgrade_id', saving.upgrade_id) != saving.upgrade_id:
                    continue
                choice = evaluate(block['blocks'], (items, *ancestors))
                if choice is not None:
                    return choice
            elif kind == 'fallback':
                choice = evaluate(block['blocks'], (items, *ancestors))
                if choice is not None:
                    return choice
            elif kind == 'budget':
                spent = facts.utility_spent_coins
                if spent is None:
                    rejected.append(f'{identity}: utility spend unknown')
                    return _Choice(identity, reason='Waiting for verified utility spend', wait=True)
                if spent >= block['target']:
                    continue
                outer = budget_room
                room = block['ceiling'] - spent
                budget_room = room if outer is None else min(outer, room)
                try:
                    choice = evaluate(block['blocks'], (items, *ancestors))
                finally:
                    budget_room = outer
                if choice is not None:
                    return choice
            elif kind == 'native':
                if lane == 'battle':
                    choice = native_battle(block)
                    if choice:
                        return choice
                else:
                    policy, phase = block['policy'], block['phase']
                    # An earlier native economy intent remains the main goal;
                    # fallback may buy cheaply while that goal needs funds.
                    if phase == 'objectives' and policy in native_intents:
                        continue
                    decision = native_decision(policy, phase)
                    progress, progress_reason = native_phase_progress(native_facts(policy), phase,
                        policy, decision, banned_upgrade_ids=route.workshop.banned_upgrade_ids)
                    next_phase = next((candidate for candidate in items[index + 1:]
                        if candidate['type'] == 'native'), None)
                    if decision is None:
                        if progress == 'waiting':
                            return _Choice(identity, reason=progress_reason, wait=True,
                                phase_id=phase, phase_state='waiting',
                                next_phase_id=next_phase['id'] if next_phase else None,
                                transition_reason=progress_reason)
                        if progress == 'blocked':
                            return _Choice(identity, reason=progress_reason, wait=True,
                                phase_id=phase, phase_state='blocked',
                                next_phase_id=next_phase['id'] if next_phase else None,
                                transition_reason=progress_reason)
                        completed_native_phases.append(phase_title(phase))
                        continue
                    current_state = "waiting" if progress == "waiting" else "active"
                    handoff = (f"{', '.join(completed_native_phases)} complete → "
                               f"{phase_title(phase)} {current_state}"
                               if completed_native_phases else progress_reason)
                    choice = _Choice(identity, decision.upgrade_id, decision.reason, native=decision,
                        phase_id=phase, phase_state=progress,
                        next_phase_id=next_phase['id'] if next_phase else None,
                        transition_reason=handoff)
                    if decision.state == 'save_coins' and phase != 'starter':
                        native_intents[policy] = decision
                        waiting_native = choice
                        continue
                    return choice
            elif kind == 'buy':
                if eligible(block['upgrade_id']):
                    return _Choice(identity, block['upgrade_id'], 'First affordable eligible upgrade')
            elif kind == 'pool':
                needs_counts = 'max_purchases' in block or 'level_caps' in block or block.get('decay_pct', 0) > 0
                if needs_counts and counts is None:
                    rejected.append(f'{identity}: confirmed purchase counts unavailable')
                    continue
                reference_price = None
                if 'discount_pct' in block:
                    reference = block.get('reference_upgrade_id', 'priority')
                    if reference == 'priority':
                        reference = priority_reference((items, *ancestors), identity)
                    reference_price = price_for(reference, reference=True) if reference else None
                    if reference_price is None or not observed_quote(reference):
                        rejected.append(f'{identity}: reference price unverified')
                        observation = observe_prices(identity, [*([reference] if reference else []), *block['upgrade_ids']])
                        if observation is not None:
                            return observation
                        continue
                candidates = pool_candidates(block, identity, reference_price)
                if not candidates:
                    if 'discount_pct' in block:
                        observation = observe_prices(identity, block['upgrade_ids'])
                        if observation is not None:
                            return observation
                    continue
                chosen = next(iter(candidates))
                selected = None
                if block.get('selection', 'priority') == 'weighted':
                    visit = facts.visit_id or f'{lane}:{facts.run_id if lane == "battle" else facts.account_id}'
                    matches = (pending is not None and pending.account_id == facts.account_id
                        and pending.revision == route.revision and pending.visit_id == visit
                        and pending.sequence == facts.decision_sequence and pending.matched_rule_id == identity)
                    if matches:
                        if pending.chosen_id not in candidates:
                            return _Choice(identity, reason='Pending block choice awaits valid evidence', wait=True)
                        chosen, selected = pending.chosen_id, pending
                    else:
                        key = f'{facts.account_id}:{route.revision}:{visit}:{facts.decision_sequence}:{identity}'
                        roll = (int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big') / 2**64) * sum(candidates.values())
                        for uid, weight in candidates.items():
                            if roll < weight:
                                chosen = uid
                                break
                            roll -= weight
                        selected = PendingDecision(facts.account_id, route.revision, visit,
                            facts.decision_sequence, hashlib.sha256(repr(candidates).encode()).hexdigest(),
                            chosen, None, candidates, identity)
                return _Choice(identity, chosen, 'Eligible pool after price, cap and affordability filters',
                               weights=candidates if selected else None, pending=selected,
                               target=block.get('targets', {}).get(chosen))
        return None

    choice = evaluate(program) or saving or waiting_native
    visit = facts.visit_id or f'{lane}:{facts.run_id if lane == "battle" else facts.account_id}'
    active_pending = (pending is not None and pending.account_id == facts.account_id
        and pending.revision == route.revision and pending.visit_id == visit
        and pending.sequence == facts.decision_sequence)
    if active_pending and (choice is None or (choice.pending != pending and not choice.observation_ids)):
        choice = _Choice(pending.matched_rule_id, reason="Pending block choice awaits valid evidence", wait=True)
    if choice is None:
        choice = _Choice('blocks', reason='No eligible block purchase; waiting for price, funds or conditions', wait=True,
            phase_id=completed_native_phases[-1] if completed_native_phases else None,
            phase_state='complete' if completed_native_phases else 'waiting',
            transition_reason=(f"{', '.join(completed_native_phases)} complete; waiting for the next eligible decision."
                               if completed_native_phases else None))
    weights = choice.weights or {}
    odds = {uid: weight / sum(weights.values()) for uid, weight in weights.items()}
    trace = DecisionTrace(choice.block_id, choice.reason, age,
        'worker price evidence' if lane == 'workshop' else 'cached same-run battle rows (up to 60s)', facts.variant,
        tuple(rejected), ceiling, phase_id=choice.phase_id, phase_state=choice.phase_state,
        next_phase_id=choice.next_phase_id, transition_reason=choice.transition_reason,
        eligible_odds=odds, observation_ids=choice.observation_ids)
    if choice.observation_ids:
        upgrade = upgrades.by_id(choice.upgrade_id)
        decision = RerollDecision(facts.account_id, "strategy_observe", "Verify cheap-pool prices",
            "observe_price", upgrade.id, upgrade.name, upgrade.category, None, wallet,
            facts.lifetime_coins, choice.reason)
        return RouteEvaluation(facts.account_id, route.revision, "projected", decision, trace, facts.observed_at, pending)
    if choice.save_price is not None and lane == 'workshop':
        upgrade = upgrades.by_id(choice.upgrade_id)
        decision = RerollDecision(facts.account_id, 'strategy', 'Follow assigned strategy', 'save_coins',
            upgrade.id, upgrade.name, upgrade.category, choice.save_price, wallet,
            facts.lifetime_coins, choice.reason)
        return RouteEvaluation(facts.account_id, route.revision, 'blocked', decision, trace, facts.observed_at, pending)
    if choice.wait:
        return RouteEvaluation(facts.account_id, route.revision, 'blocked', None, trace,
                               facts.observed_at, pending)
    if choice.native is not None:
        decision = choice.native
        status = 'blocked' if decision.state in {'needs_operator', 'save_coins'} else 'projected'
        if decision.state == 'buy' and facts.screen == 'workshop' and decision.upgrade_id in facts.prices:
            status = 'observed'
        return RouteEvaluation(facts.account_id, route.revision, status, decision, trace, facts.observed_at)
    upgrade = upgrades.by_id(choice.upgrade_id)
    if lane == 'workshop':
        decision = RerollDecision(facts.account_id, 'strategy', 'Follow assigned strategy', 'buy',
            upgrade.id, upgrade.name, upgrade.category, price_for(upgrade.id), wallet,
            facts.lifetime_coins, choice.reason)
    else:
        decision = BattleDecision(facts.account_id, 'battle', 'buy', upgrade.id, upgrade.name,
            upgrade.category, price_for(upgrade.id), wallet, choice.reason, target=choice.target)
    status = 'observed' if lane == 'battle' or facts.screen == 'workshop' else 'projected'
    return RouteEvaluation(facts.account_id, route.revision, status, decision, trace,
                           facts.observed_at, choice.pending)
