"""Build the saved Blender strategy's in-run Battle purchase program."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any, Mapping

import httpx2

from fleet.build_route import RouteBaseline


ECONOMY_IDS = ("cash_bonus", "cash_per_wave", "coins_per_kill_bonus", "coins_per_wave")
COMBAT_IDS = (
    "attack_speed", "health", "defense_percent", "orbs", "orb_speed",
    "knockback_force", "knockback_chance", "multishot_chance", "multishot_targets",
    "bounce_shot_chance", "bounce_shot_targets", "bounce_shot_range",
)
# In-run levels, not displayed values: the displayed Thorns % already includes
# the Workshop base, so a 51% target buys next to nothing on a built account.
DEF_ABS_LEVELS = 25
THORNS_LEVELS = 20


def configure_battle(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Buy economy before wave 20, then Defense Absolute, some Thorns and the cheapest combat upgrade.

    Defense Absolute has no modeled cash curve, so it buys from read prices,
    and the run holds its cash for it until all its levels are bought.
    """
    updated = deepcopy(dict(baseline))
    updated["battle"].update(mode="blocks", branches=[], blocks=[
        {"id": "blender.battle.wave20", "type": "condition", "field": "wave", "op": "lt", "value": 20,
         "then": [
             {"id": "blender.battle.economy", "type": "pool", "label": "Economy before wave 20",
              "selection": "cheapest", "price_source": "model", "upgrade_ids": list(ECONOMY_IDS),
              "batch_size": 5, "max_price_premium_pct": 25},
         ],
         "else": [
             {"id": "blender.battle.def_abs", "type": "pool", "label": "Defense Absolute after the economy",
              "upgrade_ids": ["defense_absolute"], "hold_until_capped": True,
              "level_caps": {"defense_absolute": {"base": DEF_ABS_LEVELS}}},
             {"id": "blender.battle.thorns", "type": "pool", "label": "Thorns from wave 20",
              "selection": "cheapest", "price_source": "model", "upgrade_ids": ["thorns"],
              "level_caps": {"thorns": {"base": THORNS_LEVELS}}},
             {"id": "blender.battle.combat", "type": "pool", "label": "Blender combat from wave 20",
              "selection": "cheapest", "price_source": "model", "upgrade_ids": list(COMBAT_IDS),
              "batch_size": 5, "max_price_premium_pct": 25},
         ]},
    ])
    RouteBaseline.from_dict(updated)
    return updated


async def update_saved_blender(base_url: str, *, apply: bool = False) -> dict[str, Any]:
    """Preview or publish a new saved version for every currently assigned worker."""
    async with httpx2.AsyncClient(base_url=base_url, timeout=20) as client:
        library_response = await client.get("/api/fleet/reroll/strategies")
        library_response.raise_for_status()
        library = library_response.json()
        route_response = await client.get("/api/fleet/reroll/route")
        route_response.raise_for_status()
        route = route_response.json()
        matches = [row for row in library["strategies"] if row["name"].casefold() == "blender"]
        if len(matches) != 1:
            raise ValueError("expected exactly one saved blender strategy")
        saved = matches[0]
        baseline = configure_battle(saved["baseline"])
        needs_save = baseline != saved["baseline"]
        assignments = [{"worker": worker, "account_id": assignment["account_id"]}
                       for worker, assignment in route["assignments"].items()
                       if assignment["strategy_id"] == saved["id"]]
        if not assignments:
            raise ValueError("blender is not assigned to any worker")
        result = {"strategy_id": saved["id"], "current_version": saved["version"],
                  "target_version": saved["version"] + int(needs_save),
                  "workers": [row["worker"] for row in assignments], "save": needs_save}
        if not apply:
            return result
        if needs_save:
            save_response = await client.post("/api/fleet/reroll/strategies", json={
                "expected_revision": library["revision"], "strategy_id": saved["id"],
                "name": saved["name"], "source_template": saved["source_template"],
                "baseline": baseline,
            })
            save_response.raise_for_status()
            library = save_response.json()
            saved = next(row for row in library["strategies"] if row["id"] == saved["id"])
        outdated = [row for row in assignments if
                    route["assignments"][row["worker"]]["strategy_version"] != saved["version"]]
        if outdated:
            assign_response = await client.post("/api/fleet/reroll/strategies/assign", json={
                "expected_revision": route["revision"], "strategy_id": saved["id"],
                "strategy_version": saved["version"], "workers": outdated,
            })
            assign_response.raise_for_status()
            result["route_revision"] = assign_response.json()["revision"]
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--apply", action="store_true", help="save and publish; default previews")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(update_saved_blender(args.base_url, apply=args.apply)),
                     sort_keys=True))


if __name__ == "__main__":
    main()
