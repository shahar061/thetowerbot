"""Pure research choice and account-bound lab revisit timing."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from typing import Mapping
from uuid import uuid4

import config
from lab_screen import LabHomeReading, LabPickerReading


# Per-slot ownership a Labs visit can prove: an owned card or the "Unlock Nth lab" tile.
SLOT_STATES = frozenset({"locked", "owned"})


@dataclass(frozen=True)
class LabVisitOptions:
    """What one Labs visit may do. Nothing is unlocked unless a slot is named."""
    start_research: bool = True
    unlock_slots: tuple[int, ...] = ()
    keep_gems: int = 0


@dataclass(frozen=True)
class LabDecision:
    kind: str
    price: int | None = None
    wallet_coins: int | None = None
    job_completes_at: float | None = None
    game_speed_level: int | None = None
    slot: int = 1
    research_id: str = 'labs.game-speed'
    strategy_revision: int | None = None

    @property
    def target_level(self) -> int | None:
        return self.game_speed_level


def decide(slot: LabHomeReading, row: LabPickerReading | None, *,
           research_id: str = 'labs.game-speed') -> LabDecision:
    """Never produce a start without a fresh, affordable, enabled row."""
    if not slot.page:
        return LabDecision("unknown")
    if slot.slot_status == "locked":
        return LabDecision("wait_unlock")
    if slot.slot_status == "researching":
        if slot.job is None or slot.job.status != "researching":
            return LabDecision("unknown")
        return LabDecision("wait_running", job_completes_at=slot.job.completes_at,
                           slot=slot.job.slot, research_id=slot.job.concept_id or "")
    if slot.slot_status != "idle":
        return LabDecision("unknown")
    if row is None:
        return LabDecision("inspect")
    if not row.page or row.game_speed is None or row.game_speed.concept_id != research_id:
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
    """Restart-safe research and slot-two checks bound to one account."""

    def __init__(self, root: Path, account_id: str) -> None:
        self.path = Path(root) / "lab-slot1-cadence.json"
        self.slot2_path = Path(root) / "lab-slot2-cadence.json"
        self.slots_path = Path(root) / "lab-slots.json"
        self.unlock_path = Path(root) / "lab-unlock.json"
        self.account_id = account_id

    def _read(self, path: Path) -> dict[str, object] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("account_id") != self.account_id:
            return None
        return data

    def _record(self) -> dict[str, object] | None:
        return self._read(self.path)

    def unlocked(self) -> bool:
        record = self._read(self.unlock_path)
        return record is not None and record.get("status") == "unlocked"

    def locked(self) -> bool:
        record = self._read(self.unlock_path)
        return record is not None and record.get("status") == "locked"

    def note_locked(self, source: str, now: float) -> None:
        """Persist an explicit lock observation without undoing an unlock."""
        if source != "labs_tab":
            raise ValueError("unsupported Labs lock evidence source")
        previous = self._read(self.unlock_path)
        if previous is not None and previous.get("status") == "unlocked":
            return
        if previous is not None and previous.get("status") == "locked":
            return
        self._write(self.unlock_path, {
            "account_id": self.account_id,
            "status": "locked",
            "source": source,
            "observed_at": now,
        })

    def note_unlocked(self, source: str, now: float,
                      caption: str | None = None) -> None:
        """Persist a positive Labs-unlocked observation for this account."""
        if source not in {"unlock_card", "labs_tab", "labs_page"}:
            raise ValueError("unsupported Labs unlock evidence source")
        previous = self._read(self.unlock_path)
        if previous is not None and previous.get("status") == "unlocked":
            return
        payload: dict[str, object] = {
            "account_id": self.account_id,
            "status": "unlocked",
            "source": source,
            "observed_at": now,
        }
        if caption is not None:
            payload["caption"] = caption
        payload["confirmed_at"] = now
        self._write(self.unlock_path, payload)

    def due(self, now: float, wallet_coins: int | None = None) -> bool:
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
        if record.get("kind") == "wait_coins" and type(record.get("price")) is int:
            price = record["price"]
            if type(wallet_coins) is int:
                return wallet_coins >= price
            # A death screen has no trustworthy coin counter. Recheck only
            # occasionally if visits to the menu do not happen naturally.
            observed = record.get("observed_at")
            return isinstance(observed, (int, float)) and now >= observed + 3600
        next_check = record.get("next_check_at")
        return not isinstance(next_check, (int, float)) or now >= next_check

    @staticmethod
    def _slot_entry(raw: object) -> dict[str, object] | None:
        if not isinstance(raw, dict) or raw.get("status") not in SLOT_STATES:
            return None
        gems, observed = raw.get("wallet_gems"), raw.get("observed_at")
        return {"status": raw["status"],
                "wallet_gems": gems if type(gems) is int else None,
                "observed_at": (float(observed) if isinstance(observed, (int, float))
                                and not isinstance(observed, bool) else None)}

    def slot_records(self) -> dict[int, dict[str, object]]:
        """Slots 2-5 this account was seen owning or locked, from lab-slots.json.

        Until that file exists at all, an older lab-slot2-cadence.json stands in for
        slot 2. Once lab-slots.json exists, the legacy file is never read again - even
        when the new file turns out to be unreadable or another account's, which must
        not resurrect stale legacy state.
        """
        if not self.slots_path.exists():
            legacy = self._slot_entry(self._read(self.slot2_path))
            return {2: legacy} if legacy is not None else {}
        data = self._read(self.slots_path)
        if data is None:
            return {}
        raw = data.get("slots")
        records: dict[int, dict[str, object]] = {}
        for key, value in (raw.items() if isinstance(raw, dict) else ()):
            entry = self._slot_entry(value)
            if key in {"2", "3", "4", "5"} and entry is not None:
                records[int(key)] = entry
        return records

    def slot_status_map(self) -> dict[str, str]:
        """Slot 2-5 statuses only, string-keyed to survive a JSON facts snapshot."""
        return {str(slot): str(record["status"]) for slot, record in self.slot_records().items()}

    def slot_owned(self, slot: int) -> bool:
        record = self.slot_records().get(slot)
        return record is not None and record["status"] == "owned"

    def slot_due(self, slot: int, now: float, wallet_gems: int | None = None,
                 min_gems: int | None = None) -> bool:
        """Revisit a locked slot once the gems cover it, else hourly. Never an owned slot."""
        record = self.slot_records().get(slot)
        if record is None:
            return True
        if record["status"] == "owned":
            return False
        if type(wallet_gems) is int and type(min_gems) is int:
            if wallet_gems < min_gems:
                return False
            previous = record.get("wallet_gems")
            if type(previous) is int and previous < min_gems:
                return True
        observed = record.get("observed_at")
        return not isinstance(observed, (int, float)) or now >= observed + 3600

    def note_slots(self, statuses: Mapping[int, str], wallet_gems: int | None, now: float) -> None:
        valid = {slot: status for slot, status in statuses.items()
                 if slot in (2, 3, 4, 5) and status in SLOT_STATES}
        if not valid:
            return
        records = self.slot_records()
        for slot, status in valid.items():
            records[slot] = {"status": status, "wallet_gems": wallet_gems, "observed_at": now}
        self._write(self.slots_path, {"account_id": self.account_id,
                                      "slots": {str(slot): records[slot] for slot in sorted(records)}})

    def route_observation(self) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        """Account-bound saved Lab decisions for the fleet route display."""
        return self._record(), self.slot_records().get(2)

    def speed_target(self) -> float:
        """The fastest readable speed the completed Game Speed research allows.

        The base ceiling is x1.5 and each research adds x0.5, up to x5.0. The
        row names the next level ("Lv.3" means two are done) until it is
        maxed, when it names the last one.
        """
        record = self._record()
        level = record.get("game_speed_level") if record is not None else None
        completed = 0
        if type(level) is int and level >= 1:
            completed = level if record.get("kind") == "done" else level - 1
        ceiling = 1.5 + .5 * completed
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
                   "game_speed_level": observed_level,
                   # Read by the Labs & Gems page; only a running job has one.
                   "job_completes_at": (decision.job_completes_at
                                        if decision.kind == "wait_running" else None)}
        self._write(self.path, payload)

    def backing_off(self, now: float) -> bool:
        """A failed visit holds every Labs check, research and slot unlock alike."""
        record = self._record()
        retry_at = record.get("retry_at") if record is not None else None
        return isinstance(retry_at, (int, float)) and now < retry_at

    def note_failed(self, now: float) -> None:
        """A visit that ended without reading slot 1 keeps the saved observation.

        The wait_coins price and Game Speed level still drive the lab coin hold;
        overwriting them with "unknown" would release it. Only the retry backs off.
        """
        if self._record() is None:
            self.note(LabDecision("unknown"), now)
        record = self._record()
        if record is not None:
            self._write(self.path, {**record, "retry_at": now + 300.})

    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(payload, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
