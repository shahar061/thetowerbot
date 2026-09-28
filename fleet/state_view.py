"""The Fleet State page: one live column per emulator account.

Every `build_*` function takes plain values - a revision's JSON, ledger rows
SQL has already bounded or summed, a worker's /api/status dict - and returns
a JSON-safe dict, so each one is tested on literals without a live worker. A
value nobody observed is None, never 0 and never a guess.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import cards
import lab_catalog
import upgrades
import workshop_levels
from account_state import completed_lab_level
from concepts import REGISTRY
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


def _whole(value: object) -> int | None:
    return value if type(value) is int else None


def _number(value: object) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_bot(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """Screen, current activity ("Now") and whether a battle is live."""
    if status is None:
        return {"screen": None, "now": None, "live": False}
    screen = status.get("screen") if isinstance(status.get("screen"), str) else None
    activity = status.get("activity")
    now = activity.get("label") if isinstance(activity, Mapping) else None
    return {"screen": screen, "now": now if isinstance(now, str) else None,
            "live": screen == "IN_RUN"}


def build_decision(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The autopilot's latest decision. It carries no price, so `cost` stays None."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("phase"), str):
        return None
    upgrade_id = raw.get("upgrade_id") if isinstance(raw.get("upgrade_id"), str) else None
    upgrade = upgrades.by_id(upgrade_id) if upgrade_id else None
    return {"phase": raw["phase"], "reason": str(raw.get("reason") or ""),
            "upgrade_id": upgrade_id,
            "category": _category(upgrade.category) if upgrade else None,
            "name": upgrade.name if upgrade else None, "cost": None}


def build_battle(status: Mapping[str, Any] | None, tier: int | None,
                 best_waves: Mapping[int, int]) -> dict[str, Any] | None:
    """The live battle HUD, or None when the worker is not in a run."""
    if status is None or status.get("screen") != "IN_RUN":
        return None
    run = status.get("run") if isinstance(status.get("run"), Mapping) else {}
    return {"tier": tier, "wave": _whole(status.get("wave")),
            "cash": _number(status.get("wallet")), "elapsed_s": _number(run.get("elapsed")),
            "best_wave": best_waves.get(tier) if tier is not None else None}


def build_balances(overview: Mapping[str, Any] | None,
                   ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    """Coins and gems from the live scope when fresh, else the ledger's last reading.

    Stones have no reader yet, so they are always None.
    """
    overview, ledger = overview or {}, ledger or {}
    coins = overview.get("coins_lower")
    gems = overview.get("gems")
    return {"coins": coins if coins is not None else ledger.get("coins"),
            "gems": gems if gems is not None else ledger.get("gems"),
            "stones": None}


def build_runs(rows: Iterable[Mapping[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """The newest finished runs, newest first."""
    runs = []
    for row in list(rows)[:limit]:
        started, ended = _number(row.get("started_at")), _number(row.get("ended_at"))
        runs.append({"tier": row.get("tier"), "wave": row.get("wave"), "coins": row.get("coins"),
                     "duration_s": round(ended - started, 1)
                     if started is not None and ended is not None else None,
                     "ended_at": iso(ended), "abandoned": bool(row.get("abandoned"))})
    return runs


def build_run_upgrades(rows: Iterable[Mapping[str, Any]], scope: str | None) -> dict[str, Any] | None:
    """In-run upgrade levels bought in the current run, or the last one.

    None only when there is no run to describe. A run that bought nothing is
    a real answer: total 0 and no items.
    """
    if scope is None:
        return None
    by_category = dict.fromkeys(CATEGORIES, 0)
    items = []
    for row in rows:
        upgrade_id, levels = row.get("upgrade_id"), row.get("levels")
        if not isinstance(upgrade_id, str) or not upgrade_id or type(levels) is not int or levels <= 0:
            continue
        upgrade = upgrades.by_id(upgrade_id)
        category = _category(upgrade.category) if upgrade else None
        if category is not None:
            by_category[category] += levels
        items.append({"id": upgrade_id, "name": upgrade.name if upgrade else upgrade_id,
                      "category": category, "levels": levels})
    items.sort(key=lambda item: (-item["levels"], item["name"]))
    return {"scope": scope, "total": sum(item["levels"] for item in items),
            "by_category": by_category, "items": items}


def build_cards(revision: Mapping[str, Any] | None, gems_invested: int | None,
                recent_rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Card slots, per-card level and copies, gems spent and recent card buys."""
    facts = {fact.get("concept_id"): fact.get("value")
             for fact in (revision or {}).get("cards") or ()}
    capacity = _whole(facts.get(cards.SLOT_CAPACITY_KEY))
    items: dict[str, dict[str, Any]] = {}
    for key, value in facts.items():
        if not isinstance(key, str) or not key.startswith("cards.") or key.count(".") != 2:
            continue
        concept_id, _, field = key.rpartition(".")
        if field not in ("level", "copies"):
            continue
        concept = REGISTRY.by_id(concept_id)
        entry = items.setdefault(concept_id, {"name": concept.name if concept else concept_id,
                                              "level": None, "copies": None})
        entry[field] = _whole(value)
    return {"slots": {"equipped": _whole(facts.get(cards.SLOT_EQUIPPED_KEY)), "capacity": capacity,
                      "next_slot_gems": lab_catalog.card_slot_gems(capacity + 1)
                      if capacity is not None else None},
            "items": sorted(items.values(), key=lambda item: item["name"]),
            "gems_invested": gems_invested,
            "recent": [{"ts": iso(row.get("ts")), "name": str(row.get("item") or "Card"),
                        "gems": row.get("price")} for row in list(recent_rows)[:RECENT_LIMIT]]}


