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
    "attack_speed", "health", "defense_percent", "thorns", "orbs", "orb_speed",
    "knockback_force", "knockback_chance", "multishot_chance", "multishot_targets",
    "bounce_shot_chance", "bounce_shot_targets", "bounce_shot_range",
)


def configure_battle(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Require 20 confirmed buys per available economy row, then buy cheapest combat."""
    updated = deepcopy(dict(baseline))
    updated["battle"].update(mode="blocks", branches=[], blocks=[
        {"id": "blender.battle.economy", "type": "pool", "label": "Economy: 20 buys each",
         "selection": "cheapest", "upgrade_ids": list(ECONOMY_IDS),
         "level_caps": {uid: {"base": 20} for uid in ECONOMY_IDS},
         "hold_until_capped": True},
        {"id": "blender.battle.combat", "type": "pool", "label": "Cheapest Blender upgrade",
         "selection": "cheapest", "upgrade_ids": list(COMBAT_IDS)},
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
