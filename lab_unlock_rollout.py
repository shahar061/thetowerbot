"""Fleet-wide rollout of the bot's own lab-slot unlocks: dry run, canary, fleet.

One JSON file under the fleet root records, for each slot 2-5, how far the
unlock has been proven. Workers read it before a rehearsal or a tap. They
write it only under an exclusive file lock, and every promotion re-checks the
stage it expects while holding that lock, so a worker that loses a race simply
reads the newer record. A missing or corrupt file means every slot is back at
dry run, so nothing taps.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator, Mapping

import lab_catalog
from fleet.build_route_store import _write_json_atomic

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
SLOTS = (2, 3, 4, 5)
STAGES = ("dry_run", "canary", "fleet", "halted")
PROMOTION_REHEARSALS = 2
PROMOTION_SPACING_SECONDS = 600.
MAX_DRY_RUNS_PER_PAIR = 5
MAX_EVIDENCE = 32
OUTCOMES = ("bought", "not_charged")


class RolloutError(ValueError):
    """An owner action that does not apply to the slot's current stage."""


@dataclass(frozen=True)
class DryRun:
    worker: str
    at: float
    price: int
    gems: int
    account_id: str | None = None


def _qualifies(runs: list[DryRun]) -> bool:
    """Two rehearsals, same price and account, at least ten minutes apart."""
    return any(first.price == second.price and first.account_id == second.account_id
               and abs(second.at - first.at) >= PROMOTION_SPACING_SECONDS
               for index, first in enumerate(runs) for second in runs[index + 1:])


def _trim_dry_runs(runs: tuple[DryRun, ...]) -> tuple[DryRun, ...]:
    """Keep only the newest MAX_DRY_RUNS_PER_PAIR runs for each (worker, account) pair.

    A single shared cap would let a burst of rehearsals from many other workers push
    a worker's own first rehearsal out of the list before its second one ever lands,
    so a slot with a large, actively-rehearsing fleet could never promote. Capping
    per pair instead means one worker's rehearsals never evict another's.
    """
    seen: dict[tuple[str, str | None], int] = {}
    kept: list[DryRun] = []
    for run in reversed(runs):
        key = (run.worker, run.account_id)
        if seen.get(key, 0) >= MAX_DRY_RUNS_PER_PAIR:
            continue
        seen[key] = seen.get(key, 0) + 1
        kept.append(run)
    kept.reverse()
    return tuple(kept)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _whole(value: object, low: int) -> bool:
    return type(value) is int and value >= low


@dataclass(frozen=True)
class SlotRollout:
    stage: str = "dry_run"
    canary_worker: str | None = None
    canary_account: str | None = None
    dry_runs: tuple[DryRun, ...] = ()
    unlock: Mapping[str, Any] | None = None
    halted_reason: str | None = None
    evidence: tuple[str, ...] = ()

    def rehearsals(self, worker: str | None) -> int:
        """Clean rehearsals by `worker` counted toward promotion: 0, 1 or 2."""
        mine = [run for run in self.dry_runs if run.worker == worker]
        if not mine:
            return 0
        return PROMOTION_REHEARSALS if _qualifies(mine) else 1

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "canary_worker": self.canary_worker,
                "canary_account": self.canary_account,
                "dry_runs": [asdict(run) for run in self.dry_runs],
                "unlock": dict(self.unlock) if self.unlock is not None else None,
                "halted_reason": self.halted_reason, "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, raw: object) -> SlotRollout:
        if not isinstance(raw, dict):
            raise ValueError("slot entry must be an object")
        stage = raw.get("stage", "dry_run")
        canary, account = raw.get("canary_worker"), raw.get("canary_account")
        if (stage not in STAGES or canary is not None and not isinstance(canary, str)
                or account is not None and not isinstance(account, str)
                or stage == "canary" and not canary):
            raise ValueError("bad stage or canary")
        runs = []
        for item in raw.get("dry_runs") or []:
            if (not isinstance(item, dict) or not isinstance(item.get("worker"), str)
                    or not _finite(item.get("at")) or not _whole(item.get("price"), 1)
                    or not _whole(item.get("gems"), 0)
                    or item.get("account_id") is not None and not isinstance(item.get("account_id"), str)):
                raise ValueError("bad dry run")
            runs.append(DryRun(item["worker"], float(item["at"]), item["price"], item["gems"],
                               item.get("account_id")))
        unlock, reason, evidence = raw.get("unlock"), raw.get("halted_reason"), raw.get("evidence") or []
        if (unlock is not None and not isinstance(unlock, dict)
                or reason is not None and not isinstance(reason, str)
                or not isinstance(evidence, list) or any(not isinstance(path, str) for path in evidence)):
            raise ValueError("bad unlock, reason or evidence")
        return cls(stage, canary, account, tuple(runs), unlock, reason, tuple(evidence))


