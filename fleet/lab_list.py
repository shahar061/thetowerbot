"""The ranked lab list: one ordered list of labs every owned slot draws from.

Validation here; the pure per-slot evaluation (pins, ranked walk, fillers) is
added below it. Kept apart from resource_blocks.py, which keeps slot tracks.
"""

from __future__ import annotations

from typing import Any, Mapping

import lab_catalog
from fleet.build_route import TIERS
from fleet.strategy_blocks import MAX_BLOCKS

# The block's own id also claims a slot against _Ids's shared MAX_BLOCKS cap,
# so the list itself may hold at most MAX_BLOCKS - 1 entries.
MAX_ENTRIES = MAX_BLOCKS - 1

_ENTRY_FIELDS = {"id", "lab_id", "to_level", "tier", "pin_slot", "label"}


def validate_lab_list(raw: Mapping[str, Any]) -> dict[str, Any]:
    from fleet.resource_blocks import LAB_SLOTS, _fields, _Ids, _int, _lab_id, _max_level
    block = dict(raw)
    ids = _Ids()
    ids.claim(block)
    _fields(block, {"entries"})
    items = block.get("entries")
    if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= MAX_ENTRIES:
        raise ValueError(f"a lab list needs 1 to {MAX_ENTRIES} entries")
    entries: list[dict[str, Any]] = []
    highest: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("lab list entry must be an object")
        entry = dict(item)
        if set(entry) - _ENTRY_FIELDS:
            raise ValueError("unknown lab list entry field")
        ids.claim(entry)
        lab_id = _lab_id(entry.get("lab_id"))
        level = _int(entry.get("to_level"), "to_level", 1, _max_level(lab_id))
        if entry.get("tier") not in TIERS:
            raise ValueError("lab list tier must be one of S+, S, A, B, C")
        if "pin_slot" in entry:
            _int(entry["pin_slot"], "pin_slot", LAB_SLOTS[0], LAB_SLOTS[-1])
        if lab_id in highest and level <= highest[lab_id]:
            raise ValueError("a repeated lab needs a higher to_level further down the list")
        highest[lab_id] = level
        entries.append(entry)
    first = entries[0]
    if first["lab_id"] != lab_catalog.GAME_SPEED or first.get("pin_slot") != 1:
        raise ValueError("the lab list must start with Game Speed pinned to slot 1")
    block["entries"] = entries
    return block