def _lab_name(concept_id: str) -> str:
    concept = REGISTRY.by_id(concept_id)
    if concept is not None:
        return concept.name
    entry = lab_catalog.lab(concept_id)
    return entry.name if entry is not None else concept_id


def build_labs(revision: Mapping[str, Any] | None, recent_rows: Iterable[Mapping[str, Any]],
               now: float) -> dict[str, Any]:
    """Lab slots, running jobs, known levels with the next price, and the next lab."""
    revision = revision or {}
    known: dict[str, int | None] = {}
    levels = []
    for fact in revision.get("lab_levels") or ():
        concept_id = fact.get("concept_id")
        if not isinstance(concept_id, str):
            continue
        level = completed_lab_level(fact.get("status"), fact.get("value"))
        known[concept_id] = level
        step = lab_catalog.level(concept_id, level + 1) if level is not None else None
        levels.append({"id": concept_id, "name": _lab_name(concept_id), "level": level,
                       "next_cost": step.coins if step is not None else None})
    running = []
    for fact in revision.get("lab_jobs") or ():
        concept_id, completes = fact.get("concept_id"), _number(fact.get("value"))
        # A job past its completion time has finished; the revision just has
        # not been re-read since. Listing it would show a timer below zero.
        if not isinstance(concept_id, str) or completes is None or completes <= now:
            continue
        level = known.get(concept_id)
        running.append({"id": concept_id, "name": _lab_name(concept_id),
                        "to_level": level + 1 if level is not None else None,
                        "completes_at": iso(completes)})
    busy = {job["id"] for job in running}
    priced = [row for row in levels if row["id"] not in busy and row["next_cost"] is not None]
    upcoming: dict[str, Any] | None = None
    if priced:
        cheapest = min(priced, key=lambda row: row["next_cost"])
        upcoming = {"id": cheapest["id"], "name": cheapest["name"], "cost": cheapest["next_cost"]}
    else:
        entry = next((lab for lab in lab_catalog.CATALOG.labs if lab.id not in busy), None)
        if entry is not None:
            step = lab_catalog.level(entry.id, (known.get(entry.id) or 0) + 1)
            upcoming = {"id": entry.id, "name": entry.name,
                        "cost": step.coins if step is not None else None}
    return {"slots": _whole(revision.get("lab_slots_owned")), "running": running,
            "levels": levels, "next": upcoming,
            "recent": [{"ts": iso(row.get("ts")),
                        "name": _lab_name(item) if isinstance(item := row.get("item"), str)
                        and item.startswith("labs.") else str(item or "Lab"),
                        "price": row.get("price")} for row in list(recent_rows)[:RECENT_LIMIT]]}
