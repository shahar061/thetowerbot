"""Infer Workshop levels from the stat values the bot reads.

The bot OCRs each Workshop row's displayed value (Health "154"), never its
level. `catalog/workshop-levels.v1.json` holds every upgrade's full value
ladder, so the level is the rung whose value rounds to what was read. The read
is only as precise as its displayed digits: "1.00x" pins one rung, "20%" on a
ladder that repeats 20 pins several, and those stay an explicit range rather
than a guess. A value off the ladder (lab or card bonuses folded into the
display, a misread) is reported as unmatched, never snapped to the nearest rung.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import upgrades

CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "workshop-levels.v1.json"
_SUFFIXES = {"": 1., "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12, "q": 1e15, "Q": 1e18}
_NUMBER = re.compile(r"(\d+(?:\.(\d+))?)\s*([KMBTqQ]?)")


@dataclass(frozen=True)
class Ladder:
    max_level: int
    values: tuple[float, ...]
    next_coins: tuple[int | None, ...]


@dataclass(frozen=True)
class LevelEstimate:
    """`level_min == level_max` is an exact read; `status` is one of
    exact, ambiguous, unmatched."""
    status: str
    level_min: int | None
    level_max: int | None


@cache
def ladders() -> dict[str, Ladder]:
    payload = json.loads(CATALOG_PATH.read_text())
    return {upgrade_id: Ladder(row["max_level"], tuple(row["values"]), tuple(row["next_coins"]))
            for upgrade_id, row in payload["upgrades"].items()}


def displayed(raw: str) -> tuple[float, float] | None:
    """The number a Workshop row shows and half its last displayed digit."""
    match = _NUMBER.search(raw.replace(",", ""))
    if match is None:
        return None
    scale = _SUFFIXES[match[3]]
    decimals = len(match[2] or "")
    return float(match[1]) * scale, 0.5 * 10 ** -decimals * scale


def infer_level(ladder: Ladder, value: float, half_step: float) -> LevelEstimate:
    values = ladder.values
    # Ladders are monotone; a falling one (Wall Rebuild seconds) is searched negated.
    descending = values[-1] < values[0]
    keys = [-v for v in values] if descending else list(values)
    target = -value if descending else value
    tolerance = half_step + 1e-9 * max(1., abs(value))
    lo, hi = bisect_left(keys, target - tolerance), bisect_right(keys, target + tolerance) - 1
    if lo > hi:
        return LevelEstimate("unmatched", None, None)
    return LevelEstimate("exact" if lo == hi else "ambiguous", lo, hi)


def upgrade_state(upgrade_id: str, fact: dict[str, Any] | None) -> dict[str, Any]:
    ladder = ladders()[upgrade_id]
    row: dict[str, Any] = {"max_level": ladder.max_level, "value": None, "raw_value": None,
                           "observed_at": None, "status": "unseen", "level_min": None,
                           "level_max": None, "next_coins": None}
    if fact is None or fact.get("value") is None:
        return row
    evidence = fact.get("evidence") or {}
    raw = evidence.get("raw_value") or str(fact["value"])
    row.update(value=fact["value"], raw_value=raw, observed_at=evidence.get("observed_at"))
    read = displayed(raw)
    estimate = (infer_level(ladder, read[0], read[1]) if read is not None
                else LevelEstimate("unmatched", None, None))
    row.update(status=estimate.status, level_min=estimate.level_min, level_max=estimate.level_max)
    if estimate.level_min is not None:
        if estimate.level_min >= ladder.max_level:
            row["status"] = "maxed"
        else:
            row["next_coins"] = ladder.next_coins[estimate.level_min]
    return row


def workshop_state(workshop_stats: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One row per levelled Workshop upgrade, in the game's tab order, from a
    stored revision's `workshop_stats` facts."""
    facts = {fact["concept_id"]: fact for fact in workshop_stats or ()}
    return [{"id": item.id, "name": item.name, "category": item.category,
             **upgrade_state(item.id, facts.get(f"stats.{item.id}"))}
            for item in upgrades.CATALOG if item.id in ladders()]
