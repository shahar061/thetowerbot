"""Atomic, account-unverified worker startup and incident evidence."""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator, Sequence
from uuid import uuid4

from fleet.identity import Attempt
from runtime_identity import BackendIdentity


PROCESS_BOOT_ID = uuid4().hex
MAX_INCIDENTS = 64
MAX_EVIDENCE_REFS = 4
MAX_RECENT_OUTCOMES = 8
MAX_DETAIL_CHARS = 1024


class RuntimeRecordsError(ValueError):
    """A runtime file cannot be trusted or an incident is out of scope."""


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _timestamp(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _bounded_details(values: Sequence[str], limit: int) -> list[str]:
    supplied = list(values)
    if any(not _nonempty(value) or not _nonempty(value[:MAX_DETAIL_CHARS])
           for value in supplied):
        raise ValueError("incident detail entries must be nonblank strings")
    return [value[:MAX_DETAIL_CHARS] for value in supplied[-limit:]]


def _valid_start(start: Any) -> bool:
    if not isinstance(start, dict) or not all(key in start for key in (
        "worker_id", "endpoint", "lease_id", "attempt_id", "generation",
        "created_at", "boot_id", "pid", "revision", "source_hash",
        "started_at", "verified", "attempt_verified",
    )):
        return False
    if (not all(_nonempty(start[key]) for key in (
            "worker_id", "endpoint", "lease_id", "attempt_id", "boot_id"))
            or type(start["pid"]) is not int or start["pid"] <= 0
            or not _timestamp(start["started_at"])
            or start["verified"] is not False
            or type(start["attempt_verified"]) is not bool
            or not all(start[key] is None or _nonempty(start[key])
                       for key in ("revision", "source_hash"))):
        return False
    if start["attempt_verified"]:
        return _nonempty(start["generation"]) and _timestamp(start["created_at"])
    return start["generation"] is None and start["created_at"] is None


def _valid_incident(row: Any, generation: str | None) -> bool:
    if not isinstance(row, dict) or not all(key in row for key in (
        "generation", "fingerprint", "revision", "source_hash", "first_seen_at",
        "last_seen_at", "count", "phase", "reason", "outcome",
        "evidence_refs", "recent_outcomes",
    )):
        return False
    return (
        generation is not None and row["generation"] == generation
        and all(_nonempty(row[key]) for key in ("fingerprint", "phase", "reason", "outcome"))
        and all(row[key] is None or _nonempty(row[key]) for key in ("revision", "source_hash"))
        and _timestamp(row["first_seen_at"]) and _timestamp(row["last_seen_at"])
        and type(row["count"]) is int and row["count"] > 0
        and isinstance(row["evidence_refs"], list)
        and len(row["evidence_refs"]) <= MAX_EVIDENCE_REFS
        and all(_nonempty(item) and len(item) <= MAX_DETAIL_CHARS for item in row["evidence_refs"])
        and isinstance(row["recent_outcomes"], list)
        and len(row["recent_outcomes"]) <= MAX_RECENT_OUTCOMES
        and all(_nonempty(item) and len(item) <= MAX_DETAIL_CHARS for item in row["recent_outcomes"])
    )


class RuntimeRecords:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(f".{self.path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, UnicodeError) as exc:
            raise RuntimeRecordsError("runtime record invalid") from exc
        if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
                or raw["schema_version"] != 1 or not _valid_start(raw.get("start"))
                or not isinstance(raw.get("incidents"), list)
                or len(raw["incidents"]) > MAX_INCIDENTS
                or not all(_valid_incident(row, raw["start"]["generation"])
                           for row in raw["incidents"])):
            raise RuntimeRecordsError("runtime record invalid")
        failure = raw.get("failure")
        if failure is not None and (not isinstance(failure, dict)
                or not _nonempty(failure.get("reason"))
                or not _timestamp(failure.get("at"))):
            raise RuntimeRecordsError("runtime record invalid")
        return raw

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    def begin(
        self, *, worker_id: str, endpoint: str, lease_id: str,
        attempt_id: str, identity: BackendIdentity, boot_id: str, pid: int,
    ) -> None:
        """Record the child's claimed identity before binding validation."""
        if (not all(_nonempty(value) for value in (
                worker_id, endpoint, lease_id, attempt_id, boot_id))
                or type(pid) is not int or pid <= 0):
            raise ValueError("startup needs claimed identity and positive PID")
        start = {
            "worker_id": worker_id, "endpoint": endpoint, "lease_id": lease_id,
            "attempt_id": attempt_id, "generation": None, "created_at": None,
            "boot_id": boot_id, "pid": pid, "revision": identity.revision,
            "source_hash": identity.source_hash, "started_at": identity.started_at,
            "verified": False, "attempt_verified": False,
        }
        with self._locked():
            self.read()  # Refuse to erase a corrupt prior record.
            self._write({"schema_version": 1, "start": start, "incidents": [],
                         "failure": None})

    def fail_startup(self, *, boot_id: str, pid: int, reason: str) -> None:
        """Preserve why identity validation stopped before an attempt was bound."""
        if not _nonempty(reason):
            raise ValueError("startup failure reason is required")
        with self._locked():
            saved = self.read()
            if (saved is None or saved["start"]["boot_id"] != boot_id
                    or saved["start"]["pid"] != pid):
                raise RuntimeRecordsError("startup identity is not current")
            saved["failure"] = {"reason": reason, "at": time.time()}
            self._write(saved)

    def start(
        self, attempt: Attempt, identity: BackendIdentity, *, boot_id: str, pid: int
    ) -> None:
        """Persist unverified process identity before device or OCR setup."""
        if not boot_id.strip() or type(pid) is not int or pid <= 0:
            raise ValueError("startup needs a boot identity and positive PID")
        with self._locked():
            existing = self.read()
            if existing is not None:
                prior = existing["start"]
                if (prior["attempt_verified"]
                        and all(prior[key] == value for key, value in asdict(attempt).items())
                        and prior["boot_id"] == boot_id and prior["pid"] == pid):
                    return
            start = {
                **asdict(attempt), "boot_id": boot_id, "pid": pid,
                "revision": identity.revision, "source_hash": identity.source_hash,
                "started_at": identity.started_at, "verified": False,
                "attempt_verified": True,
            }
            self._write({"schema_version": 1, "start": start, "incidents": [],
                         "failure": None})

    def incident(
        self, *, generation: str, fingerprint: str, phase: str, reason: str,
        outcome: str, evidence_refs: Sequence[str] = (),
        recent_outcomes: Sequence[str] = (),
    ) -> None:
        """Group repeated failures under the current attempt generation."""
        if not all(isinstance(item, str) and item.strip()
                   for item in (generation, fingerprint, phase, reason, outcome)):
            raise ValueError("incident fields are required")
        bounded_refs = _bounded_details(evidence_refs, MAX_EVIDENCE_REFS)
        bounded_outcomes = _bounded_details(recent_outcomes, MAX_RECENT_OUTCOMES)
        with self._locked():
            saved = self.read()
            if saved is None or saved["start"].get("generation") != generation:
                raise RuntimeRecordsError("incident generation is not current")
            now = time.time()
            incidents = saved["incidents"]
            row = next((item for item in incidents
                        if isinstance(item, dict) and item.get("fingerprint") == fingerprint
                        and item.get("generation") == generation), None)
            if row is None:
                row = {
                    "generation": generation, "fingerprint": fingerprint,
                    "revision": saved["start"].get("revision"),
                    "source_hash": saved["start"].get("source_hash"),
                    "first_seen_at": now, "count": 0,
                }
                incidents.append(row)
            row.update({
                "phase": phase, "reason": reason, "outcome": outcome,
                "last_seen_at": now, "count": row["count"] + 1,
                "evidence_refs": bounded_refs,
                "recent_outcomes": bounded_outcomes,
            })
            saved["incidents"] = incidents[-MAX_INCIDENTS:]
            self._write(saved)
