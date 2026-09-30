"""Fleet-wide rollout of the bot's own research starts, per slot, plus per-lab rehearsals.

`start:N` walks dry run → canary → fleet like a slot unlock: two clean
rehearsals by one worker on one account, ten minutes apart, name that worker
the canary; its first proven start opens the slot to the fleet. Separately, a
lab may be started only after one clean rehearsal of it, anywhere, matched its
catalog price. Game Speed in slot 1 keeps its legacy route and needs neither.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import math
from pathlib import Path
from typing import Any, Mapping

import lab_catalog
from staged_rollout import (MAX_EVIDENCE, PROMOTION_SPACING_SECONDS, STAGES, LockedJsonFile,
                            RolloutError, canary_present)

__all__ = ["LabStarterRollout", "RolloutError", "StageRecord", "StarterState", "StarterGate",
           "price_matches", "starter_gate", "start_key"]

SCHEMA_VERSION = 1
START_KEYS = tuple(f"start:{slot}" for slot in range(1, 6))
LEGACY = (1, "labs.game-speed")
PROMOTION_REHEARSALS = 2
MAX_DRY_RUNS_PER_PAIR = 5
MISSES_TO_UNFINDABLE = 3
MISS_SPACING_SECONDS = 120.
OUTCOMES = ("bought", "not_charged")
LAB_STATUSES = ("rehearsed", "needs_review", "unfindable", "missing")


def start_key(slot: int) -> str:
    if type(slot) is not int or not 1 <= slot <= 5:
        raise ValueError("lab slot must be 1-5")
    return f"start:{slot}"


def price_matches(observed: int, catalog: int) -> bool:
    """Equal, or equal within what an abbreviated reading ("1.12K") can hide."""
    from transactions import abbreviation_slack
    return abs(observed - catalog) <= abbreviation_slack(observed)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _name(lab_id: str) -> str:
    entry = lab_catalog.lab(lab_id)
    return entry.name if entry is not None else lab_id


@dataclass(frozen=True)
class StartDryRun:
    worker: str
    account_id: str | None
    at: float
    lab_id: str
    level: int
    price: int


def _qualifies(runs: list[StartDryRun]) -> bool:
    return any(abs(second.at - first.at) >= PROMOTION_SPACING_SECONDS
               for index, first in enumerate(runs) for second in runs[index + 1:])


def _trim(runs: tuple[StartDryRun, ...]) -> tuple[StartDryRun, ...]:
    """The newest MAX_DRY_RUNS_PER_PAIR per (worker, account), so no worker evicts another's."""
    seen: dict[tuple[str, str | None], int] = {}
    kept: list[StartDryRun] = []
    for run in reversed(runs):
        key = (run.worker, run.account_id)
        if seen.get(key, 0) < MAX_DRY_RUNS_PER_PAIR:
            seen[key] = seen.get(key, 0) + 1
            kept.append(run)
    return tuple(reversed(kept))


@dataclass(frozen=True)
class StageRecord:
    stage: str = "dry_run"
    canary_worker: str | None = None
    canary_account: str | None = None
    dry_runs: tuple[StartDryRun, ...] = ()
    outcome: Mapping[str, Any] | None = None
    halted_reason: str | None = None
    evidence: tuple[str, ...] = ()

    def rehearsals(self, worker: str | None, account_id: str | None) -> int:
        mine = [run for run in self.dry_runs if run.worker == worker and run.account_id == account_id]
        if not mine:
            return 0
        return PROMOTION_REHEARSALS if _qualifies(mine) else 1

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "canary_worker": self.canary_worker,
                "canary_account": self.canary_account,
                "dry_runs": [asdict(run) for run in self.dry_runs],
                "outcome": dict(self.outcome) if self.outcome is not None else None,
                "halted_reason": self.halted_reason, "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, raw: object) -> StageRecord:
        if not isinstance(raw, dict):
            raise ValueError("rollout entry must be an object")
        stage, canary, account = raw.get("stage", "dry_run"), raw.get("canary_worker"), raw.get("canary_account")
        if (stage not in STAGES or canary is not None and not isinstance(canary, str)
                or account is not None and not isinstance(account, str)
                or stage == "canary" and not canary):
            raise ValueError("bad stage or canary")
        runs = []
        for item in raw.get("dry_runs") or []:
            if (not isinstance(item, dict) or not isinstance(item.get("worker"), str)
                    or not _finite(item.get("at")) or not isinstance(item.get("lab_id"), str)
                    or type(item.get("level")) is not int or type(item.get("price")) is not int
                    or item.get("account_id") is not None and not isinstance(item.get("account_id"), str)):
                raise ValueError("bad dry run")
            runs.append(StartDryRun(item["worker"], item.get("account_id"), float(item["at"]),
                                    item["lab_id"], item["level"], item["price"]))
        outcome, reason, evidence = raw.get("outcome"), raw.get("halted_reason"), raw.get("evidence") or []
        if (outcome is not None and not isinstance(outcome, dict)
                or reason is not None and not isinstance(reason, str)
                or not isinstance(evidence, list) or any(not isinstance(path, str) for path in evidence)):
            raise ValueError("bad outcome, reason or evidence")
        return cls(stage, canary, account, tuple(runs), outcome, reason, tuple(evidence))


