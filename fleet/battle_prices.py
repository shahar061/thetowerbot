"""Calibrated battle cash curves, separate from permanent Workshop coin prices."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from fleet.build_route_store import _write_json_atomic


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    return json.loads((Path(__file__).resolve().parent.parent / "catalog/battle-cash.v1.json").read_text())


def supported_ids() -> frozenset[str]:
    return frozenset(catalog()["curves"])


def price_matches(price: int, observed: int | None, raw: str | None = None) -> bool:
    """An abbreviated UI price is an interval, not an exact integer cost."""
    if type(observed) is not int:
        return False
    match = re.fullmatch(r'\s*\$?(\d+)(?:\.(\d+))?([KMBTqQ])\s*', raw or '')
    if not match:
        return price == observed
    unit = 10 ** ({'K': 3, 'M': 6, 'B': 9, 'T': 12, 'q': 15, 'Q': 18}[match[3]] - len(match[2] or ''))
    # Covers rounding and truncation. Multiple matching levels refuse calibration.
    return observed - unit < price < observed + unit


class BattlePrices:
    """A price index advances only with a persisted purchase receipt or calibration.

    DB runs may start mid-battle: even a new run must read prices initially.
    Across waves quotes remain useful lower bounds, but need reconciliation
    before execution because free upgrades can have raised unseen prices.
    """

    def __init__(self, root: Path, account_id: str, *, read_only: bool = False) -> None:
        self.path = root / "battle-price-state.json"
        self.account_id = account_id
        self.read_only = read_only
        self.run_id: int | None = None
        self.wave: int | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(self.path.read_text())
            if (data.get("account_id") == account_id
                    and data.get("catalog_revision") == catalog()["source_revision"]
                    and type(data.get("run_id")) is int and isinstance(data.get("rows"), dict)):
                self.run_id, self.rows = data["run_id"], data["rows"]
                self.wave = data.get("wave") if type(data.get("wave")) is int else None
        except (OSError, ValueError, TypeError):
            pass

    def update(self, *, run_id: int | None, wave: int | None,
               counts: Mapping[str, int] | None, rows: Mapping[str, Mapping[str, Any]],
               now: float, pending: bool = False) -> dict[str, dict[str, Any]]:
        if type(run_id) is not int or type(wave) is not int or wave < 1 or counts is None:
            return {}
        before = json.dumps((self.run_id, self.wave, self.rows), sort_keys=True)
        if self.run_id != run_id or self.wave is not None and wave < self.wave:
            self.run_id, self.rows = run_id, {}
        self.wave = wave
        curves = catalog()["curves"]
        quotes: dict[str, dict[str, Any]] = {}
        for uid, curve in curves.items():
            count = counts.get(uid, 0)
            if type(count) is not int or count < 0:
                continue
            state = self.rows.get(uid, {})
            if not isinstance(state, dict):
                state = {}
            if (type(state.get("count")) is not int or count < state["count"]
                    or state.get("status") not in {"available", "locked", "maxed"}
                    or state.get("status") == "available" and type(state.get("index")) is not int):
                state = {}
            row = rows.get(uid, {})
            seen = row.get("observed_at")
            fresh = (not pending and type(seen) in (int, float) and 0 <= now - seen <= 2
                     and seen > state.get("seen", -1))
            if fresh and row.get("status") in {"locked", "maxed"}:
                state = dict(status=row["status"], count=count, wave=wave, seen=seen, value=row.get("value"))
            elif fresh and row.get("value") is not None:
                price = row.get("price")
                if type(price) is int:
                    matches = [i for i, candidate in enumerate(curve) if price_matches(candidate, price, row.get("raw_price"))]
                    if len(matches) == 1:
                        state = dict(status="available", index=matches[0], count=count,
                                     wave=wave, seen=seen, value=row["value"])
                    else:
                        # Unknown price/discount/version: keep the observed
                        # path usable; do not extrapolate a different curve.
                        state = {}
                elif state.get("status") == "available":
                    delta = count - state["count"]
                    if row["value"] != state.get("value") and delta == 0:
                        state = {}  # manual/free upgrade or modifier; reconcile
                    elif delta > 0:
                        state = dict(state, index=state["index"] + delta, count=count,
                                     value=row["value"], seen=seen)
            if state:
                self.rows[uid] = state
            else:
                self.rows.pop(uid, None)
                continue
            quote = dict(state, account_id=self.account_id, run_id=run_id, source="model",
                         verified=state.get("wave") == wave or state["status"] in {"locked", "maxed"})
            if state["status"] == "available":
                index = state.get("index")
                if type(index) is not int:
                    self.rows.pop(uid, None)
                    continue
                index += count - state["count"]
                if not 0 <= index < len(curve):
                    self.rows.pop(uid, None)
                    continue  # exhausting the table is NOT observed MAX
                quote.update(index=index, price=curve[index])
            quotes[uid] = quote
        if not self.read_only and before != json.dumps((self.run_id, self.wave, self.rows), sort_keys=True):
            _write_json_atomic(self.path, dict(account_id=self.account_id, run_id=self.run_id,
                catalog_revision=catalog()["source_revision"], wave=self.wave, rows=self.rows))
        return quotes
