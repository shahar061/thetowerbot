"""Account-bound Workshop observations and deliberately labelled price estimates.

Bot purchase counts are offsets from a price observation, never starting levels.
The small attributed catalog has coin prices only; the buyer still reads the game.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from uuid import uuid4

import upgrades

CATALOG = json.loads((Path(__file__).resolve().parents[1] / "catalog" /
                     "workshop-prices.v1.json").read_text(encoding="utf-8"))


def catalog_price(upgrade_id: str, level: int) -> int | None:
    prices = CATALOG["upgrades"].get(upgrade_id, {}).get("next_coins", [])
    return prices[level] if type(level) is int and 0 <= level < len(prices) else None


@dataclass(frozen=True)
class PriceQuote:
    price: int
    level: int | None
    source: str


class WorkshopPrices:
    def __init__(self, root: Path, account_id: str) -> None:
        self.path = root / "workshop-prices.json"
        self.account_id = account_id
        self.entries: dict[str, dict] = {}
        self.wallet: dict | None = None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if (payload.get("version") == 1 and payload.get("account_id") == account_id
                    and payload.get("catalog_revision") == CATALOG["source_revision"]):
                for uid, entry in payload.get("entries", {}).items():
                    if (upgrades.by_id(uid) is not None and isinstance(entry, dict)
                            and type(entry.get("price")) is int and entry["price"] >= 0
                            and type(entry.get("purchases")) is int and entry["purchases"] >= 0
                            and isinstance(entry.get("observed_at"), (int, float))
                            and math.isfinite(entry["observed_at"])
                            and isinstance(entry.get("discount_signature"), str)):
                        self.entries[uid] = entry
                wallet = payload.get("wallet")
                if (isinstance(wallet, dict) and type(wallet.get("coins")) is int
                        and wallet["coins"] >= 0 and type(wallet.get("run_id")) is int
                        and isinstance(wallet.get("observed_at"), (int, float))
                        and math.isfinite(wallet["observed_at"])):
                    self.wallet = wallet
        except (OSError, ValueError, TypeError, AttributeError):
            self.entries = {}
            self.wallet = None

    def observe(self, upgrade_id: str, price: int | None, purchases: int,
                *, now: float, discount_signature: str = "unknown") -> None:
        if (upgrades.by_id(upgrade_id) is None or type(price) is not int or price < 0
                or type(purchases) is not int or purchases < 0 or not math.isfinite(now)):
            return
        self.entries[upgrade_id] = {"price": price, "purchases": purchases,
                                    "observed_at": now,
                                    "discount_signature": discount_signature}

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
            prices = CATALOG["upgrades"].get(uid, {}).get("next_coins", [])
            matches = [level for level, price in enumerate(prices) if price == entry["price"]]
            # A base-price match is an inferred level, not an account fact.
            # Discounted/nonmatching observations remain usable at this level
            # but cannot safely predict the next one.
            level = matches[0] if len(matches) == 1 and discount_signature in {"unknown", "none"} else None
            if delta == 0:
                quotes[uid] = PriceQuote(entry["price"], level, "observed")
            elif level is not None and (price := catalog_price(uid, level + delta)) is not None:
                quotes[uid] = PriceQuote(price, level + delta, "catalog_estimate")
        return quotes

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"version": 1, "account_id": self.account_id,
                           "catalog_revision": CATALOG["source_revision"],
                           "entries": self.entries, "wallet": self.wallet}, output)
                output.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
