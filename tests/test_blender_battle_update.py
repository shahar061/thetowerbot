"""The saved Blender Battle migration preserves other lanes and its phase order."""

import json

from fleet.build_route import RouteDocument
from fleet.blender_battle_update import configure_battle


def test_configure_battle_uses_twenty_economy_buys_then_cheapest_combat() -> None:
    original = RouteDocument.compatibility().baseline.to_dict()
    updated = configure_battle(original)
    assert updated["workshop"] == original["workshop"]
    assert updated["labs"] == original["labs"]
    assert updated["battle"]["mode"] == "blocks"
    assert len(updated["battle"]["blocks"]) == 2
    economy, combat = updated["battle"]["blocks"]
    assert economy["selection"] == "cheapest"
    assert economy["hold_until_capped"] is True
    assert set(economy["level_caps"]) == set(economy["upgrade_ids"])
    assert {cap["base"] for cap in economy["level_caps"].values()} == {20}
    assert combat["selection"] == "cheapest"
    assert set(combat["upgrade_ids"]) == {
        "attack_speed", "health", "defense_percent", "thorns", "orbs", "orb_speed",
        "knockback_force", "knockback_chance", "multishot_chance", "multishot_targets",
        "bounce_shot_chance", "bounce_shot_targets", "bounce_shot_range",
    }
    assert configure_battle(updated) == updated
    saved = json.loads(json.dumps(updated))
    assert configure_battle(saved) == saved