@dataclass(frozen=True)
class RolloutChange:
    slot: int
    before: SlotRollout
    after: SlotRollout

    @property
    def promoted(self) -> str | None:
        forward = {("dry_run", "canary"), ("canary", "fleet")}
        return self.after.stage if (self.before.stage, self.after.stage) in forward else None

    @property
    def halted(self) -> bool:
        return self.after.stage == "halted" and self.before.stage != "halted"


def _check_slot(slot: int) -> None:
    if slot not in SLOTS:
        raise ValueError(f"lab slot must be one of {SLOTS}")


def canary_present(fleet_root: Path, worker: str, account_id: str | None) -> bool | None:
    """Whether `worker` is still a pool member bound to `account_id`. None when unreadable."""
    from web.account_catalog import registered_worker
    try:
        members = json.loads((Path(fleet_root) / "reroll-pool.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    if not isinstance(members, list):
        return None
    if not any(isinstance(item, dict) and item.get("name") == worker for item in members):
        return False
    registration = registered_worker(Path(fleet_root) / "workers" / worker)
    if registration is None:
        return False
    return account_id is None or registration.account_id == account_id


def rollout_status(state: SlotRollout, slot: int, worker: str | None) -> str:
    """The Fleet State line for a worker whose next gem step unlocks `slot`."""
    if state.stage == "halted":
        return f"Halted: {state.halted_reason or 'no reason recorded'}"
    if state.stage == "canary":
        return (f"Canary: {state.canary_worker} unlocks slot {slot} next visit"
                if worker == state.canary_worker else "Waiting for canary")
    if state.stage == "fleet":
        return f"Unlocking slot {slot}"
    return f"Rehearsing slot {slot} · {state.rehearsals(worker)}/{PROMOTION_REHEARSALS} dry runs"


class LabUnlockRollout:
    """The shared rollout file under the fleet root, serialized by an OS file lock."""

    def __init__(self, fleet_root: Path) -> None:
        self.root = Path(fleet_root)
        self.path = self.root / "lab-unlock-rollout.json"
        self.lock_path = self.root / ".lab-unlock-rollout.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _corrupt(self, quarantine: bool, exc: Exception) -> dict[int, SlotRollout]:
        if quarantine:
            aside = self.path.with_name(f"{self.path.name}.corrupt-{time.time_ns()}")
            os.replace(self.path, aside)
            logger.warning("Lab unlock rollout %s is corrupt (%s); moved to %s; every slot "
                           "is back at dry run", self.path, exc, aside)
        else:
            logger.warning("Lab unlock rollout %s is corrupt (%s); reading every slot as dry run",
                           self.path, exc)
        return {}

    def _load(self, *, quarantine: bool) -> dict[int, SlotRollout]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except UnicodeDecodeError as exc:
            return self._corrupt(quarantine, exc)
        except OSError as exc:
            logger.warning("Lab unlock rollout %s unreadable (%s); every slot is at dry run", self.path, exc)
            return {}
        try:
            document = json.loads(text)
            if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported rollout schema")
            raw = document.get("slots")
            if not isinstance(raw, dict) or any(key not in {str(slot) for slot in SLOTS} for key in raw):
                raise ValueError("slots must be keyed 2-5")
            return {int(key): SlotRollout.from_dict(value) for key, value in raw.items()}
        except (ValueError, TypeError, AttributeError) as exc:
            return self._corrupt(quarantine, exc)

    def slots(self, *, quarantine: bool = True) -> dict[int, SlotRollout]:
        """Every slot 2-5. A dashboard read passes quarantine=False and never moves a file."""
        if quarantine:
            with self._locked():
                stored = self._load(quarantine=True)
        else:
            stored = self._load(quarantine=False)
        return {slot: stored.get(slot, SlotRollout()) for slot in SLOTS}

    def slot(self, slot: int) -> SlotRollout:
        _check_slot(slot)
        return self.slots()[slot]

    def may_tap(self, slot: int, worker: str) -> bool:
        state = self.slot(slot)
        return state.stage == "fleet" or state.stage == "canary" and state.canary_worker == worker

    def _update(self, slot: int, change: Callable[[SlotRollout], SlotRollout]) -> RolloutChange:
        _check_slot(slot)
        with self._locked():
            stored = self._load(quarantine=True)
            before = stored.get(slot, SlotRollout())
            after = change(before)
            if after != before:
                stored[slot] = after
                _write_json_atomic(self.path, {
                    "schema_version": SCHEMA_VERSION,
                    "slots": {str(key): value.to_dict() for key, value in sorted(stored.items())}})
        return RolloutChange(slot, before, after)

    def note_dry_run(self, slot: int, worker: str, price: int, gems: int, at: float, *,
                     account_id: str | None = None) -> RolloutChange:
        """Record one clean rehearsal. A price that disagrees with the catalog halts the slot."""
        catalog = lab_catalog.lab_slot_gems(slot)

        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "dry_run" or catalog is None:
                return current
            if price != catalog:
                return replace(current, stage="halted", halted_reason=(
                    f"Rehearsal read {price} gems for slot {slot}; the catalog says {catalog}"))
            runs = _trim_dry_runs((*current.dry_runs, DryRun(worker, float(at), price, gems, account_id)))
            mine = [run for run in runs
                    if run.worker == worker and run.account_id == account_id and run.price == price]
            if _qualifies(mine):
                return replace(current, stage="canary", canary_worker=worker,
                               canary_account=account_id, dry_runs=runs)
            return replace(current, dry_runs=runs)
        return self._update(slot, change)

    def note_unlock(self, slot: int, worker: str, transaction_key: str, outcome: str, *,
                    at: float) -> RolloutChange:
        """The canary's settled tap. Bought promotes to fleet; a second miss halts."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        record = {"worker": worker, "transaction_key": transaction_key, "outcome": outcome, "at": float(at)}

        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "canary" or current.canary_worker != worker:
                return current  # fleet stage, or another worker lost the race
            if outcome == "bought":
                return replace(current, stage="fleet", unlock=record)
            if current.unlock is not None and current.unlock.get("outcome") == "not_charged":
                return replace(current, stage="halted", unlock=record,
                               halted_reason="The canary's unlock tap did not land twice")
            return replace(current, unlock=record)
        return self._update(slot, change)

    def halt(self, slot: int, reason: str, evidence: tuple[str, ...] = ()) -> RolloutChange:
        def change(current: SlotRollout) -> SlotRollout:
            kept = (*current.evidence, *evidence)[-MAX_EVIDENCE:]
            if current.stage == "halted":
                return replace(current, evidence=kept)
            return replace(current, stage="halted", halted_reason=reason, evidence=kept)
        return self._update(slot, change)

    def release_canary(self, slot: int, *, expected_worker: str | None = None) -> RolloutChange:
        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "canary" or (expected_worker is not None
                                             and current.canary_worker != expected_worker):
                return current
            return SlotRollout()
        return self._update(slot, change)

    def release_absent_canary(self, slot: int) -> RolloutChange:
        """Back to dry run when the canary left the pool or now plays another account."""
        state = self.slot(slot)
        if (state.stage != "canary" or state.canary_worker is None
                or canary_present(self.root, state.canary_worker, state.canary_account) is not False):
            return RolloutChange(slot, state, state)
        return self.release_canary(slot, expected_worker=state.canary_worker)

    def reset(self, slot: int) -> RolloutChange:
        """The owner's action on a halted slot."""
        def change(current: SlotRollout) -> SlotRollout:
            if current.stage != "halted":
                raise RolloutError("slot_not_halted")
            return SlotRollout()
        return self._update(slot, change)

    def snapshot(self) -> list[dict[str, Any]]:
        """Dashboard rows for slots 2-5. This read never moves a corrupt file."""
        return [{"slot": slot, "stage": state.stage, "canary_worker": state.canary_worker,
                 "dry_runs": len(state.dry_runs), "price": lab_catalog.lab_slot_gems(slot),
                 "halted_reason": state.halted_reason, "evidence": list(state.evidence)}
                for slot, state in self.slots(quarantine=False).items()]