@dataclass(frozen=True)
class RehearsedLab:
    status: str
    worker: str | None = None
    account_id: str | None = None
    slot: int | None = None
    at: float | None = None
    level: int | None = None
    price: int | None = None
    seconds: float | None = None
    catalog_price: int | None = None
    misses: tuple[float, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "misses": list(self.misses), "evidence": list(self.evidence)}

    @classmethod
    def from_dict(cls, raw: object) -> RehearsedLab:
        if not isinstance(raw, dict) or raw.get("status") not in LAB_STATUSES:
            raise ValueError("bad rehearsed lab")
        worker, account_id, slot = raw.get("worker"), raw.get("account_id"), raw.get("slot")
        at, level, price = raw.get("at"), raw.get("level"), raw.get("price")
        seconds, catalog_price = raw.get("seconds"), raw.get("catalog_price")
        if (worker is not None and not isinstance(worker, str)
                or account_id is not None and not isinstance(account_id, str)
                or slot is not None and (type(slot) is not int or not 1 <= slot <= 5)
                or at is not None and not _finite(at)
                or level is not None and type(level) is not int
                or price is not None and type(price) is not int
                or seconds is not None and not _finite(seconds)
                or catalog_price is not None and type(catalog_price) is not int):
            raise ValueError("bad rehearsed lab field")
        misses, evidence = raw.get("misses") or [], raw.get("evidence") or []
        if (not isinstance(misses, list) or not all(_finite(m) for m in misses)
                or not isinstance(evidence, list) or not all(isinstance(p, str) for p in evidence)):
            raise ValueError("bad misses or evidence")
        return cls(raw["status"], worker, account_id, slot, at, level, price, seconds,
                   catalog_price, tuple(float(m) for m in misses), tuple(evidence))


@dataclass(frozen=True)
class StarterState:
    rollouts: Mapping[str, StageRecord] = field(default_factory=dict)
    labs: Mapping[str, RehearsedLab] = field(default_factory=dict)

    def rollout(self, key: str) -> StageRecord:
        return self.rollouts.get(key, StageRecord())

    def lab(self, lab_id: str) -> RehearsedLab | None:
        return self.labs.get(lab_id)

    def blocked_labs(self) -> frozenset[str]:
        """Labs the planner must skip: not found in the picker, or awaiting price review."""
        return frozenset(lab_id for lab_id, lab in self.labs.items()
                         if lab.status in ("unfindable", "needs_review"))


@dataclass(frozen=True)
class StarterChange:
    key: str
    before: StageRecord
    after: StageRecord

    @property
    def promoted(self) -> str | None:
        forward = {("dry_run", "canary"), ("canary", "fleet")}
        return self.after.stage if (self.before.stage, self.after.stage) in forward else None

    @property
    def halted(self) -> bool:
        return self.after.stage == "halted" and self.before.stage != "halted"


@dataclass(frozen=True)
class StarterGate:
    """`start` spends; `rehearse` walks to the confirmation and cancels; `blocked` does nothing."""
    mode: str
    reason: str
    evidence: str

    @property
    def enabled(self) -> bool:
        return self.mode == "start"


