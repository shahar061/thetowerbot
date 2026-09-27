"""Coordinate an in-process scan-owner handoff with the process supervisor."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fleet.identity import Attempt
from fleet.input_lease import InputLease, InputLeaseExpired


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def rotate_registered_attempt(
    root: Path, previous: Attempt, next_attempt: Attempt,
    publish: Callable[[Attempt, Path], None],
) -> tuple[Attempt, Path]:
    """Fence, publish all owner records, then grant under the lifecycle lock.

    The old scan thread must already be dead. An interrupted transition leaves
    a durable marker and a revoked lease for explicit operator review; neither
    the runner nor the coordinator guesses how to resume a partial handoff.
    ``publish`` only records R1/R2 startup evidence, never starts device work.
    """
    root = Path(root)
    state_root = root.parent.parent / "reroll-processes"
    process_path = state_root / f"{previous.worker_id}.json"
    marker = root / "generation-transition.json"
    if (next_attempt.generation == previous.generation
            or any(getattr(next_attempt, key) != getattr(previous, key)
                   for key in ("worker_id", "endpoint", "lease_id", "attempt_id"))):
        raise InputLeaseExpired("generation_transition_scope_changed")
    with (state_root / f"{previous.worker_id}.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if marker.exists():
                raise InputLeaseExpired("generation_transition_incomplete")
            process = json.loads(process_path.read_text(encoding="utf-8"))
            registration_path = root / "fleet-registration.json"
            registration = json.loads(registration_path.read_text(encoding="utf-8"))
            source_path = Path(registration["binding"])
            expected_source = root / "checkpoints" / f"{previous.generation}.json"
            if source_path.resolve() != expected_source.resolve():
                raise InputLeaseExpired("generation_binding_changed")
            source = json.loads(source_path.read_text(encoding="utf-8"))
            if (process.get("pid") != os.getpid() or process.get("state") != "running"
                    or process.get("desired_state") == "paused"
                    or process.get("name") != previous.worker_id
                    or process.get("input_generation") != previous.generation
                    or any(process.get(key) != getattr(previous, key)
                           for key in ("endpoint", "lease_id", "attempt_id"))
                    or any(source.get(key) != getattr(previous, key) for key in (
                        "worker_id", "endpoint", "lease_id", "attempt_id", "generation"))
                    or registration.get("state") != "registered"
                    or registration.get("instance") != previous.worker_id
                    or registration.get("endpoint") != previous.endpoint
                    or registration.get("lease_id") != previous.lease_id
                    or registration.get("job_id") != previous.attempt_id
                    or source.get("account_id") != registration.get("account_id")):
                raise InputLeaseExpired("generation_owner_changed")
            lease = InputLease(root / "input-lease.json")
            lease.assert_current(previous.generation)
            # Copied account evidence remains anchored to its original observation.
            next_attempt = replace(next_attempt, created_at=source["created_at"])
            binding_path = root / "checkpoints" / f"{next_attempt.generation}.json"
            _write(marker, {"pid": os.getpid(), "worker_id": previous.worker_id,
                            "endpoint": previous.endpoint, "lease_id": previous.lease_id,
                            "attempt_id": previous.attempt_id,
                            "from_generation": previous.generation,
                            "to_generation": next_attempt.generation,
                            "reason": "generation_transition_incomplete"})
            lease.revoke(previous.generation, "generation_transition")
            _write(binding_path, {**source, "generation": next_attempt.generation,
                                  "predecessor_generation": previous.generation,
                                  "origin_generation": source.get("origin_generation",
                                                                  previous.generation)})
            _write(registration_path, {**registration, "binding": str(binding_path)})
            _write(process_path, {**process, "input_generation": next_attempt.generation})
            publish(next_attempt, binding_path)
            lease.handoff(previous.generation, next_attempt.generation)
            marker.unlink()
            _sync_directory(root)
            return next_attempt, binding_path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
