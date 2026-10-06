"""Account-bound Workshop observations and deliberately labelled price estimates.

Bot purchase counts are offsets from a price observation, never starting levels.
The full ladder adapter supplies coin planning prices; settlement retains its
original narrow whitelist and the buyer still reads the game.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

import upgrades
import workshop_levels

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "catalog" /
                     "workshop-prices.v1.json").read_text(encoding="utf-8"))
# The full ladder serializes rounded source prices as integers. Only the
# attributed exact-prefix table retains price precision; match it by rung.
_PRECISION_DIGEST = hashlib.sha256(json.dumps(CATALOG["upgrades"],
    sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]
CATALOG_REVISION = f"{CATALOG['source_revision']}:precision-exact-prefix-v1:{_PRECISION_DIGEST}"



def _prices(upgrade_id: str) -> tuple[int | None, ...]:
    ladder = workshop_levels.ladders().get(upgrade_id)
    return (ladder.next_coins if ladder is not None else
            tuple(CATALOG["upgrades"].get(upgrade_id, {}).get("next_coins", [])))


def catalog_price(upgrade_id: str, level: int) -> int | None:
    prices = _prices(upgrade_id)
    value = prices[level] if type(level) is int and 0 <= level < len(prices) else None
    exact = CATALOG["upgrades"].get(upgrade_id, {}).get("next_coins", [])
    # Never recover source precision from integer storage or numeric magnitude.
    return (value if type(level) is int and 0 <= level < len(exact)
            and type(value) is int and value == exact[level] else None)


def lists_price(upgrade_id: str, price: int) -> bool:
    """Historical settlement whitelist; the full planning ladder is not proof.

    Existing journal/repair callers rely on this narrower attributed list.
    Adding inferred planning rungs must never widen their spending authority.
    """
    return price in CATALOG["upgrades"].get(upgrade_id, {}).get("next_coins", [])


@dataclass(frozen=True)
class PriceQuote:
    price: int
    level: int | None
    source: str
    catalog_revision: str = CATALOG_REVISION
    level_confidence: str = "unknown"
    modifier_signature: str = "unknown"
    currency: str = "coins"
    context: str = "workshop"

    @property
    def lower(self) -> int:
        from transactions import abbreviation_slack
        return max(0, self.price - (abbreviation_slack(self.price) if self.source == "observed" else 0))

    @property
    def upper(self) -> int:
        from transactions import abbreviation_slack
        return self.price + (abbreviation_slack(self.price) if self.source == "observed" else 0)

    def for_execution(self, *, context: str, currency: str) -> int | None:
        """A quote is planning evidence, never cross-currency authority."""
        if (context != self.context or currency != self.currency
                or self.lower != self.upper or type(self.level) is not int or self.level < 0
                or self.level_confidence != "exact" or self.modifier_signature != "none"):
            return None
        return self.price


class WorkshopPrices:
    def __init__(self, root: Path, account_id: str) -> None:
        self.path = root / "workshop-prices.json"
        self.account_id = account_id
        self.entries: dict[str, dict] = {}
        # Rows read as MAX, with the purchase count at that read. Terminal
        # evidence: a maxed row shows no price, so no read can ever quote it.
        self.maxed: dict[str, dict] = {}
        self.wallet: dict | None = None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (payload.get("version") == 1 and payload.get("account_id") == account_id
                    and payload.get("catalog_revision") == CATALOG_REVISION):
                for uid, entry in payload.get("entries", {}).items():
                    if (upgrades.by_id(uid) is not None and isinstance(entry, dict)
                            and type(entry.get("price")) is int and entry["price"] >= 0
                            and type(entry.get("purchases")) is int and entry["purchases"] >= 0
                            and isinstance(entry.get("observed_at"), (int, float))
                            and math.isfinite(entry["observed_at"])
                            and isinstance(entry.get("discount_signature"), str)):
                        self.entries[uid] = entry
                maxed = payload.get("maxed")
                for uid, entry in (maxed.items() if isinstance(maxed, dict) else ()):
                    if (upgrades.by_id(uid) is not None and isinstance(entry, dict)
                            and type(entry.get("purchases")) is int and entry["purchases"] >= 0
                            and isinstance(entry.get("observed_at"), (int, float))
                            and math.isfinite(entry["observed_at"])):
                        self.maxed[uid] = entry
                wallet = payload.get("wallet")
                if (isinstance(wallet, dict) and type(wallet.get("coins")) is int
                        and wallet["coins"] >= 0 and type(wallet.get("run_id")) is int
                        and isinstance(wallet.get("observed_at"), (int, float))
                        and math.isfinite(wallet["observed_at"])):
                    self.wallet = wallet
        except (OSError, ValueError, TypeError, AttributeError):
            self.entries = {}
            self.maxed = {}
            self.wallet = None

    def observe(self, upgrade_id: str, price: int | None, purchases: int,
                *, now: float, discount_signature: str = "unknown") -> None:
        if (upgrades.by_id(upgrade_id) is None or type(price) is not int or price < 0
                or type(purchases) is not int or purchases < 0 or not math.isfinite(now)):
            return
        self.entries[upgrade_id] = {"price": price, "purchases": purchases,
                                    "observed_at": now,
                                    "discount_signature": discount_signature}
        # A priced row is not maxed (for example after a raised max level).
        self.maxed.pop(upgrade_id, None)

    def observe_maxed(self, upgrade_id: str, purchases: int, *, now: float) -> None:
        if (upgrades.by_id(upgrade_id) is None or type(purchases) is not int
                or purchases < 0 or not math.isfinite(now)):
            return
        self.maxed[upgrade_id] = {"purchases": purchases, "observed_at": now}
        # Any earlier price is for a level the row has already passed.
        self.entries.pop(upgrade_id, None)

    def maxed_ids(self, purchases: Mapping[str, int]) -> tuple[str, ...]:
        """Maxed reads no confirmed buy or ledger change has contradicted since.

        A buy after the read means the row was not maxed; a lower count means
        the purchase history changed under it (for example a reset). Either
        way the row is read again rather than trusted.
        """
        return tuple(sorted(uid for uid, entry in self.maxed.items()
                            if purchases.get(uid, 0) == entry["purchases"]))

    def quotes(self, purchases: Mapping[str, int], *,
               invalidated: Mapping[str, float] | None = None,
               changed_at: float = 0,
               discount_signature: str = "unknown") -> dict[str, PriceQuote]:
        quotes = {}
        for uid, entry in self.entries.items():
            if (entry["observed_at"] < max(changed_at, (invalidated or {}).get(uid, 0))
                    or entry["discount_signature"] != discount_signature):
                continue
            delta = purchases.get(uid, 0) - entry["purchases"]
            if delta < 0:
                continue
            prices = _prices(uid)
            matches = [level for level, price in enumerate(prices) if price == entry["price"] and catalog_price(uid, level) == price]
            # A base-price match is an inferred level, not an account fact.
            # Discounted/nonmatching observations remain usable at this level
            # but cannot safely predict the next one.
            level = (matches[0] if len(matches) == 1 and entry["price"] < 1000
                     and discount_signature in {"unknown", "none"} else None)
            if delta == 0:
                quotes[uid] = PriceQuote(entry["price"], level, "observed", level_confidence=("exact" if discount_signature == "none" else "inferred") if level is not None else "unknown", modifier_signature=discount_signature)
            elif level is not None and (price := catalog_price(uid, level + delta)) is not None:
                quotes[uid] = PriceQuote(price, level + delta, "catalog_estimate", level_confidence="exact" if discount_signature == "none" else "inferred", modifier_signature=discount_signature)
        return quotes

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"version": 1, "account_id": self.account_id,
                           "catalog_revision": CATALOG_REVISION,
                           "entries": self.entries, "maxed": self.maxed,
                           "wallet": self.wallet}, output)
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
