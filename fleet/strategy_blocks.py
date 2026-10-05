"""Bounded strategy programs, evaluated identically by workers and previews."""
from __future__ import annotations

import hashlib
import math
import operator
from dataclasses import dataclass
from typing import Any, Mapping

import builds
import upgrades
from workshop_unlocks import gate_for, path_to

MAX_BLOCKS = 80
MAX_DEPTH = 6

COMPARISONS = {'gte': operator.ge, 'lte': operator.le, 'gt': operator.gt, 'lt': operator.lt}
SYMBOLS = {'gte': '≥', 'lte': '≤', 'gt': '>', 'lt': '<'}


def relative_wave_limit(relative: Mapping[str, int], best: int | None) -> int:
    """The wave a relative condition compares against: a share of the best
    finished Tier 1 wave, clamped to [floor, cap]; unknown best → floor."""
    if best is None:
        return relative['floor']
    return min(relative['cap'], max(relative['floor'], best * relative['pct'] // 100))


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
            allowed = {'id', 'type', 'label'}
            if 'label' in block:
                label = block['label']
                if not isinstance(label, str) or not label.strip() or len(label) > 60:
                    raise ValueError('block label must be 1 to 60 characters')
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
                            'targets', 'level_caps', 'price_cap', 'wallet_share_pct',
                            'wallet_share_basis', 'cheaper_than_upgrade_ids', 'hold_until_capped', 'price_source',
                            'batch_size', 'max_price_premium_pct'}
                ids = block.get('upgrade_ids')
                if not isinstance(ids, (tuple, list)) or not ids or len(ids) > 30:
                    raise ValueError('pool requires 1 to 30 upgrades')
                ids = [uid(item) for item in ids]
                if len(set(ids)) != len(ids):
                    raise ValueError('duplicate pool upgrade')
                block['upgrade_ids'] = ids
                if block.get('selection', 'priority') not in {'priority', 'weighted', 'cheapest', 'value'}:
                    raise ValueError('unknown pool selection')
                if block.get('selection') == 'value' and lane != 'workshop':
                    raise ValueError('value selection is only available in the Workshop')
                if lane == 'workshop' and block.get('selection') == 'cheapest':
                    raise ValueError('cheapest selection requires Battle price evidence')
                if block.get('price_source', 'observed') not in {'observed', 'model'}:
                    raise ValueError('unknown pool price source')
                if block.get('price_source') == 'model':
                    from fleet.battle_prices import supported_ids
                    if lane != 'battle' or block.get('selection') != 'cheapest':
                        raise ValueError('modeled prices require a cheapest Battle pool')
                    if set(ids) - supported_ids():
                        raise ValueError('modeled pool requires a cash curve for every upgrade')
                if 'batch_size' in block or 'max_price_premium_pct' in block:
                    if lane != 'battle' or block.get('selection') != 'cheapest' or block.get('price_source') != 'model':
                        raise ValueError('batches require a modeled cheapest Battle pool')
                    number(block.get('batch_size', 1), 'batch size', 1, 5)
                    number(block.get('max_price_premium_pct', 0), 'price premium', 0, 25)
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
                if block.get('selection') == 'value':
                    if set(weights) != set(ids):
                        raise ValueError('value selection needs a weight for every upgrade')
                    if 'decay_pct' in block or 'weight_floor' in block:
                        raise ValueError('value selection does not use decay or weight floor')
                reference = block.get('reference_upgrade_id', 'priority')
                if reference != 'priority':
                    uid(reference)
                if 'reference_upgrade_id' in block and 'discount_pct' not in block:
                    raise ValueError('price reference requires a discount')
                if 'cheaper_than_upgrade_ids' in block:
                    references = block['cheaper_than_upgrade_ids']
                    if (not isinstance(references, (list, tuple)) or not 1 <= len(references) <= 30
                            or any(not isinstance(item, str) for item in references)
                            or len(set(references)) != len(references)):
                        raise ValueError('strict price references require distinct upgrades')
                    block['cheaper_than_upgrade_ids'] = [uid(item) for item in references]
                if 'wallet_share_basis' in block:
                    if block['wallet_share_basis'] not in ('total', 'spendable') or 'wallet_share_pct' not in block:
                        raise ValueError('wallet share basis requires a share and total or spendable')
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
                if 'hold_until_capped' in block:
                    if (lane != 'battle' or block['hold_until_capped'] is not True
                            or set(caps) != set(ids)
                            or any('per_level_of' in cap for cap in caps.values())):
                        raise ValueError('hold_until_capped requires fixed Battle caps for every pool upgrade')
                if 'price_cap' in block:
                    number(block['price_cap'], 'price_cap', 1, 1000000000000)
                if 'wallet_share_pct' in block:
                    number(block['wallet_share_pct'], 'wallet_share_pct', 1, 100)
            elif kind == 'unlock':
                allowed |= {'upgrade_ids', 'max_price', 'hold'}
                if lane != 'workshop':
                    raise ValueError('unlock blocks are only available in the Workshop')
                ids = block.get('upgrade_ids')
                if not isinstance(ids, (tuple, list)) or not ids or len(ids) > 30:
                    raise ValueError('unlock block requires 1 to 30 skills')
                ids = [uid(item) for item in ids]
                if len(set(ids)) != len(ids):
                    raise ValueError('duplicate unlock skill')
                if any(upgrades.by_id(item).unlock for item in ids):
                    raise ValueError('name the skill to unlock, not its unlock tile')
                block['upgrade_ids'] = ids
                if 'max_price' in block:
                    number(block['max_price'], 'max_price', 1, 1000000000000)
                if 'hold' in block and type(block['hold']) is not bool:
                    raise ValueError('hold must be true or false')
            elif kind == 'condition':
                allowed |= {'field', 'op', 'value', 'relative', 'then', 'else', 'upgrade_id'}
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
                if 'relative' in block:
                    if 'value' in block:
                        raise ValueError('a condition has a value or a relative threshold, not both')
                    if field != 'wave':
                        raise ValueError('relative thresholds are only for the current wave')
                    relative = block['relative']
                    if not isinstance(relative, dict) or set(relative) != {'pct', 'floor', 'cap'}:
                        raise ValueError('a relative threshold needs exactly pct, floor and cap')
                    number(relative['pct'], 'relative pct', 1, 1000)
                    low = number(relative['floor'], 'relative floor', 1, 100000)
                    number(relative['cap'], 'relative cap', low, 100000)
                    block['relative'] = dict(relative)
                else:
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
                if any(key in block['goal'][0] for key in ('discount_pct', 'cheaper_than_upgrade_ids')):
                    raise ValueError('a saving goal cannot be a price-comparison pool')
                if block['goal'][0].get('selection') == 'value':
                    raise ValueError('a saving goal cannot be a value pool')
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


def native_template_program(policy: str, lane: str) -> tuple[dict[str, Any], ...]:
    phases = ('starter', 'economy', 'objectives', 'fallback') if lane == 'workshop' else ('battle',)
    return validate_program([{'id': f'{policy}.{phase}', 'type': 'native',
                              'policy': policy, 'phase': phase} for phase in phases], lane)


_ECONOMY_WEIGHTS = {'unlock_cash_bonuses': 100, 'cash_per_wave': 200, 'unlock_coin_bonuses': 80,
                    'coins_per_kill_bonus': 150, 'cash_bonus': 60}
_FILLER_CAPS = {'cash_per_wave': 5, 'coins_per_kill_bonus': 5, 'cash_bonus': 5, 'damage': 3, 'attack_speed': 3}
# Blender v2: coin weights for value-per-coin buying (sum 100) and stop targets.
_BLENDER_WEIGHTS = {
    'coins_per_kill_bonus': 18, 'defense_percent': 12, 'attack_speed': 10, 'health': 9,
    'knockback_chance': 6, 'thorns': 6, 'damage': 4, 'lifesteal': 4, 'orb_speed': 4,
    'free_utility_upgrade': 3, 'free_defense_upgrade': 3, 'cash_bonus': 3, 'orbs': 3,
    'knockback_force': 3, 'free_attack_upgrade': 2, 'coins_per_wave': 2, 'cash_per_wave': 2,
    'critical_chance': 2, 'multishot_targets': 2, 'multishot_chance': 1, 'critical_factor': 1,
}
_BLENDER_TARGETS = {'thorns': 51, 'lifesteal': 3.5, 'orbs': 3, 'multishot_targets': 5}
# Every gated pool skill, plus Recovery Packages (whose path buys Interest).
_BLENDER_UNLOCKS = (*(uid for uid in _BLENDER_WEIGHTS if gate_for(uid) is not None),
                    'max_recovery', 'package_chance')
_OLDER_BUILTIN_WORKSHOP_IDS = {
    'opening': ('opening.starter', 'opening.economy', 'opening.objectives', 'opening.filler'),
    'turtle': ('turtle.economy', 'turtle.attack', 'turtle.objectives',
               'turtle.unlock_filler', 'turtle.cheap_defense', 'turtle.filler'),
}


def _pool(identity: str, ids: list[str], **extra: Any) -> dict[str, Any]:
    return {'id': identity, 'type': 'pool', 'upgrade_ids': ids, 'selection': 'priority', **extra}


def _capped(base: int) -> dict[str, int]:
    return {'base': base}


def _legacy_saving_filler(block: Mapping[str, Any]) -> bool:
    """Only untouched pinned built-in filler blocks lose their old share cap."""
    identity = block['id']
    if identity not in {'opening.filler', 'turtle.filler'}:
        return False
    policy = identity.split('.')[0]
    expected = {'id': identity, 'type': 'while_saving', 'label': 'Filler while saving',
                'blocks': [_pool(f'{policy}.filler.pool', list(_FILLER_CAPS), wallet_share_pct=20,
                                 level_caps={uid: _capped(cap) for uid, cap in _FILLER_CAPS.items()})]}
    return block == expected or block == swap_kill_bonus((expected,))[0]


def _workshop_template(policy: str) -> list[dict[str, Any]]:
    economy = {'id': f'{policy}.economy', 'type': 'budget', 'label': 'Early economy', 'metric': 'utility_spent',
               'target': 350, 'ceiling': 400,
               'blocks': [{'id': f'{policy}.economy.goal', 'type': 'save_for', 'label': 'Save for utility', 'goal': [
                   _pool(f'{policy}.economy.pool', list(_ECONOMY_WEIGHTS), selection='weighted', weights=_ECONOMY_WEIGHTS,
                         level_caps={'cash_per_wave': _capped(2), 'coins_per_kill_bonus': _capped(3)})]}]}
    fallback = _pool(f'{policy}.filler', list(_FILLER_CAPS), label='Affordable fallback',
                     level_caps={uid: _capped(cap) for uid, cap in _FILLER_CAPS.items()})
    if policy == 'opening':
        attack_cap = {'base': 2, 'per_level_of': 'coins_per_wave'}
        return [
            _pool('opening.starter', ['damage', 'attack_speed', 'health', 'unlock_defense_upgrades', 'defense_absolute'],
                  label='Survival starter', price_cap=75, max_purchases=1),
            economy,
            _pool('opening.objectives',
                ['damage', 'attack_speed', 'unlock_defense_upgrades', 'unlock_cash_bonuses', 'unlock_coin_bonuses',
                 'coins_per_wave', 'defense_absolute', 'unlock_thorns', 'thorns'],
                label='Objectives',
                level_caps={'damage': attack_cap, 'attack_speed': attack_cap,
                            'coins_per_wave': _capped(3), 'defense_absolute': _capped(2)},
                targets={'thorns': 51}),
            fallback,
        ]
    return [
        economy,
        # Defense only keeps the tower alive; until Thorns is unlocked Damage
        # is the only way to kill, so a minimum attack comes before defense.
        _pool('turtle.attack', ['damage', 'attack_speed'], label='Minimum attack',
              level_caps={'damage': _capped(3), 'attack_speed': _capped(3)}),
        _pool('turtle.objectives',
            ['unlock_defense_upgrades', 'defense_absolute', 'unlock_thorns', 'thorns',
             'cash_bonus', 'coins_per_kill_bonus', 'health'],
            label='Objectives', level_caps={'defense_absolute': _capped(5)},
            targets={'thorns': 51}),
        fallback,
    ]


def _blender_workshop_template(policy: str) -> dict[str, Any]:
    return {'id': f'{policy}.blender.wave450', 'type': 'condition',
            'label': 'Blender after best wave 450',
            'field': 'best_tier_1_wave', 'op': 'gte', 'value': 450,
            'then': [
                {'id': f'{policy}.blender.unlocks', 'type': 'unlock',
                 'label': 'Unlock missing skills', 'upgrade_ids': list(_BLENDER_UNLOCKS),
                 'max_price': 20000, 'hold': True},
                {'id': f'{policy}.blender.value', 'type': 'pool', 'label': 'Blender value per coin',
                 'upgrade_ids': list(_BLENDER_WEIGHTS), 'selection': 'value',
                 'weights': dict(_BLENDER_WEIGHTS), 'targets': dict(_BLENDER_TARGETS)},
                {'id': f'{policy}.blender.wait', 'type': 'wait',
                 'label': 'Wait for an affordable Blender upgrade'},
            ], 'else': _workshop_template(policy)}


def _older_builtin_workshop_shape(program: tuple[dict[str, Any], ...], policy: str) -> bool:
    """Recognize pinned copies from before the affordable-priority template."""
    return (tuple(block.get('id') for block in program) == _OLDER_BUILTIN_WORKSHOP_IDS[policy]
            and next(block for block in program if block['id'] == f'{policy}.objectives')['type'] == 'save_for')


def _battle_template(policy: str) -> list[dict[str, Any]]:
    economy = {'cash_per_wave': 10, 'coins_per_kill_bonus': 1.25, 'cash_bonus': 1.25}
    if policy == 'opening':
        starters = {'defense_absolute': 10, 'thorns': 11, 'damage': 12, 'attack_speed': 1.10, 'health': 20}
        return [
            _pool('opening.battle.starters', list(starters), label='Survival starters', targets=starters),
            _pool('opening.battle.cheap_defense', ['defense_percent'], label='Cheap Defense %',
                  targets={'defense_percent': 51}, wallet_share_pct=20),
            _pool('opening.battle.priorities', [*economy, 'defense_absolute', 'thorns', 'health',
                  'coins_per_wave', 'damage', 'attack_speed'],
                  label='Battle priorities', targets={**economy, 'thorns': 51, 'coins_per_wave': 10}),
        ]
    attack_floor = {'damage': 25, 'attack_speed': 1.4}

    def relative(pct: int, floor: int, cap: int) -> dict[str, int]:
        return {'pct': pct, 'floor': floor, 'cap': cap}

    thorns = {'id': 'turtle.battle.wave40', 'type': 'condition', 'label': 'Thorns steps by best wave',
              'field': 'wave', 'op': 'lte', 'relative': relative(40, 5, 40),
              'then': [_pool('turtle.battle.thorns11', ['thorns'], targets={'thorns': 11})],
              'else': [{'id': 'turtle.battle.wave80', 'type': 'condition', 'field': 'wave', 'op': 'lte',
                        'relative': relative(80, 8, 80),
                        'then': [_pool('turtle.battle.thorns21', ['thorns'], targets={'thorns': 21})],
                        'else': [{'id': 'turtle.battle.wave160', 'type': 'condition', 'field': 'wave', 'op': 'lte',
                                  'relative': relative(110, 12, 160),
                                  'then': [_pool('turtle.battle.thorns34', ['thorns'], targets={'thorns': 34})],
                                  'else': [_pool('turtle.battle.thorns51', ['thorns'], targets={'thorns': 51})]}]}]}
    return [
        {'id': 'turtle.battle.emergency', 'type': 'condition', 'label': 'Emergency defense',
         'field': 'def_abs_coverage', 'op': 'lt', 'value': 1.2,
         'then': [{'id': 'turtle.battle.emergency.paths', 'type': 'fallback', 'blocks': [
             _pool('turtle.battle.emergency.buy', ['defense_absolute']),
             {'id': 'turtle.battle.emergency.wait', 'type': 'wait',
              'label': 'Defense Absolute needed but not purchasable'}]}], 'else': []},
        {'id': 'turtle.battle.ahead', 'type': 'condition', 'label': 'Keep Def Abs ahead',
         'field': 'def_abs_coverage', 'op': 'lt', 'value': 2,
         'then': [_pool('turtle.battle.ahead.buy', ['defense_absolute'], wallet_share_pct=30)], 'else': []},
        # Without an attack floor Survival spends every spare cash on Health,
        # and an account still saving for Thorns cannot kill anything.
        _pool('turtle.battle.attack', list(attack_floor), label='Attack floor', targets=attack_floor),
        {'id': 'turtle.battle.early', 'type': 'condition', 'label': 'Early economy',
         'field': 'wave', 'op': 'lte', 'relative': relative(50, 5, 20),
         'then': [_pool('turtle.battle.economy', list(economy), targets=economy)], 'else': []},
        thorns,
        _pool('turtle.battle.cheap_defense', ['defense_percent'], label='Cheap Defense %',
              targets={'defense_percent': 53}, wallet_share_pct=10),
        _pool('turtle.battle.survival', ['health', 'health_regen', 'damage', 'attack_speed'],
              label='Survival'),
    ]


def template_program(policy: str, lane: str) -> tuple[dict[str, Any], ...]:
    """Built-in strategies as ordinary, readable blocks."""
    if policy not in {'opening', 'turtle'}:
        raise ValueError('unknown template policy')
    program = ([_blender_workshop_template(policy)] if lane == 'workshop'
               else _battle_template(policy))
    return validate_program(program, lane)


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


def uses_modeled_prices(program: tuple[dict[str, Any], ...]) -> bool:
    return any(block.get('price_source') == 'model' or any(
        uses_modeled_prices(tuple(children)) for children in child_lists(block)) for block in program)


def batch_size_for(program: tuple[dict[str, Any], ...], rule_id: str) -> int:
    for block in program:
        if block['id'] == rule_id:
            return block.get('batch_size', 1)
        for children in child_lists(block):
            size = batch_size_for(tuple(children), rule_id)
            if size > 1:
                return size
    return 1


def program_upgrade_ids(program: tuple[dict[str, Any], ...]) -> tuple[str, ...]:
    """Rows the observer may inspect even before a block can recommend a buy."""
    result: list[str] = []
    for block in program:
        kind = block["type"]
        if kind == "buy":
            result.append(block["upgrade_id"])
        elif kind == "pool":
            result.extend(block["upgrade_ids"])
            result.extend(block.get("cheaper_than_upgrade_ids", ()))
            reference = block.get("reference_upgrade_id")
            if reference and reference != "priority":
                result.append(reference)
        elif kind == "unlock":
            result.extend(block["upgrade_ids"])
            for skill in block["upgrade_ids"]:
                result.extend(group.executable_upgrade_id for group in path_to(skill, set())
                              if group.executable_upgrade_id)
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
    if KILL_BONUS in result:
        result.append(PER_WAVE)  # its stand-in while the best wave is low
    return tuple(dict.fromkeys(result))


KILL_BONUS, PER_WAVE = 'coins_per_kill_bonus', 'coins_per_wave'
# A Coins / Kill target is a multiplier (x1.25) and means nothing on a
# coins-per-wave value, so the stand-in takes the templates' own goal.
PER_WAVE_STAND_IN_TARGET = 10


def kill_bonus_hold(route: Any, best_tier_1_wave: int | None) -> int | None:
    """The best wave Coins / Kill Bonus waits for, or None once it may be bought."""
    rules = getattr(route, 'rules', None)
    threshold = rules.coins.kill_bonus_min_best_wave if rules is not None else 0
    if threshold <= 0 or (best_tier_1_wave is not None and best_tier_1_wave >= threshold):
        return None
    return threshold


def _swap_ids(ids: list[str]) -> list[str]:
    """Coins / Wave in Coins / Kill's place, listed once at the higher rank."""
    return list(dict.fromkeys(PER_WAVE if uid == KILL_BONUS else uid for uid in ids))


def swap_kill_bonus(program: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """The program with Coins / Wave bought wherever Coins / Kill Bonus would be.

    A pool already listing Coins / Wave keeps that entry's own settings; otherwise
    Coins / Wave inherits Coins / Kill's weight and level cap, and a target
    becomes PER_WAVE_STAND_IN_TARGET. Conditions still read the real value.
    """
    result = []
    for block in program:
        block = dict(block)
        if block['type'] == 'pool' and KILL_BONUS in block['upgrade_ids']:
            listed = PER_WAVE in block['upgrade_ids']
            block['upgrade_ids'] = _swap_ids(block['upgrade_ids'])
            for key in ('weights', 'level_caps', 'targets'):
                if key not in block:
                    continue
                settings = dict(block[key])
                own = settings.pop(KILL_BONUS, None)
                if own is not None and not listed:
                    settings[PER_WAVE] = PER_WAVE_STAND_IN_TARGET if key == 'targets' else own
                block[key] = settings
        for key in ('upgrade_id', 'reference_upgrade_id'):
            if block.get(key) == KILL_BONUS and block['type'] != 'condition':
                block[key] = PER_WAVE
        if 'cheaper_than_upgrade_ids' in block:
            block['cheaper_than_upgrade_ids'] = _swap_ids(block['cheaper_than_upgrade_ids'])
        for key in ('then', 'else', 'blocks', 'goal'):
            if isinstance(block.get(key), (list, tuple)):
                block[key] = list(swap_kill_bonus(tuple(block[key])))
        result.append(block)
    return tuple(result)


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
    price_source: str = "observed"


def evaluate_program(route: Any, facts: Any, pending: Any, lane: str) -> Any:
    """Evaluate one bounded program. No writes, devices, clocks or random state."""
    from fleet.build_route_eval import (BattleDecision, DecisionTrace, PendingDecision,
                                        RouteEvaluation)
    from fleet.reroll_planner import (RerollDecision, RerollFacts, _ban_closure, _owned_groups,
                                      choose_native_phase, native_phase_progress)
    from workshop_unlocks import available
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
    if lane == 'workshop' and facts.best_tier_1_wave is not None and facts.best_tier_1_wave >= 450:
        # Existing assignments pin their block snapshots, including the older
        # save_for layout. Recognize the two built-in shapes at this wave.
        for policy in ('opening', 'turtle'):
            if (program == validate_program(_workshop_template(policy), 'workshop')
                    or program == native_template_program(policy, 'workshop')
                    or _older_builtin_workshop_shape(program, policy)):
                program = template_program(policy, 'workshop')
                break
    kill_bonus_wave = kill_bonus_hold(route, facts.best_tier_1_wave)
    if kill_bonus_wave is not None:
        program = swap_kill_bonus(program)
    from fleet.coin_share import spendable_wallet, workshop_ceiling, workshop_limit_pct
    jar = getattr(facts, 'lab_coin_jar', 0) if lane == 'workshop' else 0
    ceiling = workshop_ceiling(route, wallet, jar) if lane == 'workshop' else wallet
    excluded = _ban_closure(route.workshop.banned_upgrade_ids)
    workshop_owned = _owned_groups(facts.purchases, facts.values)
    counts = facts.confirmed_purchases if lane == 'workshop' else facts.run_purchases
    rejected: list[str] = []
    native_bans = route.workshop.banned_upgrade_ids
    if kill_bonus_wave is not None:
        native_bans = native_bans | {KILL_BONUS}
        best = 'unknown' if facts.best_tier_1_wave is None else facts.best_tier_1_wave
        rejected.append(f'Coins / Wave replaces Coins / Kill Bonus until best Tier 1 wave '
                        f'{kill_bonus_wave} (best {best})')

    def model_quote(uid: str) -> Mapping[str, Any]:
        quote = facts.battle_price_quotes.get(uid, {})
        if (quote.get('account_id') == facts.account_id and quote.get('run_id') == facts.run_id
                and quote.get('source') == 'model'):
            return quote
        return {}

    def price_for(uid: str, *, reference: bool = False, source: str = 'observed') -> int | None:
        if source == 'model' and (quote := model_quote(uid)):
            price = quote.get('price')
            return price if quote.get('status') == 'available' and type(price) is int and price > 0 else None
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

    def owned_unlock(uid: str) -> bool:
        # A bought unlock leaves no tile to price: nothing to read or compare.
        return lane == 'workshop' and upgrades.by_id(uid).unlock and uid in workshop_owned

    # Workshop candidates turned away only for having no known price. If the
    # program then decides nothing, these are observed rather than waited on:
    # waiting cannot end, because only a Workshop visit reads a price.
    unpriced: list[str] = []

    def eligible(uid: str, *, ignore_funds: bool = False, source: str = "observed") -> bool:
        if uid == KILL_BONUS and kill_bonus_wave is not None:
            rejected.append(f'{uid}: held until best Tier 1 wave {kill_bonus_wave}')
            return False
        if uid in excluded:
            rejected.append(f'{uid}: blocked by Never Buy')
            return False
        if lane == 'workshop' and uid in facts.maxed_ids:
            # A maxed row shows no price: never wait on or read one.
            rejected.append(f'{uid}: maxed')
            return False
        if lane == 'workshop' and not available(uid, workshop_owned):
            rejected.append(f'{uid}: Workshop upgrade is not unlocked or the next unlock')
            return False
        price = price_for(uid, reference=ignore_funds, source=source)
        if price is None and lane == 'workshop' and not owned_unlock(uid):
            unpriced.append(uid)
        if price is None or (not ignore_funds and price > ceiling):
            return False
        if budget_room is not None and price > budget_room:
            rejected.append(f'{uid}: exceeds budget ceiling')
            return False
        if lane == 'workshop':
            gate = builds.prerequisites().get(uid)
            if gate and gate not in workshop_owned and facts.purchases.get(gate, 0) <= 0:
                return False
        return True

    native_intents: dict[str, Any] = {}
    budget_room: int | None = None
    saving: _Choice | None = None
    def native_facts(policy: str) -> RerollFacts:
        return RerollFacts(
            facts.account_id, facts.best_tier_1_wave, facts.purchases, facts.values,
            spendable_wallet(wallet, jar), facts.lifetime_coins, facts.prices,
            spend_fraction=workshop_limit_pct(route) / 100,
            variant=facts.variant, utility_spent_coins=facts.utility_spent_coins,
            policy=policy)

    def native_decision(policy: str, phase: str) -> Any:
        return choose_native_phase(native_facts(policy), phase,
            banned_upgrade_ids=native_bans,
            reference=native_intents.get(policy))

    def priority_reference(scopes: tuple[Any, ...], current: str) -> str | None:
        for items in scopes:
            for item in items:
                if item['id'] == current:
                    continue
                if item['type'] == 'buy':
                    if owned_unlock(item['upgrade_id']):
                        continue
                    return item['upgrade_id']
                if item['type'] == 'pool':
                    unowned = [uid for uid in item['upgrade_ids'] if not owned_unlock(uid)]
                    if not unowned:
                        continue
                    return unowned[0]
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
        if evidence.get('source') == 'catalog_estimate' and upgrades.by_id(uid).unlock:
            # An unlock has a single price, so the tracked catalog figure is
            # exact - no level to infer, nothing a Workshop read would add.
            return True
        timestamp = evidence.get('observed_at')
        # An observed Workshop quote stays current only while the price memory
        # proves its account, purchase offset and discount signature unchanged.
        return (evidence.get('source') == 'observed' and type(timestamp) in {int, float}
                and math.isfinite(timestamp) and 0 <= timestamp <= facts.now)

    def observe_prices(identity: str, ids: list[str]) -> _Choice | None:
        if lane == 'battle':
            # A row unread for 60s is unverified, not unaffordable: the bot sat
            # on another tab. Skipping it would hand every later block the win.
            # A row locked in the Workshop is the exception: it cannot unlock
            # mid-run, and the autopilot drops it when the run changes.
            stale = [uid for uid in dict.fromkeys(ids) if uid not in excluded
                     and facts.upgrade_rows.get(uid, {}).get('status') not in {'locked', 'maxed'} and not (
                isinstance(seen := facts.upgrade_rows.get(uid, {}).get('observed_at'), (float, int))
                and 0 <= facts.now - seen <= 60)]
            return (_Choice(identity, stale[0], 'Observe stale battle rows before later blocks',
                            observation_ids=tuple(stale)) if stale else None)
        if lane != 'workshop':
            return None
        needed = []
        for uid in dict.fromkeys(ids):
            gate = builds.prerequisites().get(uid)
            if (uid in excluded or uid in facts.maxed_ids or not available(uid, workshop_owned)
                    or (gate and gate not in workshop_owned and facts.purchases.get(gate, 0) <= 0)):
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
        if kill_bonus_wave is not None:
            order = _swap_ids([rule.upgrade_id for rule in rules])
            by_id = {rule.upgrade_id: rule for rule in rules}
            rules = tuple(by_id[uid] for uid in order)
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
            rows = facts.upgrade_rows  # def_abs_coverage is Battle-only
            # No Defense Absolute to buy yet, so the emergency check does not apply.
            if rows.get('defense_absolute', {}).get('status') == 'locked':
                return math.inf
            absolute = upgrade_value('defense_absolute')
            locked_percent = rows.get('defense_percent', {}).get('status') == 'locked'
            percent = 0.0 if locked_percent else upgrade_value('defense_percent')
            damage = facts.enemy_damage
            if absolute is None or percent is None or damage is None:
                return None
            remaining = damage * max(0.0, 1.0 - percent / 100.0)
            return math.inf if remaining <= 0 else absolute / remaining
        return getattr(facts, field)

    def pool_candidates(block: Mapping[str, Any], identity: str, reference_price: int | None,
                        *, ignore_funds: bool = False) -> dict[str, float]:
        candidates: dict[str, float] = {}
        source = block.get('price_source', 'observed')
        for uid in block['upgrade_ids']:
            count = (counts or {}).get(uid, 0)
            if 'max_purchases' in block and count >= block['max_purchases']:
                rejected.append(f'{identity}: {uid} max purchases reached')
                continue
            cap = block.get('level_caps', {}).get(uid)
            if cap is not None:
                limit = cap['base'] + (cap.get('step', 1) * (counts or {}).get(cap['per_level_of'], 0)
                                       if 'per_level_of' in cap else 0)
                if count >= limit:
                    rejected.append(f'{identity}: {uid} level cap reached')
                    continue
            target = block.get('targets', {}).get(uid)
            if target is not None:
                value = upgrade_value(uid)
                if value is None and lane == 'battle':
                    rejected.append(f'{identity}: {uid} target value unknown')
                    continue
                if value is None:
                    rejected.append(f'{identity}: {uid} target value unverified; treated as not reached')
                elif upgrades.target_reached(uid, value, float(target)):
                    rejected.append(f'{identity}: {uid} target reached')
                    continue
            if not eligible(uid, ignore_funds=ignore_funds, source=source):
                continue
            price = price_for(uid, reference=ignore_funds, source=source)
            if 'price_cap' in block and price > block['price_cap']:
                rejected.append(f'{identity}: {uid} over price cap')
                continue
            # Wallet share is a funds limit, so a saving goal may exceed it.
            share_wallet = (min(spendable_wallet(wallet, jar), ceiling)
                            if block.get('wallet_share_basis') == 'spendable' else wallet)
            if (not ignore_funds and 'wallet_share_pct' in block
                    and price * 100 > share_wallet * block['wallet_share_pct']):
                rejected.append(f'{identity}: {uid} over wallet share')
                continue
            if any(key in block for key in ('discount_pct', 'cheaper_than_upgrade_ids')) and not observed_quote(uid):
                continue
            comparisons = [price_for(ref, reference=True) for ref in block.get('cheaper_than_upgrade_ids', ())]
            if any(reference is None or price >= reference for reference in comparisons):
                rejected.append(f'{identity}: {uid} not strictly cheaper than every reference')
                continue
            if reference_price is not None and price * 100 > reference_price * (100 - block['discount_pct']):
                rejected.append(f'{identity}: {uid} over discount limit')
                continue
            base = block.get('weights', {}).get(uid, 1)
            candidates[uid] = max(block.get('weight_floor', 1),
                                  base * ((100 - block.get('decay_pct', 0)) / 100) ** count)
        return candidates

    def top_pick(goal: Mapping[str, Any]) -> tuple[str, int] | None:
        """First goal item passing every filter except wallet affordability."""
        if goal['type'] == 'buy':
            uid = goal['upgrade_id']
            return (uid, price_for(uid, reference=True)) if eligible(uid, ignore_funds=True) else None
        needs_counts = 'max_purchases' in goal or 'level_caps' in goal or goal.get('decay_pct', 0) > 0
        if needs_counts and counts is None:
            rejected.append(f"{goal['id']}: confirmed purchase counts unavailable")
            return None
        uid = next(iter(pool_candidates(goal, goal['id'], None, ignore_funds=True)), None)
        return (uid, price_for(uid, reference=True)) if uid else None

    def evaluate(items: Any, ancestors: tuple[Any, ...] = ()) -> _Choice | None:
        nonlocal waiting_native, budget_room, saving
        for index, block in enumerate(items):
            kind, identity = block['type'], block['id']
            if kind == 'wait':
                # Items passed over only for an unread price may have come
                # first; holding on missing evidence never reads it.
                if unpriced and (observation := observe_prices(identity, unpriced)):
                    return observation
                return _Choice(identity, reason='Wait block reached', wait=True)
            if kind == 'condition':
                value = condition_value(block)
                if value is None:
                    rejected.append(f'{identity}: condition evidence unknown')
                    return _Choice(identity, reason='Condition evidence unknown; decision paused', wait=True)
                if 'relative' in block:
                    relative = block['relative']
                    threshold = relative_wave_limit(relative, facts.best_tier_1_wave)
                    matched = COMPARISONS[block['op']](value, threshold)
                    basis = (f"best wave unknown → floor {relative['floor']}" if facts.best_tier_1_wave is None
                             else f"{relative['pct']}% of best {facts.best_tier_1_wave}, "
                                  f"floor {relative['floor']}, cap {relative['cap']}")
                    rejected.append(f"{identity}: wave {value} {SYMBOLS[block['op']]} {threshold} "
                                    f"→ {'then' if matched else 'else'} ({basis})")
                else:
                    matched = COMPARISONS[block['op']](value, block['value'])
                choice = evaluate(block['then'] if matched else block['else'], (items, *ancestors))
                if choice is not None:
                    return choice
            elif kind == 'save_for':
                goal = block['goal'][0]
                if (lane == 'workshop' and goal['type'] == 'pool'
                        and goal.get('selection', 'priority') == 'priority'
                        and len(goal['upgrade_ids']) > 1):
                    # Saved strategy versions still wrap their ordered
                    # Workshop lists in save_for. Try every affordable item
                    # before holding coins for the list's top item.
                    choice = evaluate(block['goal'], (items, *ancestors))
                    if choice is not None:
                        return choice
                if saving is not None:
                    rejected.append(f'{identity}: another goal is already saving')
                    continue
                weighted = goal['type'] == 'pool' and goal.get('selection', 'priority') == 'weighted'
                if weighted:
                    choice = evaluate(block['goal'], (items, *ancestors))
                    if choice is not None:
                        return choice
                before = len(unpriced)
                pick = top_pick(goal)
                # An unread price is neither a goal met nor one to pass over:
                # read the goal items ranked above the pick before any later
                # block spends the coins this goal is meant to keep.
                ids = [goal['upgrade_id']] if goal['type'] == 'buy' else list(goal['upgrade_ids'])
                above = ids[:ids.index(pick[0])] if pick else ids
                if lane == 'workshop' and (observation := observe_prices(
                        identity, [uid for uid in unpriced[before:] if uid in above])):
                    return observation
                if pick is None:
                    continue
                uid, price = pick
                # A priority goal acts on its top pick only: never fall
                # through to a lower, merely affordable item.
                share = goal.get('wallet_share_pct')
                if (not weighted and price_for(uid) is not None and price <= ceiling
                        and (share is None or price * 100 <= wallet * share)):
                    return _Choice(goal['id'], uid, f'Save for goal: buy {upgrades.by_id(uid).name}',
                                   target=goal.get('targets', {}).get(uid))
                name = upgrades.by_id(uid).name
                saving = _Choice(identity, uid, f'Saving for {name} ({wallet}/{price} coins)',
                                 wait=True, save_price=price)
            elif kind == 'unlock':
                steps: dict[str, tuple[Any, int]] = {}
                for skill in block['upgrade_ids']:
                    if skill in excluded:
                        rejected.append(f'{skill}: blocked by Never Buy')
                        continue
                    path = path_to(skill, workshop_owned)
                    if not path:
                        continue
                    group = path[0]
                    tile = group.executable_upgrade_id
                    if tile is None:
                        rejected.append(f'{identity}: manual unlock needed: {group.name} (for {skill})')
                        continue
                    if tile in excluded:
                        rejected.append(f'{tile}: blocked by Never Buy')
                        continue
                    if not available(tile, workshop_owned):
                        continue
                    # An unlock has one fixed price; the catalog figure ranks it until read.
                    price = price_for(tile)
                    price = group.cost if price is None else price
                    if price is None:
                        continue
                    if 'max_price' in block and price > block['max_price']:
                        rejected.append(f'{identity}: {group.name} over unlock price limit')
                        continue
                    # Never save for a step a budget block would forbid buying.
                    if budget_room is not None and price > budget_room:
                        rejected.append(f'{identity}: {group.name} exceeds budget ceiling')
                        continue
                    steps.setdefault(tile, (group, price))
                if not steps:
                    continue
                tab_order = {'ATTACK': 0, 'DEFENSE': 1, 'UTILITY': 2}
                ranked = sorted(steps, key=lambda tile: (steps[tile][1], tab_order[steps[tile][0].category]))
                for tile in ranked:
                    group, price = steps[tile]
                    if price > ceiling:
                        rejected.append(f'{identity}: {group.name} over spend ceiling')
                        continue
                    if price_for(tile) is None:
                        if observation := observe_prices(identity, [tile]):
                            return observation
                        continue
                    return _Choice(identity, tile, f'Unlock {group.name} for skills in this strategy')
                group, price = steps[ranked[0]]
                goal = _Choice(identity, ranked[0], f'Saving for {group.name} ({wallet}/{price} coins)',
                               wait=True, save_price=price)
                if block.get('hold', True):
                    # An earlier saving goal stays the published target.
                    return saving or goal
                if saving is None:
                    saving = goal
            elif kind == 'while_saving':
                if lane == 'workshop' and _legacy_saving_filler(block):
                    # Pinned built-in strategies carry the old 20%-wallet
                    # fallback. Keep the same ordered rows and level caps,
                    # but let them use every coin left after earlier blocks.
                    fallback = [{key: value for key, value in child.items()
                                 if key != 'wallet_share_pct'} for child in block['blocks']]
                    choice = evaluate(fallback, (items, *ancestors))
                    if choice is not None:
                        return choice
                    continue
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
                        policy, decision, banned_upgrade_ids=native_bans)
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
                if lane == 'battle' and (observation := observe_prices(identity, [block['upgrade_id']])):
                    return observation
            elif kind == 'pool':
                source = block.get('price_source', 'observed')
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
                references = block.get('cheaper_than_upgrade_ids', ())
                if lane == 'workshop' and any(not available(ref, workshop_owned) for ref in references):
                    rejected.append(f'{identity}: strict price reference is locked')
                    continue
                if any(not observed_quote(ref) for ref in references):
                    rejected.append(f'{identity}: strict reference price unverified')
                    if observation := observe_prices(identity, [*references, *block['upgrade_ids']]):
                        return observation
                    continue
                before_unpriced = len(unpriced)
                candidates = pool_candidates(block, identity, reference_price)
                if lane == 'workshop' and block.get('selection', 'priority') == 'priority':
                    ordered = list(block['upgrade_ids'])
                    if candidates:
                        ordered = ordered[:ordered.index(next(iter(candidates)))]
                    if observation := observe_prices(
                            identity, [uid for uid in ordered
                                       if uid in unpriced[before_unpriced:] and price_for(uid) is None]):
                        return observation
                if lane == 'workshop' and block.get('selection') == 'value':
                    # The cheapest value may be an item whose price is unread.
                    if observation := observe_prices(
                            identity, [uid for uid in unpriced[before_unpriced:]
                                       if uid in block['upgrade_ids'] and price_for(uid) is None]):
                        return observation
                if lane == 'battle':
                    # A priority pick only needs the stale rows ranked above it;
                    # A cheapest pick can act on verified affordable rows now;
                    # it discovers unread rows when none can be bought.
                    ids = list(block['upgrade_ids'])
                    if block.get('hold_until_capped'):
                        ids = [uid for uid in ids if (counts or {}).get(uid, 0) < block['level_caps'][uid]['base']]
                    if source == 'model':
                        ids = [uid for uid in ids if not model_quote(uid)]
                    elif candidates and block.get('selection') == 'cheapest':
                        ids = []
                    if candidates and block.get('selection', 'priority') == 'priority':
                        ids = ids[:ids.index(next(iter(candidates)))]
                    if observation := observe_prices(identity, ids):
                        return observation
                if not candidates:
                    if block.get('hold_until_capped'):
                        incomplete = any(
                            uid not in excluded
                            and facts.upgrade_rows.get(uid, {}).get('status') not in {'locked', 'maxed'}
                            and (counts or {}).get(uid, 0) < block['level_caps'][uid]['base']
                            and price_for(uid, reference=True) is not None
                            for uid in block['upgrade_ids'])
                        if incomplete:
                            return _Choice(identity, reason='Waiting for available pool upgrades to reach their purchase caps', wait=True)
                    if any(key in block for key in ('discount_pct', 'cheaper_than_upgrade_ids')):
                        observation = observe_prices(identity, block['upgrade_ids'])
                        if observation is not None:
                            return observation
                    continue
                chosen = next(iter(candidates))
                selected = None
                if block.get('selection', 'priority') == 'cheapest':
                    chosen = min(candidates, key=lambda uid: (price_for(uid, source=source), block['upgrade_ids'].index(uid)))
                    if source == 'model' and (quote := model_quote(chosen)) and not quote.get('verified'):
                        return _Choice(identity, chosen, 'Reconcile the cheapest candidate after a wave change',
                                       observation_ids=(chosen,), price_source='model')
                    if (source == 'model' and facts.battle_batch_rule_id == identity
                            and 0 < facts.battle_batch_purchases < block.get('batch_size', 1)):
                        local_ceiling = price_for(chosen, source=source) * (100 + block.get('max_price_premium_pct', 0))
                        local = [uid for uid in candidates if uid in facts.visible_upgrade_ids
                                 and (local_quote := model_quote(uid)) and local_quote.get('verified')
                                 and price_for(uid, source=source) * 100 <= local_ceiling]
                        if local:
                            chosen = min(local, key=lambda uid: (price_for(uid, source=source), block['upgrade_ids'].index(uid)))
                elif block.get('selection') == 'value':
                    ranking = sorted(candidates, key=lambda uid: (
                        price_for(uid) / candidates[uid], block['upgrade_ids'].index(uid)))
                    chosen = ranking[0]
                    rejected.append(f'{identity}: value ranking ' + ', '.join(
                        f'{uid} {price_for(uid) / candidates[uid]:.1f}' for uid in ranking))
                elif block.get('selection', 'priority') == 'weighted':
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
                               target=block.get('targets', {}).get(chosen),
                               price_source='model' if source == 'model' and model_quote(chosen) else 'observed')
        return None

    choice = evaluate(program) or saving or waiting_native
    if choice is None and unpriced:
        choice = observe_prices('blocks', unpriced)
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
        'model' if choice.price_source == 'model' else ('worker price evidence' if lane == 'workshop' else 'cached same-run battle rows (up to 60s)'), facts.variant,
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
            upgrade.category, price_for(upgrade.id, source=choice.price_source), wallet, choice.reason,
            target=choice.target, price_source=choice.price_source)
    status = 'observed' if lane == 'battle' or facts.screen == 'workshop' else 'projected'
    return RouteEvaluation(facts.account_id, route.revision, status, decision, trace,
                           facts.observed_at, choice.pending)
