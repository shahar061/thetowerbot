"""The saved Blender Battle migration preserves other lanes and its phase order."""

import json

from fleet.build_route import RouteDocument
from fleet.blender_battle_update import configure_battle


def test_configure_battle_uses_one_unlimited_modeled_pool() -> None:
    from fleet.blender_battle_update import ECONOMY_IDS, COMBAT_IDS
    original = RouteDocument.compatibility().baseline.to_dict()
    updated = configure_battle(original)
    assert updated["workshop"] == original["workshop"]
    assert updated["labs"] == original["labs"]
    assert updated["battle"]["mode"] == "blocks"
    assert len(updated["battle"]["blocks"]) == 1
    pool = updated["battle"]["blocks"][0]
    assert pool["selection"] == "cheapest"
    assert pool["price_source"] == "model"
    assert pool["upgrade_ids"] == list(ECONOMY_IDS + COMBAT_IDS)
    assert "level_caps" not in pool and "max_purchases" not in pool
    assert configure_battle(updated) == updated
    saved = json.loads(json.dumps(updated))
    assert configure_battle(saved) == saved
