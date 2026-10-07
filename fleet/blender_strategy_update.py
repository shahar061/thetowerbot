"""Apply the Blender Workshop phase to the two assigned custom strategies.

Run ``python -m fleet.blender_strategy_update`` to preview. Add ``--apply`` to
save new strategy versions and assign them to emulators 82 and 83.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fleet.build_route import RouteBaseline
from fleet.reroll_planner import _ban_closure
from fleet.strategy_blocks import program_upgrade_ids, rename_block_ids, template_program

TARGETS = (("Survivor Ladder", "sl", 450, "Tiramisu64_82"),
           ("Blender Road", "br", 400, "Tiramisu64_83"))


def add_blender_gate(baseline: Mapping[str, Any], *, prefix: str,
                     threshold: int) -> dict[str, Any]:
    """Put Blender first after a recorded Tier 1 wave, preserving earlier rules.

    A terminating wait prevents the old Workshop program from buying Defense
    Absolute after the gate. Range must be unlockable for Multishot and Bounce.
    """
    if not prefix or type(threshold) is not int or threshold < 1:
        raise ValueError("Blender gate needs a prefix and positive wave")
    updated = deepcopy(dict(baseline))
    workshop = updated["workshop"]
    if workshop["mode"] != "blocks":
        raise ValueError("Blender gate requires Workshop blocks")
    gate = deepcopy(template_program("turtle", "workshop")[0])
    gate_id = f"{prefix}.blender.active"
    gate.update({"id": gate_id, "label": f"Blender after best Tier 1 wave {threshold}",
                 "value": threshold, "else": []})
    rename_block_ids(gate["then"], "turtle.blender.", f"{prefix}.blender.")
    workshop["blocks"] = [gate, *(block for block in workshop["blocks"]
                                  if block["id"] != gate_id)]
    workshop["banned_upgrade_ids"] = [uid for uid in workshop["banned_upgrade_ids"]
                                       if uid != "unlock_range_upgrades"]
    priorities = set(program_upgrade_ids(tuple(gate["then"])))
    if _ban_closure(frozenset(workshop["banned_upgrade_ids"])) & priorities:
        raise ValueError("a Blender priority is still banned")
    RouteBaseline.from_dict(updated)
    return updated


def _request(base_url: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(base_url.rstrip("/") + path, data=data,
                      headers={"Content-Type": "application/json"} if data else {},
                      method="POST" if data else "GET")
    try:
        with urlopen(request, timeout=20) as response:
            result = json.load(response)
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{path}: HTTP {exc.code}: {detail}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return result


def update_saved_strategies(base_url: str, *, apply: bool = False) -> list[dict[str, Any]]:
    """Preview or save new versions and assign them through the guarded API."""
    library = _request(base_url, "/api/fleet/reroll/strategies")
    route = _request(base_url, "/api/fleet/reroll/route")
    changes: list[dict[str, Any]] = []
    for name, prefix, threshold, worker in TARGETS:
        matches = [row for row in library["strategies"] if row["name"] == name]
        if len(matches) != 1:
            raise ValueError(f"expected one saved strategy named {name!r}")
        saved = matches[0]
        baseline = add_blender_gate(saved["baseline"], prefix=prefix, threshold=threshold)
        needs_save = baseline != saved["baseline"]
        next_version = saved["version"] + int(needs_save)
        assignment = route["assignments"][worker]
        if assignment["strategy_id"] != saved["id"]:
            raise ValueError(f"{worker} is no longer assigned {name}")
        workers = ([{"worker": worker, "account_id": assignment["account_id"]}]
                   if (assignment["strategy_version"] != next_version
                       or assignment["baseline"] != baseline) else [])
        change = {"strategy": name, "wave": threshold, "current_version": saved["version"],
                  "target_version": next_version, "save": needs_save,
                  "assign_workers": [row["worker"] for row in workers]}
        changes.append(change)
        if not apply:
            continue
        if needs_save:
            library = _request(base_url, "/api/fleet/reroll/strategies", {
                "expected_revision": library["revision"], "strategy_id": saved["id"],
                "name": name, "source_template": saved["source_template"],
                "baseline": baseline,
            })
            saved = next(row for row in library["strategies"] if row["id"] == saved["id"])
            if saved["version"] != next_version:
                raise RuntimeError(f"{name}: saved version changed unexpectedly")
        if workers:
            route = _request(base_url, "/api/fleet/reroll/strategies/assign", {
                "expected_revision": route["revision"], "strategy_id": saved["id"],
                "strategy_version": saved["version"], "workers": workers,
            })
            change["route_revision"] = route["revision"]
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--apply", action="store_true",
                        help="save strategy versions and assign them; default is read-only preview")
    args = parser.parse_args()
    for change in update_saved_strategies(args.base_url, apply=args.apply):
        print(json.dumps(change, sort_keys=True))


if __name__ == "__main__":
    main()
