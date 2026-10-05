"""The saved Blender Battle migration preserves other lanes and its phase order."""

import json
from types import SimpleNamespace

import pytest

from fleet.build_route import RouteBaseline, RouteDocument
from fleet.blender_battle_update import COMBAT_IDS, ECONOMY_IDS, configure_battle
from fleet.build_route_eval import RouteFacts
from fleet.strategy_blocks import evaluate_program


def test_configure_battle_uses_unlimited_pools_split_at_wave_twenty() -> None:
    original = RouteDocument.compatibility().baseline.to_dict()
    updated = configure_battle(original)
    assert updated["workshop"] == original["workshop"]
    assert updated["labs"] == original["labs"]
    assert updated["battle"]["mode"] == "blocks"
    assert len(updated["battle"]["blocks"]) == 1
    condition = updated["battle"]["blocks"][0]
    assert (condition["type"], condition["field"], condition["op"], condition["value"]) == (
        "condition", "wave", "lt", 20)
    for branch, ids in (("then", ECONOMY_IDS), ("else", COMBAT_IDS)):
        pool, = condition[branch]
        assert pool["selection"] == "cheapest"
        assert pool["price_source"] == "model"
        assert (pool['batch_size'], pool['max_price_premium_pct']) == (5, 25)
        assert pool["upgrade_ids"] == list(ids)
        assert "level_caps" not in pool and "max_purchases" not in pool
    assert configure_battle(updated) == updated
    saved = json.loads(json.dumps(updated))
    assert configure_battle(saved) == saved


@pytest.mark.parametrize("wave,economy_price,combat_price,expected", [
    (1, 10, 1, "cash_bonus"),
    (19, 10, 1, "cash_bonus"),
    (20, 1, 10, "attack_speed"),
    (100, 1, 10, "attack_speed"),
    (19, None, 1, None),
])
def test_wave_selects_pool_even_after_twenty_purchases(
    wave: int, economy_price: int | None, combat_price: int, expected: str | None,
) -> None:
    baseline = RouteBaseline.from_dict(configure_battle(RouteDocument.compatibility().baseline.to_dict()))
    route = SimpleNamespace(revision=1, battle=baseline.battle, workshop=baseline.workshop)
    quote = dict(account_id="account", run_id=7, source="model", verified=True, value=1)
    quotes = {uid: dict(quote, status="available", price=100 + i)
              for i, uid in enumerate(ECONOMY_IDS + COMBAT_IDS)}
    if economy_price is None:
        quotes.update({uid: dict(quote, status="maxed", price=None) for uid in ECONOMY_IDS})
    else:
        quotes["cash_bonus"]["price"] = economy_price
    quotes["attack_speed"]["price"] = combat_price
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=wave,
                       battle_cash=1000, run_purchases={uid: 50 for uid in ECONOMY_IDS},
                       battle_price_quotes=quotes)
    result = evaluate_program(route, facts, None, "battle")
    assert (result.decision.upgrade_id if result.decision else None) == expected
