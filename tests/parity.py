"""Parsed-result comparison for parity tests (spec invariant 4).

Compares what the bot acts on, never raw OCR strings.
"""
from __future__ import annotations

from typing import Any

from perception import Observation


def observation_diff(old: Observation, new: Observation) -> tuple[set[str], set[str]]:
    """(changes, gains) between two observations of one frame.

    A gain is a field `old` left unknown - None, a missing combat reading or
    row, an 'unreadable' row status, no tap - that `new` fills in. Every other
    difference is a change. Taps are compared by presence: a band read maps
    boxes back through a scale, so a tap point may move by a pixel.
    """
    changes: set[str] = set()
    gains: set[str] = set()

    def compare(name: str, a: Any, b: Any, unknown: tuple[Any, ...] = (None,)) -> None:
        if a == b:
            return
        (gains if a in unknown and b not in unknown else changes).add(name)

    for field in ("category", "cash", "paused"):
        compare(field, getattr(old, field), getattr(new, field))
    for key in set(old.combat) | set(new.combat):
        compare(f"combat.{key}", old.combat.get(key), new.combat.get(key))
    old_rows = {row.upgrade_id: row for row in old.rows}
    new_rows = {row.upgrade_id: row for row in new.rows}
    for upgrade_id in set(old_rows) | set(new_rows):
        a, b = old_rows.get(upgrade_id), new_rows.get(upgrade_id)
        if a is None or b is None:
            (gains if a is None else changes).add(f"rows.{upgrade_id}")
            continue
        compare(f"rows.{upgrade_id}.name", a.name, b.name)
        compare(f"rows.{upgrade_id}.value", a.value, b.value)
        compare(f"rows.{upgrade_id}.price", a.price, b.price)
        compare(f"rows.{upgrade_id}.status", a.status, b.status, unknown=("unreadable",))
        compare(f"rows.{upgrade_id}.tap", a.tap is not None or None, b.tap is not None or None)
    return changes, gains
