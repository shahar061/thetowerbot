"""Lossless policy snapshots and a single definition of profile ownership."""
from __future__ import annotations

from dataclasses import fields, replace
import json
from typing import Any, Mapping

from strategy import Strategy
import builds

PLAN_FIELDS = frozenset({'actions', 'affordability', 'autopilot', 'shopping',
                        'cards', 'tier_promotion', 'build', '_build_recipe', 'tournament'})
BOT_FIELDS = frozenset({'name', 'interval', 'menu_interval', 'click_cooldown',
    'auto_navigate', 'max_runs', 'push_every_farm_runs', 'navigation_cooldown', 'screen_confirmations',
    'tap_jitter_px', 'timing_jitter', 'tap_delay', 'target_speed', 'auto_fastest', 'claims'})


def plan_owned_values(strategy: Strategy) -> dict[str, Any]:
    data = strategy.to_dict()
    return {key: data.get(key) for key in sorted(PLAN_FIELDS)}


def capture_legacy(strategy: Strategy, source_revision: str) -> dict[str, Any]:
    if {f.name for f in fields(Strategy)} != PLAN_FIELDS | BOT_FIELDS:
        raise ValueError('unclassified strategy field')
    result = {'schema_version': 1, 'profile': strategy.name,
            'source_revision': source_revision, 'strategy': strategy.to_dict()}
    if strategy.build is not None:
        result['build_pack'] = json.loads(builds.PACK_PATH.read_text(encoding='utf-8'))
    return result


def captured_build(snapshot: Mapping[str, Any]) -> builds.Build | None:
    build_id = snapshot['strategy'].get('build')
    if build_id is None:
        return None
    pack = builds.BuildPack.from_payload(snapshot['build_pack'])
    recipe = pack.by_id(build_id)
    if recipe is None:
        raise ValueError('imported build recipe unavailable')
    return recipe


def validate_snapshot(snapshot: Mapping[str, Any]) -> Strategy:
    if snapshot.get('schema_version') != 1 or not snapshot.get('source_revision'):
        raise ValueError('invalid legacy snapshot')
    recipe = captured_build(snapshot)
    if recipe is None:
        return Strategy.from_dict(snapshot['strategy'])
    # Validate using the captured recipe rather than the mutable global pack.
    parsed = Strategy.from_dict({**snapshot['strategy'], 'build': None})
    return replace(parsed, build=snapshot['strategy']['build'], _build_recipe=recipe)


def restore_legacy(local: Strategy, snapshot: Mapping[str, Any]) -> Strategy:
    captured = validate_snapshot(snapshot)
    return replace(local, **{key: getattr(captured, key) for key in PLAN_FIELDS})


def assert_settings_only(before: Strategy, after: Strategy) -> None:
    if plan_owned_values(before) != plan_owned_values(after):
        raise ValueError('strategy_plan_owns_purchase_settings')