def _blocked(reason: str) -> StarterGate:
    return StarterGate("blocked", f"Planning only: {reason}", "starter_rollout")


def starter_gate(state: StarterState | None, slot: int, lab_id: str, worker: str | None,
                 account_id: str | None) -> StarterGate:
    if (slot, lab_id) == LEGACY:
        return StarterGate("start", "", "legacy_fixture_regression")
    if state is None:
        return _blocked("no fleet lab starter record")
    lab = state.lab(lab_id)
    if lab is not None and lab.status == "unfindable":
        return _blocked(f"{_name(lab_id)} not found in picker")
    if lab is not None and lab.status == "needs_review":
        return _blocked(f"{_name(lab_id)} price needs review")
    record = state.rollout(start_key(slot))
    if record.stage == "halted":
        return _blocked(f"Halted: {record.halted_reason or 'no reason recorded'}")
    if record.stage == "dry_run":
        done = record.rehearsals(worker, account_id)
        return StarterGate("rehearse", f"Rehearsing slot {slot} · {done}/{PROMOTION_REHEARSALS} dry runs",
                           "starter_rollout")
    if lab is None or lab.status != "rehearsed":
        return StarterGate("rehearse", "Lab not rehearsed yet", "starter_rollout")
    if record.stage == "fleet" or (record.canary_worker == worker and record.canary_account == account_id):
        return StarterGate("start", "", "starter_rollout")
    return _blocked("Waiting for canary")


def starter_rows(state: StarterState) -> list[dict[str, Any]]:
    return [{"key": key, "stage": record.stage, "canary_worker": record.canary_worker,
             "dry_runs": len(record.dry_runs), "halted_reason": record.halted_reason,
             "evidence": list(record.evidence)}
            for key in START_KEYS for record in (state.rollout(key),)]


def rehearsed_rows(state: StarterState) -> list[dict[str, Any]]:
    return [{"lab_id": lab_id, "name": _name(lab_id), "status": lab.status, "level": lab.level,
             "price": lab.price, "catalog_price": lab.catalog_price, "seconds": lab.seconds,
             "mismatch": (lab.price is not None and lab.catalog_price is not None
                          and not price_matches(lab.price, lab.catalog_price)),
             "worker": lab.worker, "misses": len(lab.misses), "evidence": list(lab.evidence)}
            for lab_id, lab in sorted(state.labs.items())]


