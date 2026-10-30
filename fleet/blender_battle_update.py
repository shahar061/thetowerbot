"""Build the saved Blender strategy's in-run Battle purchase program."""

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from typing import Any, Mapping

import httpx2

from fleet.build_route import RouteBaseline
from upgrades import CATALOG


ECONOMY_IDS = ("cash_bonus", "cash_per_wave", "coins_per_kill_bonus", "coins_per_wave")
# Battle-only exclusions: permanent Workshop spending is configured separately.
EXCLUDED_IDS = frozenset({"defense_absolute", "health_regen"})
# Resolve ties in favor of the Blender core; every other battle stat remains
# eligible. Unlock tiles spend permanent coins and never belong in this pool.
_CORE_IDS = ("health", "defense_percent", "attack_speed", "knockback_chance",
             "knockback_force", "orbs", "orb_speed", "thorns", "lifesteal")
BATTLE_IDS = tuple(dict.fromkeys((*_CORE_IDS, *(upgrade.id for upgrade in CATALOG
                    if not upgrade.unlock and upgrade.id not in EXCLUDED_IDS))))


def configure_battle(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Buy the cheapest affordable battle stat, favoring early economy on ties.

    Compare observed prices so stats without calibrated price curves can
    compete too. Verified curves enable budgeted tap bursts; other stats use
    single taps. Refresh one row per ten purchased levels to discover upgrades
    without interrupting every purchase. No saving goal blocks spending.
    """
    updated = deepcopy(dict(baseline))
    early_ids = list(dict.fromkeys((*ECONOMY_IDS, *BATTLE_IDS)))
    def pool(identity: str, ids: list[str]) -> dict[str, Any]:
        return {"id": identity, "type": "pool", "label": "Cheapest affordable battle upgrade",
                "selection": "cheapest", "upgrade_ids": ids, "discovery_every_purchases": 10,
                "batch_size": 5, "max_price_premium_pct": 25}

    updated["battle"].update(mode="blocks", branches=[], blocks=[
        {"id": "blender.battle.wave20", "type": "condition", "field": "wave", "op": "lt", "value": 20,
         "then": [pool("blender.battle.early", early_ids)],
         "else": [pool("blender.battle.affordable", list(BATTLE_IDS))]},
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
