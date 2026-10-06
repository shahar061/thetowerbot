"""How Workshop and Labs share one coin wallet, and the jar that holds lab savings.

Every Workshop coin ceiling - in the worker, in previews and in block
programs - is computed here, so they cannot disagree.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from fleet.build_route_store import _write_json_atomic

logger = logging.getLogger(__name__)
JAR_FILE = "lab-coin-jar.json"
GAME_SPEED = "labs.game-speed"


@dataclass(frozen=True)
class SaveTarget:
    """The lab a save_pct jar saves toward: the slot-1 plan's target (or the one its filler
    saves for), else the slot-1 cadence's waiting Game Speed level."""
    lab_id: str
    level: int | None
    price: int


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


def cadence_target(route: Any, lab_record: Mapping[str, Any] | None) -> SaveTarget | None:
    """The slot-1 cadence's waiting Game Speed level, when no lab plan names a target."""
    price = waiting_lab_price(route, lab_record)
    if price is None or lab_record is None:
        return None
    level = lab_record.get("game_speed_level")
    return SaveTarget(GAME_SPEED, level if type(level) is int else None, price)


def workshop_paused(route: Any, lab_record: Mapping[str, Any] | None,
                     wallet: int | None = None) -> bool:
    """labs_first: Workshop waits while the wallet can't yet cover the
    automated lab's price - the same condition the UI's splitPreview uses,
    so the worker and the preview never disagree about "paused"."""
    rules = getattr(route, "rules", None)
    price = waiting_lab_price(route, lab_record)
    return (rules is not None and rules.coins.lab_share.mode == "labs_first"
            and price is not None and (wallet is None or wallet < price))


def jit_hold(saving: Any, wallet: int | None) -> tuple[int, bool, str | None]:
    """just_in_time: the coins Workshop leaves for labs, as `(jar, paused, reason)`.

    The hold rides the lab_coin_jar seam every Workshop ceiling subtracts. It covers
    the labs starting now (the wallet above the plan's wallet') and the reserve, so
    `workshop_ceiling(route, wallet, jar)` is the plan's `workshop_budget`. Paused when
    the hold takes the whole wallet, or when either wallet is unread (Workshop never
    spends blind). No saving plan (not a lab list) holds nothing. The reason is the
    plan's closing summary line ("Reserve ...; Workshop may spend ...", or "Wallet
    unread ..."), which speaks for every slot, not just one.
    """
    if saving is None:
        return 0, False, None
    reason = saving.why[-1] if saving.why else None
    if wallet is None:
        return 0, True, reason
    if saving.reserve is None or saving.wallet is None:
        return wallet, True, reason
    jar = max(0, wallet - max(0, saving.wallet) + saving.reserve)
    return jar, jar > 0 and jar >= wallet, reason


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

    def _write(self, amount: int, visit_key: str | None, now: float,
               target: Mapping[str, Any] | None = None) -> None:
        if self.read_only:
            return
        _write_json_atomic(self.path, {"account_id": self.account_id, "amount": amount,
                                       "visit_key": visit_key, "updated_at": now,
                                       **({"target": dict(target)} if target is not None else {})})

    @staticmethod
    def _stored_target(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
        target = record.get("target") if record is not None else None
        if (not isinstance(target, dict) or not isinstance(target.get("lab_id"), str)
                or not (target.get("level") is None or type(target.get("level")) is int)):
            return None
        return {"lab_id": target["lab_id"], "level": target.get("level")}

    def settle(self, route: Any, target: SaveTarget | None, wallet: int | None, visit_key: str,
               now: float, *, retired: Callable[[str, int | None], bool] | None = None) -> int:
        """The jar for this Workshop visit; grows at most once per visit key.

        save_pct only (other modes, or labs not auto-started, empty it). It grows by pct% of
        the wallet above it, up to `target`'s price. The returned (effective) jar never
        exceeds a known wallet; the stored one is not clamped to it. A new
        target starts from zero once `retired` says the old one is researching or finished.
        With no target known (an unread plan, a transient unknown cadence) it neither grows
        nor empties, unless `retired` says its stored target is researching or finished.
        """
        rules = route.rules
        if rules.coins.lab_share.mode != "save_pct" or not rules.labs.auto_start:
            record = self._record(quiet=True)
            if record is not None and record["amount"] != 0:
                self._write(0, record.get("visit_key"), now, self._stored_target(record))
            return 0
        record = self._record(quiet=target is None)
        if record is None and target is None:
            return 0
        amount = record["amount"] if record is not None else 0
        key = record.get("visit_key") if record is not None else None
        stored = self._stored_target(record)
        new_target = stored
        if target is None:
            if stored is not None and retired is not None and retired(stored["lab_id"], stored["level"]):
                amount = 0  # its target started or finished and nothing follows: release it
        else:
            new_target = {"lab_id": target.lab_id, "level": target.level}
            if (stored is not None and stored != new_target and retired is not None
                    and retired(stored["lab_id"], stored["level"])):
                amount = 0
            amount = min(amount, target.price)
            if key != visit_key and type(wallet) is int and wallet >= 0:
                amount = grow_jar(amount, wallet, target.price, rules.coins.lab_share.pct)
                key = visit_key
        if (record is None or amount != record["amount"] or key != record.get("visit_key")
                or new_target != stored):
            self._write(amount, key, now, new_target)
        # Only the effective jar is clamped to the wallet: a misread low wallet must not
        # destroy the stored savings; a real lab spend reduces them through `spend`.
        return min(amount, wallet) if type(wallet) is int and wallet >= 0 else amount

    def spend(self, coins: int, now: float) -> int:
        """A confirmed lab coin debit takes at most its own price from the jar.

        Labs may spend the whole wallet, jar included, so a cheap filler only
        dips into the savings; it never empties them. Returns the new amount.
        """
        record = self._record(quiet=True)
        if record is None:
            return 0
        amount = max(0, record["amount"] - max(0, coins))
        self._write(amount, record.get("visit_key"), now, self._stored_target(record))
        return amount
