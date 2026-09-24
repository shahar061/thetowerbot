"""Pure slot-one research choice and account-bound revisit timing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import config
from lab_screen import LabHomeReading, LabPickerReading


@dataclass(frozen=True)
class LabDecision:
    kind: str
    price: int | None = None
    wallet_coins: int | None = None
    job_completes_at: float | None = None
    game_speed_level: int | None = None


def decide(slot: LabHomeReading, row: LabPickerReading | None) -> LabDecision:
    """Never produce a start without a fresh, affordable, enabled row."""
    if not slot.page:
        return LabDecision("unknown")
    if slot.slot_status == "locked":
        return LabDecision("wait_unlock")
    if slot.slot_status == "researching":
        if slot.job is None or slot.job.status != "researching":
            return LabDecision("unknown")
        return LabDecision("wait_running", job_completes_at=slot.job.completes_at)
    if slot.slot_status != "idle":
        return LabDecision("unknown")
    if row is None:
        return LabDecision("inspect")
    if not row.page or row.game_speed is None or row.game_speed.concept_id != "labs.game-speed":
        return LabDecision("unknown")
    entry = row.game_speed
    if entry.status == "maxed" or (type(entry.level) is int
                                    and type(entry.max_level) is int
                                    and entry.level >= entry.max_level):
        return LabDecision("done", game_speed_level=entry.level)
    if type(entry.level) is not int or entry.level < 0:
        return LabDecision("unknown")
    if (entry.cost is None or not math.isfinite(entry.cost) or entry.cost < 0
            or not float(entry.cost).is_integer()):
        return LabDecision("unknown")
    price = int(entry.cost)
    wallet = row.coin_balance
    if type(wallet) is not int or wallet < 0:
        return LabDecision("unknown")
    if wallet < price:
        return LabDecision("wait_coins", price=price, wallet_coins=wallet,
                           game_speed_level=entry.level)
    if entry.status != "available" or row.buy_point is None:
        return LabDecision("unknown", price=price, wallet_coins=wallet)
    return LabDecision("start", price=price, wallet_coins=wallet,
                       game_speed_level=entry.level)


class LabCadence:
    """A restart-safe check clock bound to exactly one account."""

    def __init__(self, root: Path, account_id: str) -> None:
        self.path = Path(root) / "lab-slot1-cadence.json"
        self.account_id = account_id

    def _record(self) -> dict[str, object] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("account_id") != self.account_id:
            return None
        return data

    def due(self, now: float) -> bool:
        record = self._record()
        if record is None:
            return True
        # Older cadence files have no level evidence. Revisit a previously
        # readable picker once so the speed target can be recovered.
        if (record.get("kind") in {"wait_coins", "done"}
                and type(record.get("game_speed_level")) is not int):
            return True
        if record.get("kind") == "done":
            return False
        next_check = record.get("next_check_at")
        return not isinstance(next_check, (int, float)) or now >= next_check

    def speed_target(self) -> float:
        """Aim at x2.0 only after a later Game Speed row proves Lv.1 finished."""
        record = self._record()
        level = record.get("game_speed_level") if record is not None else None
        ceiling = 2.0 if type(level) is int and level >= 2 else 1.5
        return max(value for value in config.TARGET_SPEEDS if value <= ceiling)

    def note(self, decision: LabDecision, now: float) -> None:
        previous = self._record()
        prior_level = previous.get("game_speed_level") if previous is not None else None
        prior_level = prior_level if type(prior_level) is int and prior_level >= 1 else None
        new_level = (decision.game_speed_level if type(decision.game_speed_level) is int
                     and decision.game_speed_level >= 1 else None)
        observed_level = max((level for level in (prior_level, new_level)
                              if level is not None), default=None)
        if decision.kind == "done":
            next_check = None
        elif decision.kind == "wait_running" and decision.job_completes_at is not None:
            next_check = max(now + 60., min(now + 3600., decision.job_completes_at - 30.))
        elif decision.kind == "wait_unlock":
            next_check = now + 600.
        else:
            next_check = now + 300.
        payload = {"account_id": self.account_id, "kind": decision.kind,
                   "next_check_at": next_check, "observed_at": now,
                   "wallet_coins": decision.wallet_coins, "price": decision.price,
                   "game_speed_level": observed_level}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
