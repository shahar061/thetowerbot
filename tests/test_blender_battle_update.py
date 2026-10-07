"""The saved Blender Battle migration preserves other lanes and its phase order."""

import json
from types import SimpleNamespace

import pytest

from fleet.build_route import RouteBaseline, RouteDocument
from fleet.blender_battle_update import (
    COMBAT_IDS, DEF_ABS_LEVELS, ECONOMY_IDS, ORB_LEVELS, THORNS_LEVELS, configure_battle)
from fleet.build_route_eval import RouteFacts
from fleet.strategy_blocks import evaluate_program


def test_configure_battle_uses_unlimited_pools_split_at_wave_twenty() -> None:
    original = RouteDocument.compatibility().baseline.to_dict()
    updated = configure_battle(original)
    assert updated["workshop"] == original["workshop"]
    assert updated["labs"] == original["labs"]
    assert updated["battle"]["mode"] == "blocks"
    orbs, condition = updated["battle"]["blocks"]
    assert (orbs["upgrade_ids"], orbs["hold_until_capped"]) == (["orbs"], True)
    assert orbs["level_caps"] == {"orbs": {"base": ORB_LEVELS}}
    assert (condition["type"], condition["field"], condition["op"], condition["value"]) == (
        "condition", "wave", "lt", 20)
    def_abs, thorns = condition["else"][:2]
    assert (def_abs["upgrade_ids"], def_abs["hold_until_capped"]) == (["defense_absolute"], True)
    assert def_abs["level_caps"] == {"defense_absolute": {"base": DEF_ABS_LEVELS}}
    assert (thorns["upgrade_ids"], thorns["level_caps"]) == (["thorns"], {"thorns": {"base": THORNS_LEVELS}})
    assert "thorns" not in COMBAT_IDS
    for branch, ids in (("then", ECONOMY_IDS), ("else", COMBAT_IDS)):
        pool = condition[branch][-1]
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
              for i, uid in enumerate(ECONOMY_IDS + COMBAT_IDS + ("thorns",))}
    if economy_price is None:
        quotes.update({uid: dict(quote, status="maxed", price=None) for uid in ECONOMY_IDS})
    else:
        quotes["cash_bonus"]["price"] = economy_price
    quotes["attack_speed"]["price"] = combat_price
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=wave,
                       battle_cash=1000, run_purchases={uid: 50 for uid in ECONOMY_IDS}
                       | {"defense_absolute": DEF_ABS_LEVELS, "thorns": THORNS_LEVELS, "orbs": ORB_LEVELS},
                       battle_price_quotes=quotes)
    result = evaluate_program(route, facts, None, "battle")
    assert (result.decision.upgrade_id if result.decision else None) == expected


@pytest.mark.parametrize("def_abs,thorns,def_abs_price,expected", [
    (0, 0, 50, "defense_absolute"),
    (DEF_ABS_LEVELS - 1, 0, 50, "defense_absolute"),
    (3, 0, 5000, None),  # holds the cash for Defense Absolute rather than spending it
    (DEF_ABS_LEVELS, 0, 50, "thorns"),
    (DEF_ABS_LEVELS, THORNS_LEVELS - 1, 50, "thorns"),
    (DEF_ABS_LEVELS, THORNS_LEVELS, 50, "attack_speed"),
])
def test_after_the_economy_buys_def_abs_then_thorns_then_combat(
    def_abs: int, thorns: int, def_abs_price: int, expected: str | None,
) -> None:
    baseline = RouteBaseline.from_dict(configure_battle(RouteDocument.compatibility().baseline.to_dict()))
    route = SimpleNamespace(revision=1, battle=baseline.battle, workshop=baseline.workshop)
    quote = dict(account_id="account", run_id=7, source="model", verified=True, value=1, status="available")
    quotes = {uid: dict(quote, price=100) for uid in COMBAT_IDS} | {
        "attack_speed": dict(quote, price=5), "thorns": dict(quote, price=90)}
    rows = {"defense_absolute": {"status": "available", "value": 10, "price": def_abs_price, "observed_at": 100}}
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=25, battle_cash=1000,
                       run_purchases={"defense_absolute": def_abs, "thorns": thorns, "orbs": ORB_LEVELS},
                       upgrade_rows=rows, battle_price_quotes=quotes)
    result = evaluate_program(route, facts, None, "battle")
    assert (result.decision.upgrade_id if result.decision else None) == expected


@pytest.mark.parametrize("wave,orbs,orb_row,expected", [
    (1, 0, {"status": "available", "price": 300}, "orbs"),
    (25, ORB_LEVELS - 1, {"status": "available", "price": 900}, "orbs"),
    (1, 2, {"status": "unaffordable", "price": 4000}, None),  # holds the cash for Orbs
    (1, ORB_LEVELS, {"status": "available", "price": 1}, "cash_bonus"),
    (1, 0, {"status": "locked", "price": None}, "cash_bonus"),  # Orbs not unlocked in the Workshop
    (1, 1, {"status": "maxed", "price": None}, "cash_bonus"),
])
def test_orbs_come_first_and_hold_cash_until_capped(
    wave: int, orbs: int, orb_row: dict, expected: str | None,
) -> None:
    baseline = RouteBaseline.from_dict(configure_battle(RouteDocument.compatibility().baseline.to_dict()))
    route = SimpleNamespace(revision=1, battle=baseline.battle, workshop=baseline.workshop)
    quote = dict(account_id="account", run_id=7, source="model", verified=True, value=1, status="available")
    quotes = {uid: dict(quote, price=10) for uid in ECONOMY_IDS + COMBAT_IDS}
    rows = {"orbs": dict(orb_row, value=orbs, observed_at=100),
            "defense_absolute": {"status": "available", "value": 10, "price": 50, "observed_at": 100}}
    facts = RouteFacts("account", "worker", "battle", 100, 101, run_id=7, wave=wave, battle_cash=1000,
                       run_purchases={"orbs": orbs, "defense_absolute": DEF_ABS_LEVELS, "thorns": THORNS_LEVELS},
                       upgrade_rows=rows, battle_price_quotes=quotes)
    result = evaluate_program(route, facts, None, "battle")
    assert (result.decision.upgrade_id if result.decision else None) == expected
