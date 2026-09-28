"""The Fleet State page: one live column per emulator account.

Every `build_*` function takes plain values - a revision's JSON, ledger rows
SQL has already bounded or summed, a worker's /api/status dict - and returns
a JSON-safe dict, so each one is tested on literals without a live worker. A
value nobody observed is None, never 0 and never a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import upgrades
import workshop_levels
from fleet import workshop_prices

CATEGORIES = ("attack", "defense", "utility")
RECENT_LIMIT = 30


def iso(ts: float | None) -> str | None:
    """Epoch seconds as an ISO-8601 UTC string, or None."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _category(value: object) -> str | None:
    lowered = value.lower() if isinstance(value, str) else ""
    return lowered if lowered in CATEGORIES else None


def _resolve(item: object, category: object) -> upgrades.Upgrade | None:
    """A ledger row's upgrade: its item is an id or the name the game showed."""
    if not isinstance(item, str) or not item:
        return None
    return upgrades.by_id(item) or upgrades.resolve(item, category if isinstance(category, str) else None)


def category_totals(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Sum of known levels per Workshop tab; unseen and unmatched rows add nothing."""
    totals = dict.fromkeys(CATEGORIES, 0)
    for row in rows:
        category = _category(row.get("category"))
        level = row.get("level_min")
        if category is None or row.get("status") in ("unseen", "unmatched") or level is None:
            continue
        totals[category] += int(level)
    return totals


def invested_coins(upgrade_id: str, level: int | None) -> int | None:
    """Coins the ladder charges to reach `level`: the sum of its first `level` rungs."""
    ladder = workshop_levels.ladders().get(upgrade_id)
    if ladder is None or level is None or level < 0:
        return None
    rungs = ladder.next_coins[:min(level, ladder.max_level)]
    if any(rung is None for rung in rungs):
        return None
    return sum(rungs)  # type: ignore[arg-type]


def bot_spent(rows: Iterable[tuple[Any, Any, Any]]) -> dict[str, int]:
    """Coins the bot provably spent per upgrade, from (item, category, coins) sums."""
    spent: dict[str, int] = {}
    for item, category, coins in rows:
        upgrade = _resolve(item, category)
        if upgrade is None or coins is None:
            continue
        spent[upgrade.id] = spent.get(upgrade.id, 0) + int(coins)
    return spent


def owned_unlocks(revision: Mapping[str, Any] | None, rows_by_id: Mapping[str, Mapping[str, Any]],
                  bought: set[str]) -> set[str]:
    """Unlock tiles this account owns.

    Owned when the revision records it, when the bot bought it, or when any
    row it grants has been read - a granted row is only drawn once unlocked.
    """
    facts = {fact.get("concept_id"): fact.get("value")
             for fact in (revision or {}).get("unlocks") or ()}
    owned: set[str] = set()
    for upgrade in upgrades.CATALOG:
        if not upgrade.unlock:
            continue
        if (facts.get(upgrade.concept_id) or upgrade.id in bought
                or any(rows_by_id.get(granted, {}).get("status", "unseen") != "unseen"
                       for granted in upgrade.unlocks)):
            owned.add(upgrade.id)
    return owned


def next_unlock(category: str, owned: set[str]) -> dict[str, Any] | None:
    """The first unowned unlock tile of a tab, in the game's order; None when all are owned."""
    for upgrade in upgrades.CATALOG:
        if upgrade.unlock and upgrade.category.lower() == category and upgrade.id not in owned:
            return {"id": upgrade.id, "name": upgrade.name,
                    "cost": workshop_prices.catalog_price(upgrade.id, 0)}
    return None


def workshop_recent(rows: Iterable[Mapping[str, Any]], limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
    """Newest-first Workshop buys. The ledger does not record the level bought."""
    recent: list[dict[str, Any]] = []
    for row in rows:
        if len(recent) >= limit:
            break
        upgrade = _resolve(row.get("item"), row.get("category"))
        recent.append({
            "ts": iso(row.get("ts")),
            "id": upgrade.id if upgrade else None,
            "name": upgrade.name if upgrade else str(row.get("item") or "?"),
            "category": _category(upgrade.category if upgrade else row.get("category")),
            "level": None,
            "price": row.get("price"),
        })
    return recent


def build_workshop(revision: Mapping[str, Any] | None,
                   spent_rows: Iterable[tuple[Any, Any, Any]],
                   recent_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """The Workshop section: per-tab totals, skills, next unlock and recent buys."""
    spent_rows = list(spent_rows)
    rows = workshop_levels.workshop_state((revision or {}).get("workshop_stats"))
    rows_by_id = {row["id"]: row for row in rows}
    spent = bot_spent(spent_rows)
    bought = {upgrade.id for item, category, _ in spent_rows
              if (upgrade := _resolve(item, category)) is not None}
    owned = owned_unlocks(revision, rows_by_id, bought)
    gate = {granted: upgrade.id for upgrade in upgrades.CATALOG if upgrade.unlock
            for granted in upgrade.unlocks}
    categories: dict[str, Any] = {}
    for category in CATEGORIES:
        skills = []
        for row in rows:
            if row["category"].lower() != category:
                continue
            gated_by = gate.get(row["id"])
            skills.append({
                "id": row["id"], "name": row["name"], "level": row["level_min"],
                "invested": invested_coins(row["id"], row["level_min"]),
                "bot_spent": spent.get(row["id"], 0),
                "next_cost": row["next_coins"], "status": row["status"],
                "locked": gated_by is not None and gated_by not in owned,
            })
        categories[category] = {"unlocked": sum(not skill["locked"] for skill in skills),
                                "total": len(skills), "skills": skills,
                                "next_unlock": next_unlock(category, owned)}
    return {"totals": category_totals(rows), "categories": categories,
            "recent": workshop_recent(recent_rows)}
