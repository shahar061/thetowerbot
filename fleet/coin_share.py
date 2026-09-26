"""How Workshop and Labs share one coin wallet, and the jar that holds lab savings.

Every Workshop coin ceiling - in the worker, in previews and in block
programs - is computed here, so they cannot disagree.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from fleet.build_route_store import _write_json_atomic

logger = logging.getLogger(__name__)
JAR_FILE = "lab-coin-jar.json"


def workshop_limit_pct(route: Any) -> int:
    rules = getattr(route, "rules", None)
    return (rules.coins.workshop_spend_limit_pct if rules is not None
            else route.workshop.coin_spend_limit_pct)


def spendable_wallet(wallet: int, jar: int) -> int:
    return max(0, wallet - max(0, jar))


def workshop_ceiling(route: Any, wallet: int, jar: int) -> int:
    """(wallet − jar) × Workshop spend limit, never below zero."""
    return spendable_wallet(wallet, jar) * workshop_limit_pct(route) // 100


def grow_jar(jar: int, wallet: int, price: int, pct: int) -> int:
    jar = max(0, jar)
    return min(price, jar + pct * max(0, wallet - jar) // 100)


def waiting_lab_price(route: Any, lab_record: Mapping[str, Any] | None) -> int | None:
    """The price of the automated lab (slot-1 Game Speed) waiting for coins."""
    rules = getattr(route, "rules", None)
    if rules is None or not rules.labs.auto_start or lab_record is None:
        return None
    if lab_record.get("kind") != "wait_coins":
        return None
    price = lab_record.get("price")
    return price if type(price) is int and price > 0 else None


def workshop_paused(route: Any, lab_record: Mapping[str, Any] | None,
                     wallet: int | None = None) -> bool:
    """labs_first: Workshop waits while the wallet can't yet cover the
    automated lab's price - the same condition the UI's splitPreview uses,
    so the worker and the preview never disagree about "paused"."""
    rules = getattr(route, "rules", None)
    price = waiting_lab_price(route, lab_record)
    return (rules is not None and rules.coins.lab_share.mode == "labs_first"
            and price is not None and (wallet is None or wallet < price))


class LabCoinJar:
    """Coins Workshop may not spend, saved toward the next automated lab.

    One account's amount in lab-coin-jar.json beside the cadence files. Any
    unreadable state counts as an empty jar: the safe direction, in which
    Workshop behaves exactly as it did before the jar existed.
    """

    def __init__(self, root: Path, account_id: str, *, read_only: bool = False) -> None:
        self.path = Path(root) / JAR_FILE
        self.account_id = account_id
        self.read_only = read_only

    def _record(self, *, quiet: bool = False) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not quiet:
                logger.info("Lab coin jar %s is missing; treating it as 0 coins", self.path)
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Lab coin jar %s is unreadable (%s); treating it as 0 coins", self.path, exc)
            return None
        if (not isinstance(data, dict) or data.get("account_id") != self.account_id
                or type(data.get("amount")) is not int or data["amount"] < 0):
            logger.warning("Lab coin jar %s belongs to another account or is invalid; "
                           "treating it as 0 coins", self.path)
            return None
        return data

    def amount(self, *, quiet: bool = False) -> int:
        record = self._record(quiet=quiet)
        return record["amount"] if record is not None else 0

    def _write(self, amount: int, visit_key: str | None, now: float) -> None:
        if self.read_only:
            return
        _write_json_atomic(self.path, {"account_id": self.account_id, "amount": amount,
                                       "visit_key": visit_key, "updated_at": now})

    def settle(self, route: Any, lab_record: Mapping[str, Any] | None,
               wallet: int | None, visit_key: str, now: float) -> int:
        """The jar for this Workshop visit; grows at most once per visit key."""
        price = waiting_lab_price(route, lab_record)
        if route.rules.coins.lab_share.mode != "save_pct" or price is None:
            record = self._record(quiet=True)
            if record is not None and record["amount"] != 0:
                self._write(0, record.get("visit_key"), now)
            return 0
        record = self._record()
        amount = min(price, record["amount"]) if record is not None else 0
        if record is not None and record.get("visit_key") == visit_key:
            return amount
        if type(wallet) is not int or wallet < 0:
            return amount
        amount = grow_jar(amount, wallet, price, route.rules.coins.lab_share.pct)
        self._write(amount, visit_key, now)
        return amount

    def reset(self, now: float) -> None:
        """After the lab's confirmed coin debit, the savings are spent."""
        record = self._record(quiet=True)
        self._write(0, record.get("visit_key") if record is not None else None, now)
