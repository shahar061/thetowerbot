"""Convert every fleet strategy to a ranked lab list with direct, never-idle lab starts.

Usage:
    uv run python tools/migrate_lab_lists.py --dry-run   # print what would change
    uv run python tools/migrate_lab_lists.py             # save each strategy (new version)

Saving creates a new strategy version only; assigning it to workers is a separate step.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lab_catalog  # noqa: E402
from fleet.build_route import RouteBaseline  # noqa: E402
from fleet.lab_list import validate_lab_list  # noqa: E402
from fleet.resource_blocks import template_lab_list  # noqa: E402

GAME_SPEED = "labs.game-speed"
BANNED = ("black-hole", "cooldown")


def _template_tiers() -> dict[str, str]:
    tiers: dict[str, str] = {}
    for entry in template_lab_list()[0]["entries"]:
        tiers.setdefault(entry["lab_id"], entry["tier"])
    return tiers


def _walk(children: list[dict[str, Any]], slot: int | None, pinned: dict[int, list[tuple[str, int]]],
          late: list[tuple[str, int]], rest: list[tuple[str, int]]) -> None:
    for child in children:
        kind = child.get("type")
        if kind == "research":
            item = (child["lab_id"], int(child["to_level"]))
            if slot in (1, 2):
                pinned.setdefault(slot, []).append(item)
            elif slot is not None:
                late.append(item)  # A slot 3+ pin would never run without that slot: unpin, keep the order.
            else:
                rest.append(item)
        elif kind == "lab_pool":
            caps = child.get("caps") or {}
            for lab_id in child.get("lab_ids", []):
                entry = lab_catalog.lab(lab_id)
                top = entry.max_level if entry is not None and entry.max_level else 1
                rest.append((lab_id, int(caps.get(lab_id, top))))
        elif kind == "condition":
            _walk(list(child.get("then", [])) + list(child.get("else", [])), slot, pinned, late, rest)


def flatten_slot_tracks(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Slot tracks to ranked lab-list entries: slot-1 pins, slot-2 pins, then the rest in order."""
    pinned: dict[int, list[tuple[str, int]]] = {}
    late: list[tuple[str, int]] = []
    rest: list[tuple[str, int]] = []
    for block in blocks:
        if block.get("type") != "slot_track":
            continue
        slots = block.get("slots") or []
        _walk(block.get("children", []), slots[0] if len(slots) == 1 else None, pinned, late, rest)
    ordered = ([(lab, lvl, 1) for lab, lvl in pinned.get(1, [])]
               + [(lab, lvl, 2) for lab, lvl in pinned.get(2, [])]
               + [(lab, lvl, None) for lab, lvl in late]
               + [(lab, lvl, None) for lab, lvl in rest])
    tiers, notes = _template_tiers(), []
    entries: list[dict[str, Any]] = []
    highest: dict[str, tuple[int, int | None]] = {}
    for lab_id, level, pin in ordered:
        if any(word in lab_id for word in BANNED):
            notes.append(f"dropped {lab_id}: never researched (banned)")
            continue
        entry = lab_catalog.lab(lab_id)
        if entry is None or entry.levels is None:
            notes.append(f"dropped {lab_id}: no price table")
            continue
        level = min(level, entry.max_level)
        if lab_id in highest:
            top, top_pin = highest[lab_id]
            if level <= top or (pin is not None and top_pin is not None and pin != top_pin):
                notes.append(f"dropped {lab_id} L{level}: repeats L{top}")
                continue
        highest[lab_id] = (level, pin)
        item: dict[str, Any] = {
            "id": f"migrated.{lab_id.removeprefix('labs.')}.{level}", "lab_id": lab_id,
            "to_level": level, "tier": "S+" if lab_id == GAME_SPEED else tiers.get(lab_id, "B"),
            "label": entry.name}
        if pin is not None:
            item["pin_slot"] = pin
        entries.append(item)
    if not entries or entries[0]["lab_id"] != GAME_SPEED or entries[0].get("pin_slot") != 1:
        entries = [e for e in entries if e["lab_id"] != GAME_SPEED]
        entries.insert(0, {"id": "migrated.game-speed.7", "lab_id": GAME_SPEED, "to_level": 7,
                           "tier": "S+", "pin_slot": 1, "label": "Game Speed to max"})
        notes.append("put Game Speed first, pinned to slot 1")
    return entries, notes


def migrate_baseline(baseline: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    new = copy.deepcopy(baseline)
    labs, notes = new["labs"], []
    blocks = labs.get("blocks") or []
    if labs.get("mode") != "blocks":
        labs["blocks"] = list(template_lab_list())
        notes.append("steps mode: replaced with the built-in early-game ranked list")
    elif not (len(blocks) == 1 and blocks[0].get("type") == "lab_list"):
        entries, notes = flatten_slot_tracks(blocks)
        labs["blocks"] = [validate_lab_list({"id": "labs.list", "type": "lab_list",
                                             "label": "Migrated ranked list", "entries": entries})]
        notes.insert(0, f"slot tracks: flattened to {len(entries)} ranked entries")
    labs["mode"] = "blocks"
    rules = new["rules"]
    rules["coins"]["lab_share"] = {"mode": "just_in_time", "pct": 25}
    rules["labs"]["filler"]["enabled"] = True
    rules["labs"]["direct_start"] = True
    rules["labs"]["auto_start"] = True
    return new, notes


def _request(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    url = f"http://127.0.0.1:{args.port}/api/fleet/reroll/strategies"
    library = _request(url)
    revision, failed = library["revision"], 0
    for row in library["strategies"]:
        try:
            new, notes = migrate_baseline(row["baseline"])
            RouteBaseline.from_dict(new)
        except ValueError as exc:
            print(f"SKIP {row['name']}: {exc}")
            failed += 1
            continue
        old_labs, new_labs = row["baseline"]["labs"], new["labs"]
        print(f"== {row['name']} (v{row['version']}): "
              + ("; ".join(notes) or "rules only"))
        if old_labs != new_labs:
            for entry in new_labs["blocks"][0]["entries"]:
                pin = f" [slot {entry['pin_slot']}]" if "pin_slot" in entry else ""
                print(f"   {entry['tier']:>2} {entry['lab_id']} -> L{entry['to_level']}{pin}")
        if args.dry_run:
            continue
        try:
            library = _request(url, {"expected_revision": revision, "name": row["name"],
                                     "source_template": row["source_template"],
                                     "baseline": new, "strategy_id": row["id"]})
            revision = library["revision"]
            print(f"   saved (library revision {revision})")
        except urllib.error.HTTPError as exc:
            print(f"   SAVE FAILED {exc.code}: {exc.read().decode()[:300]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
