"""Install the Blender v2 Workshop program on the saved ``blender`` strategy.

Run ``python -m fleet.blender_v2_update`` to preview. Add ``--apply`` to save a
new version and assign it to emulators 82 and 83. The battle and other lanes
are left untouched.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from typing import Any, Mapping

from fleet.blender_strategy_update import _request
from fleet.build_route import RouteBaseline
from fleet.reroll_planner import _ban_closure
from fleet.strategy_blocks import template_program

STRATEGY = "blender"
PREFIX = "bv2"
WORKERS = ("Tiramisu64_82", "Tiramisu64_83")
# Workshop Orbs level (3,000 then 20,000 coins) bought before any other skill.
ORB_LEVELS = 2


def orbs_first() -> list[dict[str, Any]]:
    """Unlock Orbs on its own, then hold coins until the displayed Orbs reaches ORB_LEVELS."""
    return [
        {"id": f"{PREFIX}.orbs_unlock", "type": "unlock", "label": "Unlock Orbs first",
         "upgrade_ids": ["orbs"], "max_price": 20000, "hold": True},
        {"id": f"{PREFIX}.orbs", "type": "save_for", "label": f"Save for Orbs level {ORB_LEVELS}", "hold": True,
         "goal": [{"id": f"{PREFIX}.orbs.goal", "type": "pool", "label": "Orbs",
                   "upgrade_ids": ["orbs"], "selection": "priority", "targets": {"orbs": ORB_LEVELS}}]},
    ]


def blender_v2_workshop(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """The baseline with its Workshop program replaced by Blender v2, Orbs first."""
    updated = deepcopy(dict(baseline))
    workshop = updated["workshop"]
    program = deepcopy(list(template_program("turtle", "workshop")[0]["then"]))
    for block in program:
        block["id"] = block["id"].replace("turtle.blender.", f"{PREFIX}.", 1)
    program[:0] = orbs_first()
    wanted = {uid for block in program for uid in block.get("upgrade_ids", ())}
    if _ban_closure(frozenset(workshop["banned_upgrade_ids"])) & wanted:
        raise ValueError("a Blender v2 skill is banned by Never Buy")
    workshop.update(mode="blocks", blocks=program)
    RouteBaseline.from_dict(updated)
    return updated


def update_blender(base_url: str, *, apply: bool = False) -> dict[str, Any]:
    """Preview or save the new version and assign it through the guarded API."""
    library = _request(base_url, "/api/fleet/reroll/strategies")
    route = _request(base_url, "/api/fleet/reroll/route")
    matches = [row for row in library["strategies"] if row["name"] == STRATEGY]
    if len(matches) != 1:
        raise ValueError(f"expected one saved strategy named {STRATEGY!r}")
    saved = matches[0]
    baseline = blender_v2_workshop(saved["baseline"])
    needs_save = baseline != saved["baseline"]
    next_version = saved["version"] + int(needs_save)
    workers = []
    for worker in WORKERS:
        assignment = route["assignments"][worker]
        if assignment["strategy_id"] != saved["id"]:
            raise ValueError(f"{worker} is no longer assigned {STRATEGY}")
        if assignment["strategy_version"] != next_version or assignment["baseline"] != baseline:
            workers.append({"worker": worker, "account_id": assignment["account_id"]})
    change = {"strategy": STRATEGY, "current_version": saved["version"], "target_version": next_version,
              "save": needs_save, "assign_workers": [row["worker"] for row in workers]}
    if not apply:
        return change
    if needs_save:
        library = _request(base_url, "/api/fleet/reroll/strategies", {
            "expected_revision": library["revision"], "strategy_id": saved["id"],
            "name": STRATEGY, "source_template": saved["source_template"], "baseline": baseline,
        })
        saved = next(row for row in library["strategies"] if row["id"] == saved["id"])
        if saved["version"] != next_version:
            raise RuntimeError(f"{STRATEGY}: saved version changed unexpectedly")
    if workers:
        route = _request(base_url, "/api/fleet/reroll/strategies/assign", {
            "expected_revision": route["revision"], "strategy_id": saved["id"],
            "strategy_version": saved["version"], "workers": workers,
        })
        change["route_revision"] = route["revision"]
    return change


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--apply", action="store_true",
                        help="save the strategy version and assign it; default is read-only preview")
    args = parser.parse_args()
    print(json.dumps(update_blender(args.base_url, apply=args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
