"""Account-bound, confirmed lab observations; timers never complete research.

The runtime only observes. It cannot buy, cancel or repeat a research. Legacy
cadence is kept as historical information until fresh scoped readings confirm
it. Missing cards never erase the last known job on another slot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import cache
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from typing import Any
from uuid import uuid4

import config
import lab_catalog
from lab_plan import LabCadence
from labs import ACCELERATION_STATES, LAB_CONCEPT_IDS, LabJob, LabsReading


@dataclass(frozen=True)
class LabScope:
    account_id: str
    lease_id: str | None = None
    generation: str | None = None
    epoch: int = 0

    @property
    def bound(self) -> bool:
        return bool(self.lease_id and self.generation)


@dataclass(frozen=True)
class LabJobRecord:
    scope: LabScope
    slot: int
    state: str = "unknown"
    research_id: str | None = None
    source_level: int | None = None
    target_level: int | None = None
    remaining_s: float | None = None
    observed_at: float | None = None
    started_observed_at: float | None = None
    expected_finish: float | None = None
    speed_multiplier: float | None = None
    acceleration: str = "unknown"
    native_repeat: str = "unknown"
    catalog_revision: str | None = None
    transaction_id: str | None = None
    generation: str | None = None
    evidence_status: str = "unknown"
    frame_digest: str | None = None
    confirmed: bool = False


@dataclass(frozen=True)
class LabRuntimeSnapshot:
    scope: LabScope
    slots: tuple[LabJobRecord, ...]
    slots_owned: int | None = None
    observed_at: float | None = None
    verified_speed: float | None = None
    # True only when the pair that produced observed_at read the whole owned
    # strip. Retained slots_owned is history, not proof of a complete read.
    strip_complete: bool = False
    # (research_id, target_level, expected_finish) of the highest job this
    # account was seen researching per lab, under any scope. It outlives the
    # slot that showed it, so a job that finished while the bot was down still
    # counts once its finish has passed. Planning history, never authority.
    job_history: tuple[tuple[str, int, float], ...] = ()


def _remember(history: dict[str, tuple[int, float]], research: object, target: object,
              finish: object) -> None:
    """Keep the highest target per lab; a later reading of that target updates its finish."""
    if (research in LAB_CONCEPT_IDS and type(target) is int and target >= 1 and _finite(finish)
            and (research not in history or target >= history[research][0])):
        history[research] = (target, float(finish))


def _history_rows(payload: dict[str, Any]) -> dict[str, tuple[int, float]]:
    """The job history a runtime file carries, plus the jobs its slots still show."""
    history: dict[str, tuple[int, float]] = {}
    for row in payload.get("job_history") or ():
        if isinstance(row, (list, tuple)) and len(row) == 3:
            _remember(history, *row)
    for row in payload.get("slots") or ():
        # Only a record this runtime confirmed; legacy and unconfirmed rows are guesses.
        if (isinstance(row, dict) and row.get("state") == "researching"
                and row.get("confirmed") is True and row.get("evidence_status") == "verified"):
            _remember(history, row.get("research_id"), row.get("target_level"),
                      row.get("expected_finish"))
    return history


def _history(history: dict[str, tuple[int, float]]) -> tuple[tuple[str, int, float], ...]:
    return tuple((research, target, finish) for research, (target, finish) in sorted(history.items()))


def read_job_history(root: Path, account_id: str) -> tuple[tuple[str, int, float], ...]:
    """Read-only `LabRuntimeSnapshot.job_history` of one account, for the dashboard."""
    path = Path(root) / f"lab-runtime-{hashlib.sha256(account_id.encode('utf-8')).hexdigest()}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if (not isinstance(payload, dict) or payload.get("version") != 1
            or not isinstance(payload.get("scope"), dict)
            or payload["scope"].get("account_id") != account_id):
        return ()
    return _history(_history_rows(payload))


@cache
def _catalog_revision() -> str:
    return hashlib.sha256(lab_catalog.CATALOG_PATH.read_bytes()).hexdigest()


def _finite(value: object) -> bool:
    return (type(value) in (int, float) and math.isfinite(value) and value >= 0)


def _jobs(reading: LabsReading) -> dict[int, LabJob]:
    return {j.slot: j for j in reading.jobs
            if type(j.slot) is int and 1 <= j.slot <= 5
            and sum(other.slot == j.slot for other in reading.jobs) == 1
            and _finite(j.confidence) and .9 <= j.confidence <= 1
            and j.native_repeat in {"unknown", "enabled", "disabled"}
            and j.acceleration in ACCELERATION_STATES
            and (j.speed_multiplier is None or _finite(j.speed_multiplier) and j.speed_multiplier > 0)
            and (j.status == "idle" or (
                j.status == "researching" and j.concept_id in LAB_CONCEPT_IDS
                and _finite(j.completes_at) and _finite(j.remaining_s)
                and (j.target_level is None or type(j.target_level) is int and j.target_level >= 1)
                and (j.source_level is None or type(j.source_level) is int and j.source_level >= 0)))}


def _agrees(before: LabJob, after: LabJob) -> bool:
    if (before.status, before.concept_id, before.source_level, before.target_level,
        before.speed_multiplier, before.acceleration, before.native_repeat) != (
            after.status, after.concept_id, after.source_level, after.target_level,
            after.speed_multiplier, after.acceleration, after.native_repeat):
        return False
    return (after.status == "idle" or
            abs(before.completes_at - after.completes_at) <= 2.)


class LabRuntime:
    def __init__(self, root: Path, account_id: str, *, lease_id: str | None = None,
                 generation: str | None = None, epoch: int = 0) -> None:
        if (not isinstance(account_id, str) or not account_id.strip()
                or type(epoch) is not int or epoch < 0
                or any(v is not None and (not isinstance(v, str) or not v)
                       for v in (lease_id, generation))):
            raise ValueError("invalid lab runtime scope")
        self.root = Path(root)
        # A worker can visit several accounts. Neither a switch nor an account
        # name containing path characters may overwrite another account's data.
        account_key = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
        self.path = self.root / f"lab-runtime-{account_key}.json"
        self.scope = LabScope(account_id, lease_id, generation, epoch)
        self._lock = threading.RLock()
        self._pending: LabsReading | None = None
        self._speed_pending: tuple[float, float] | None = None
        # slot -> (research_id, target_level, job generation) from an earlier
        # scope of this account; identity only, never state or confirmation.
        self._carried: dict[int, tuple[str, int, str]] = {}
        self._job_history: dict[str, tuple[int, float]] = {}
        self._snapshot = LabRuntimeSnapshot(self.scope, tuple(
            LabJobRecord(self.scope, slot) for slot in range(1, 6)))
        self._load()

    def snapshot(self) -> LabRuntimeSnapshot:
        """Immutable detached state; elapsed time cannot change its meaning."""
        with self._lock:
            return self._snapshot

    def observe_speed(self, value: float | None, *, observed_at: float) -> bool:
        """Persist a widget capability after two advancing fresh physical captures."""
        with self._lock:
            if value not in config.SPEED_VALUES or value is None or value <= 0 or not _finite(observed_at):
                self._speed_pending = None
                return False
            previous = self._speed_pending
            if previous is not None and observed_at <= previous[1]:
                return False
            self._speed_pending = (value, observed_at)
            if (not self.scope.bound or previous is None or previous[0] != value
                    or not 0 < observed_at-previous[1] <= 30
                    or value <= (self._snapshot.verified_speed or 0)):
                return False
            candidate = replace(self._snapshot, verified_speed=value)
            self._save(candidate)
            self._snapshot = candidate
            return True

    def bind_transaction(self, slot: int, research_id: str, target_level: int,
                         transaction_id: str) -> bool:
        """Materialize a settled journal receipt only onto a freshly confirmed job."""
        with self._lock:
            record = self._snapshot.slots[slot - 1]
            if (not record.confirmed or record.state != 'researching'
                    or record.research_id != research_id or record.target_level != target_level
                    or record.transaction_id not in (None, transaction_id)):
                return False
            if record.transaction_id == transaction_id:
                return True
            slots = list(self._snapshot.slots)
            slots[slot - 1] = replace(record, transaction_id=transaction_id)
            candidate = replace(self._snapshot, slots=tuple(slots))
            self._save(candidate)
            self._snapshot = candidate
            return True

    def observe(self, reading: LabsReading) -> None:
        with self._lock:
            if (not _finite(reading.observed_at) or not reading.frame_digest
                    or reading.frame_width <= 0 or reading.frame_height <= 0
                    or (self._snapshot.observed_at is not None
                        and reading.observed_at <= self._snapshot.observed_at)):
                return
            previous = self._pending
            if previous is not None and reading.observed_at <= previous.observed_at:
                return
            self._pending = reading
            if previous is None or not 0 < reading.observed_at - previous.observed_at <= 30.:
                return
            before, now = _jobs(previous), _jobs(reading)
            owned_strip = (reading.strip_read() and previous.strip_read()
                           and reading.slots_owned == previous.slots_owned)
            matched = {slot: job for slot, job in now.items()
                       if slot in before and _agrees(before[slot], job)
                       and (job.status != "idle" or owned_strip)}
            if not matched:
                return
            slots = list(self._snapshot.slots)
            for slot, job in matched.items():
                old = slots[slot - 1]
                same = (old.state == job.status == "researching"
                        and old.research_id == job.concept_id
                        and job.target_level is not None
                        and old.target_level == job.target_level)
                identified = job.status != "researching" or job.target_level is not None
                confirmed = self.scope.bound and identified
                carried = self._carried.pop(slot, None) if confirmed else None
                continued = (carried[2] if carried is not None and not (same and old.generation)
                             and job.status == "researching"
                             and carried[:2] == (job.concept_id, job.target_level) else None)
                slots[slot - 1] = LabJobRecord(
                    self.scope, slot, state=job.status,
                    research_id=job.concept_id if job.status == "researching" else None,
                    source_level=job.source_level if job.status == "researching" else None,
                    target_level=job.target_level if job.status == "researching" else None,
                    remaining_s=job.remaining_s if job.status == "researching" else None,
                    observed_at=reading.observed_at,
                    started_observed_at=(old.started_observed_at if same else previous.observed_at),
                    expected_finish=job.completes_at if job.status == "researching" else None,
                    speed_multiplier=job.speed_multiplier, acceleration=job.acceleration,
                    native_repeat=job.native_repeat, catalog_revision=_catalog_revision(),
                    transaction_id=old.transaction_id if same else None,
                    generation=((old.generation if same and old.generation
                                 else continued or uuid4().hex) if identified else None),
                    evidence_status="verified" if confirmed else "historical",
                    frame_digest=reading.frame_digest, confirmed=confirmed)
            complete = owned_strip and len(matched) == reading.slots_owned
            history = dict(self._job_history)
            for record in slots:
                if record.confirmed and record.state == "researching":
                    _remember(history, record.research_id, record.target_level, record.expected_finish)
            candidate = LabRuntimeSnapshot(self.scope, tuple(slots),
                reading.slots_owned if complete else self._snapshot.slots_owned,
                reading.observed_at, self._snapshot.verified_speed, complete, _history(history))
            self._save(candidate)
            self._snapshot, self._job_history = candidate, history

    def _save(self, snapshot: LabRuntimeSnapshot) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump({"version": 1, **asdict(snapshot)}, output, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def _load(self) -> None:
        source = self.path if self.path.exists() else self.root / "lab-runtime.json"
        if not source.exists():
            self._legacy()
            return
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or payload.get("scope", {}).get("account_id") != self.scope.account_id:
                return
            self._job_history = _history_rows(payload)
            self._snapshot = replace(self._snapshot, job_history=_history(self._job_history))
            capability = payload.get('verified_speed')
            if type(capability) in (int, float) and capability in config.SPEED_VALUES and capability > 0:
                self._snapshot = replace(self._snapshot, verified_speed=capability)
            if payload.get("scope") != asdict(self.scope):
                # A worker-generation or epoch change for the same account never
                # restores slot state, but remembers each running job's identity
                # so a fresh confirmation of the same research and target keeps
                # its job generation: one deadline window and one correction
                # bound per job, across restarts.
                for row in payload["slots"]:
                    if (isinstance(row, dict) and row.get("state") == "researching"
                            and row.get("research_id") in LAB_CONCEPT_IDS
                            and type(row.get("target_level")) is int
                            and type(row.get("slot")) is int and 1 <= row["slot"] <= 5
                            and isinstance(row.get("generation"), str) and row["generation"]):
                        self._carried[row["slot"]] = (row["research_id"], row["target_level"],
                                                      row["generation"])
                return
            slots = tuple(LabJobRecord(**{**row, "scope": LabScope(**row["scope"])})
                          for row in payload["slots"])
            if (len(slots) != 5 or tuple(j.slot for j in slots) != tuple(range(1, 6))
                    or any(j.scope != self.scope or not self._valid_record(j) for j in slots)
                    or payload["slots_owned"] is not None and not (
                        type(payload["slots_owned"]) is int and 1 <= payload["slots_owned"] <= 5)
                    or payload["observed_at"] is not None and not _finite(payload["observed_at"])):
                return
            # Disk preserves history and transaction identity, never live input authority.
            slots = tuple(replace(job, confirmed=False, evidence_status='historical') for job in slots)
            self._snapshot = LabRuntimeSnapshot(self.scope, slots, payload["slots_owned"],
                                                 payload["observed_at"], self._snapshot.verified_speed,
                                                 job_history=self._snapshot.job_history)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return  # A corrupt runtime cannot authorize an action.

    def _valid_record(self, job: LabJobRecord) -> bool:
        return (job.state in {"unknown", "owned_unread", "locked", "idle", "researching"}
                and job.evidence_status in {"unknown", "historical", "verified"}
                and type(job.confirmed) is bool
                and (not job.confirmed or self.scope.bound and job.evidence_status == "verified")
                and job.native_repeat in {"unknown", "enabled", "disabled"}
                and job.acceleration in ACCELERATION_STATES
                and (job.speed_multiplier is None or _finite(job.speed_multiplier) and job.speed_multiplier > 0)
                and all(value is None or type(value) is int and value >= minimum
                        for value, minimum in ((job.source_level, 0), (job.target_level, 1)))
                and all(value is None or isinstance(value, str) and bool(value)
                        for value in (job.generation, job.transaction_id, job.catalog_revision, job.frame_digest))
                and all(value is None or _finite(value) for value in (
                    job.observed_at, job.started_observed_at, job.expected_finish, job.remaining_s))
                and (not job.confirmed or job.observed_at is not None
                     and bool(job.frame_digest) and bool(job.generation))
                and (job.state != "researching" or job.research_id in LAB_CONCEPT_IDS
                     and _finite(job.expected_finish)
                     and (not job.confirmed or job.target_level is not None)))

    def _legacy(self) -> None:
        """Old cadence is historical, including when current scope is bound."""
        slots = list(self._snapshot.slots)
        try:
            record = json.loads((self.root / "lab-slot1-cadence.json").read_text())
            if (record.get("account_id") == self.scope.account_id
                    and _finite(record.get("observed_at"))):
                observed = record["observed_at"]
                kind = record.get("kind")
                state = {"wait_running": "researching", "wait_coins": "idle",
                         "done": "idle", "wait_unlock": "locked"}.get(kind, "unknown")
                level, finish = record.get("game_speed_level"), record.get("job_completes_at")
                if state == "researching" and not _finite(finish):
                    state = "unknown"
                target = level if type(level) is int and level >= 1 and state == "researching" else None
                slots[0] = LabJobRecord(self.scope, 1, state=state,
                    research_id=lab_catalog.GAME_SPEED if state == "researching" else None,
                    source_level=target - 1 if target is not None else None, target_level=target,
                    expected_finish=finish if state == "researching" else None,
                    observed_at=observed, evidence_status="historical")
        except (OSError, ValueError, TypeError, AttributeError):
            pass

        records = LabCadence(self.root, self.scope.account_id).slot_records()
        locked: list[int] = []
        for slot, record in records.items():
            observed = record.get("observed_at")
            if not _finite(observed):
                continue
            state = "owned_unread" if record["status"] == "owned" else "locked"
            slots[slot - 1] = LabJobRecord(self.scope, slot, state=state, observed_at=observed,
                                           evidence_status="historical")
            if state == "locked":
                locked.append(slot)
        owned = min(locked) - 1 if locked else None
        self._snapshot = replace(self._snapshot, slots=tuple(slots), slots_owned=owned)