class LabStarterRollout:
    """The shared starter record under the fleet root, serialized by an OS file lock."""

    def __init__(self, fleet_root: Path) -> None:
        self.root = Path(fleet_root)
        self._file = LockedJsonFile(self.root / "lab-starter-rollout.json",
                                    self.root / ".lab-starter-rollout.lock", "Lab starter rollout")

    @staticmethod
    def _parse(document: object) -> StarterState:
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported starter schema")
        rollouts, labs = document.get("rollouts") or {}, document.get("rehearsed") or {}
        if (not isinstance(rollouts, dict) or any(key not in START_KEYS for key in rollouts)
                or not isinstance(labs, dict) or any(not isinstance(key, str) for key in labs)):
            raise ValueError("bad rollouts or rehearsed map")
        return StarterState({key: StageRecord.from_dict(value) for key, value in rollouts.items()},
                            {key: RehearsedLab.from_dict(value) for key, value in labs.items()})

    def state(self, *, quarantine: bool = True) -> StarterState:
        if quarantine:
            with self._file.locked():
                return self._file.load(self._parse, quarantine=True) or StarterState()
        return self._file.load(self._parse, quarantine=False) or StarterState()

    def _write(self, state: StarterState) -> None:
        self._file.write({"schema_version": SCHEMA_VERSION,
                          "rollouts": {key: value.to_dict() for key, value in sorted(state.rollouts.items())},
                          "rehearsed": {key: value.to_dict() for key, value in sorted(state.labs.items())}})

    def _edit(self, key: str | None, change) -> tuple[StarterChange | None, StarterState]:
        """Apply `change(state) -> state` under the lock and write only if it changed."""
        with self._file.locked():
            before = self._file.load(self._parse, quarantine=True, for_write=True) or StarterState()
            after = change(before)
            if after != before:
                self._write(after)
        return (StarterChange(key, before.rollout(key), after.rollout(key)) if key else None), after

    @staticmethod
    def _with_rollout(state: StarterState, key: str, record: StageRecord) -> StarterState:
        return replace(state, rollouts={**state.rollouts, key: record})

    @staticmethod
    def _with_lab(state: StarterState, lab_id: str, lab: RehearsedLab | None) -> StarterState:
        labs = {k: v for k, v in state.labs.items() if k != lab_id}
        if lab is not None:
            labs[lab_id] = lab
        return replace(state, labs=labs)

    def note_start_dry_run(self, slot: int, worker: str, account_id: str | None, lab_id: str,
                           level: int, price: int, seconds: float | None, at: float,
                           evidence: tuple[str, ...] = ()) -> StarterChange:
        """One confirmation read, then Cancel. A catalog mismatch halts the slot."""
        key = start_key(slot)
        priced = lab_catalog.level(lab_id, level)
        catalog = priced.coins if priced is not None else None

        def change(state: StarterState) -> StarterState:
            record = state.rollout(key)
            if record.stage == "halted":
                return state
            if catalog is not None and not price_matches(price, catalog):
                halted = replace(record, stage="halted", evidence=(*record.evidence, *evidence)[-MAX_EVIDENCE:],
                                 halted_reason=(f"Rehearsal read {price} coins for {_name(lab_id)} "
                                                f"Lv.{level}; the catalog says {catalog}"))
                return self._with_rollout(state, key, halted)
            status = "rehearsed" if catalog is not None else "needs_review"
            current = state.lab(lab_id)
            if current is None or current.status != "rehearsed":
                state = self._with_lab(state, lab_id, RehearsedLab(
                    status, worker, account_id, slot, float(at), level, price, seconds, catalog,
                    (), tuple(evidence)))
            if record.stage != "dry_run" or status != "rehearsed":
                return state
            runs = _trim((*record.dry_runs, StartDryRun(worker, account_id, float(at), lab_id, level, price)))
            mine = [run for run in runs if run.worker == worker and run.account_id == account_id]
            if _qualifies(mine):
                return self._with_rollout(state, key, replace(record, stage="canary", canary_worker=worker,
                                                              canary_account=account_id, dry_runs=runs))
            return self._with_rollout(state, key, replace(record, dry_runs=runs))
        return self._edit(key, change)[0]

    def note_research_miss(self, lab_id: str, worker: str, account_id: str | None, at: float,
                           evidence: tuple[str, ...] = ()) -> RehearsedLab:
        """The picker search ended without the lab. Spaced misses make it unfindable."""
        def change(state: StarterState) -> StarterState:
            current = state.lab(lab_id)
            if current is not None and current.status in ("rehearsed", "needs_review", "unfindable"):
                return state
            misses = current.misses if current is not None else ()
            if misses and at - misses[-1] < MISS_SPACING_SECONDS:
                return state
            misses = (*misses, float(at))
            status = "unfindable" if len(misses) >= MISSES_TO_UNFINDABLE else "missing"
            kept = ((*current.evidence, *evidence) if current else tuple(evidence))[-MAX_EVIDENCE:]
            return self._with_lab(state, lab_id, RehearsedLab(status, worker, account_id,
                                                              misses=misses, evidence=kept))
        return self._edit(None, change)[1].lab(lab_id)

    def note_start(self, slot: int, worker: str, account_id: str | None, transaction_key: str,
                   outcome: str, *, at: float) -> StarterChange:
        """The canary's settled start. Bought promotes to fleet; a second miss halts."""
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {OUTCOMES}")
        key = start_key(slot)
        stamp = {"worker": worker, "transaction_key": transaction_key, "outcome": outcome, "at": float(at)}

        def change(state: StarterState) -> StarterState:
            record = state.rollout(key)
            if (record.stage != "canary" or record.canary_worker != worker
                    or record.canary_account != account_id):
                return state
            if outcome == "bought":
                return self._with_rollout(state, key, replace(record, stage="fleet", outcome=stamp))
            if record.outcome is not None and record.outcome.get("outcome") == "not_charged":
                return self._with_rollout(state, key, replace(
                    record, stage="halted", outcome=stamp,
                    halted_reason="The canary's start tap did not land twice"))
            return self._with_rollout(state, key, replace(record, outcome=stamp))
        return self._edit(key, change)[0]

    def halt(self, key: str, reason: str, evidence: tuple[str, ...] = ()) -> StarterChange:
        def change(state: StarterState) -> StarterState:
            record = state.rollout(key)
            kept = (*record.evidence, *evidence)[-MAX_EVIDENCE:]
            if record.stage == "halted":
                return self._with_rollout(state, key, replace(record, evidence=kept))
            return self._with_rollout(state, key, replace(record, stage="halted",
                                                          halted_reason=reason, evidence=kept))
        return self._edit(key, change)[0]

    def halt_canary(self, key: str, worker: str, account_id: str | None, reason: str,
                    evidence: tuple[str, ...] = ()) -> StarterChange:
        def change(state: StarterState) -> StarterState:
            record = state.rollout(key)
            if (record.stage != "canary" or record.canary_worker != worker
                    or record.canary_account != account_id):
                return state
            return self._with_rollout(state, key, replace(
                record, stage="halted", halted_reason=reason,
                evidence=(*record.evidence, *evidence)[-MAX_EVIDENCE:]))
        return self._edit(key, change)[0]

    def release_canary(self, key: str, *, expected_worker: str,
                       expected_account: str | None = None) -> StarterChange:
        """Back to dry run, only while `expected_worker` (on `expected_account`, when given) is the canary."""
        def change(state: StarterState) -> StarterState:
            current = state.rollout(key)
            if (current.stage != "canary" or current.canary_worker != expected_worker
                    or expected_account is not None and current.canary_account != expected_account):
                return state
            return self._with_rollout(state, key, StageRecord())
        return self._edit(key, change)[0]

    def release_absent_canary(self, key: str) -> StarterChange:
        """Back to dry run when the canary left the pool or now plays another account."""
        record = self.state().rollout(key)
        if (record.stage != "canary" or record.canary_worker is None
                or canary_present(self.root, record.canary_worker, record.canary_account) is not False):
            return StarterChange(key, record, record)
        # The presence check judged this exact worker and account; a canary
        # re-promoted since then (even the same worker on another account) stays.
        def change(state: StarterState) -> StarterState:
            current = state.rollout(key)
            if (current.stage != "canary" or current.canary_worker != record.canary_worker
                    or current.canary_account != record.canary_account):
                return state
            return self._with_rollout(state, key, StageRecord())
        return self._edit(key, change)[0]

    def reset(self, key: str) -> StarterChange:
        """The owner's action on a halted slot."""
        if key not in START_KEYS:
            raise ValueError(f"key must be one of {START_KEYS}")

        def change(state: StarterState) -> StarterState:
            if state.rollout(key).stage != "halted":
                raise RolloutError("not_halted")
            return self._with_rollout(state, key, StageRecord())
        return self._edit(key, change)[0]

    def reset_lab(self, lab_id: str) -> None:
        """Forget an unfindable or missing lab so it is searched for again."""
        def change(state: StarterState) -> StarterState:
            lab = state.lab(lab_id)
            if lab is None or lab.status not in ("unfindable", "missing"):
                raise RolloutError("lab_not_blocked")
            return self._with_lab(state, lab_id, None)
        self._edit(None, change)

    def accept_lab(self, lab_id: str) -> None:
        """The owner accepts an uncatalogued lab's observed price."""
        def change(state: StarterState) -> StarterState:
            lab = state.lab(lab_id)
            if lab is None or lab.status != "needs_review":
                raise RolloutError("lab_not_in_review")
            return self._with_lab(state, lab_id, replace(lab, status="rehearsed"))
        self._edit(None, change)

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        state = self.state(quarantine=False)
        return {"starter_rollout": starter_rows(state), "rehearsed_labs": rehearsed_rows(state)}
