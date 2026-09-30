"""Replay confirmed Workshop actions relative to observations, never from level zero.

The planner and Fleet State share the same quote invalidation rules. SQL
returns at most one row per catalog upgrade even when a worker has a long
ledger; callers retain their own connection timeout and account verification.
"""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

import upgrades
from fleet.workshop_prices import CATALOG, PriceQuote, WorkshopPrices, catalog_price

CHEAPEST_WORKSHOP_PRICE = min(price for upgrade in CATALOG["upgrades"].values()
                              for price in upgrade.get("next_coins", []) if price > 0)


def discount_signature(revision: Mapping[str, Any] | None,
                       scope: Mapping[str, Any] | None, *, now: float) -> str:
    """Only fresh, scoped facts can establish that all three discounts are zero."""
    levels = (revision or {}).get("lab_levels")
    if not isinstance(levels, (list, tuple)):
        return "unknown"
    facts = [fact for fact in levels if isinstance(fact, Mapping)]
    expected = {f"labs.workshop-{category}-discount" for category in ("attack", "defense", "utility")}
    zero = set()
    for fact in facts:
        evidence = fact.get("evidence")
        observed = evidence.get("observed_at") if isinstance(evidence, Mapping) else None
        if (scope is not None and fact.get("scope") == scope
                and ((fact.get("status") == "verified" and fact.get("value") == 0)
                     or (fact.get("status") == "available" and fact.get("value") == 1))
                and type(observed) in (int, float) and 0 <= now - observed <= 30):
            zero.add(fact.get("concept_id"))
    if zero == expected:
        return "none"
    discounts = sorted((fact.get("concept_id"), fact.get("status"), fact.get("value"))
                       for fact in facts if "workshop-" in str(fact.get("concept_id"))
                       and "discount" in str(fact.get("concept_id")))
    return json.dumps(discounts) if discounts else "unknown"


@dataclass(frozen=True)
class WorkshopActions:
    price_purchases: Mapping[str, int]
    level_purchases: Mapping[str, int]
    invalidated: Mapping[str, float]
    changed_at: float

    def stale(self, upgrade_id: str, observed_at: float | None) -> bool:
        cutoff = max(self.changed_at, self.invalidated.get(upgrade_id, 0))
        return cutoff > 0 and (observed_at is None or observed_at < cutoff)


@dataclass(frozen=True)
class WorkshopEvidence:
    actions: WorkshopActions
    quotes: Mapping[str, PriceQuote]
    quote_ids: frozenset[str]


def read_actions(conn: sqlite3.Connection, memory: WorkshopPrices, *, now: float,
                 level_anchors: Mapping[str, float] | None = None) -> WorkshopActions:
    """Aggregate receipt offsets and uncertainties on the caller's account-bound DB."""
    anchors = {uid: {"price_at": entry["observed_at"]} for uid, entry in memory.entries.items()}
    for uid, observed in (level_anchors or {}).items():
        if type(observed) in (int, float) and math.isfinite(observed):
            anchors.setdefault(uid, {})["level_at"] = observed
    wallet = memory.wallet
    earliest = min([at for value in anchors.values() for at in value.values()]
                   + ([wallet["observed_at"]] if wallet else [now]))

    def upgrade_id(item: object, category: object) -> str | None:
        row = upgrades.resolve(item, category)  # type: ignore[arg-type]
        return row.id if row is not None else None

    conn.create_function("workshop_upgrade_id", 2, upgrade_id, deterministic=True)
    rows = conn.execute("""
        WITH history AS (
            SELECT workshop_upgrade_id(item,category) AS uid, ts, kind, reason,
                   CASE WHEN json_valid(detail) THEN json_extract(detail,'$.verdict') END AS verdict
            FROM ledger WHERE dry_run=0 AND ts>=? AND (
                (kind='WORKSHOP_BUY' AND (currency='coins' OR currency IS NULL))
                OR (kind='BUY_SKIPPED' AND reason='unconfirmed'))
        ), anchors AS (SELECT key AS uid, value FROM json_each(?))
        SELECT history.uid,
               SUM(CASE WHEN kind='WORKSHOP_BUY' AND verdict IN ('bought','free')
                    AND ts>json_extract(anchors.value,'$.price_at') THEN 1 ELSE 0 END) AS price_purchases,
               SUM(CASE WHEN kind='WORKSHOP_BUY' AND verdict IN ('bought','free')
                    AND ts>json_extract(anchors.value,'$.level_at') THEN 1 ELSE 0 END) AS level_purchases,
               MAX(CASE WHEN (kind='BUY_SKIPPED' AND reason='unconfirmed')
                    OR (kind='WORKSHOP_BUY' AND COALESCE(verdict,'') NOT IN ('bought','free'))
                    THEN ts ELSE 0 END) AS invalidated
        FROM history LEFT JOIN anchors ON history.uid=anchors.uid
        WHERE history.uid IS NOT NULL GROUP BY history.uid
        """, (earliest, json.dumps(anchors))).fetchall()
    # A reconciliation of this wallet frame invalidates older prices, not
    # quotes observed on the same frame. Small rounding gaps move no price.
    at = wallet["observed_at"] if wallet else None
    changed = conn.execute("""
        SELECT MAX(CASE WHEN observed=? AND ts>=? AND ts-?<2 THEN ? ELSE ts END)
        FROM ledger WHERE dry_run=0 AND ts>=? AND kind='UNEXPLAINED'
            AND currency='coins' AND delta<=?
        """, (wallet["coins"] if wallet else None, at, at, at, earliest,
              -CHEAPEST_WORKSHOP_PRICE)).fetchone()[0]
    return WorkshopActions(
        {row[0]: row[1] for row in rows}, {row[0]: row[2] for row in rows},
        {row[0]: row[3] for row in rows if row[3] > 0}, changed or 0.)


def replay_quotes(memory: WorkshopPrices, actions: WorkshopActions,
                  purchases: Mapping[str, int], *, signature: str) -> dict[str, PriceQuote]:
    offsets = {uid: entry["purchases"] + actions.price_purchases.get(uid, 0)
               for uid, entry in memory.entries.items()}
    quotes = memory.quotes(offsets, invalidated=actions.invalidated,
                           changed_at=actions.changed_at, discount_signature=signature)
    # Preserve the planner's calibrated starter-unlock estimates. These are
    # planning hints, never evidence that a tile or its children are owned.
    for uid in ("unlock_cash_bonuses", "unlock_coin_bonuses", "unlock_defense_upgrades", "unlock_thorns"):
        if uid not in quotes and uid not in actions.invalidated and not purchases.get(uid):
            price = catalog_price(uid, 0)
            if price is not None:
                quotes[uid] = PriceQuote(price, 0, "catalog_estimate")
    return quotes
