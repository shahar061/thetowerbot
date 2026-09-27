"""Independent, bounded progress checks for exact fleet worker processes."""

from __future__ import annotations

import json
import logging
import math
import os
import fcntl
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator
from uuid import uuid4

from fleet.input_lease import InputLease, InputLeaseExpired
from fleet.runtime import RuntimeIsolationError, reserve_endpoint
from runtime_records import RuntimeRecords, RuntimeRecordsError


logger = logging.getLogger(__name__)


class StartupPending(Exception):
    """The verified new PID has not published its child evidence yet."""


class WorkerMonitor:
    """Classify scoped progress, and restart only after ownership is gone."""

    def __init__(self, root: Path, supervisor: Any, *, interval: float = 5.0,
                 scan_stale_seconds: float = 90.0, stop_grace: float = 3.0,
                 max_restarts: int = 2, restart_window: float = 900.0,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 recover: bool = True,
                 endpoint_available: Callable[[str], bool] | None = None,
                 account_unverified_seconds: float = 900.0) -> None:
        if (interval <= 0 or scan_stale_seconds <= 0 or stop_grace < 0
                or max_restarts < 1 or restart_window <= 0):
            raise ValueError("monitor needs bounded positive intervals")
        self.root = Path(root)
        self.supervisor = supervisor
        self.interval = interval
        self.scan_stale_seconds = scan_stale_seconds
        self.stop_grace = stop_grace
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.recover = recover
        # A connected worker whose account stays unverified this long is
        # blocked (scans still complete, so the heartbeat alone looks healthy);
        # a fenced restart re-verifies through verify_restart_account.
        self.account_unverified_seconds = account_unverified_seconds
        self.endpoint_available = endpoint_available or self._endpoint_available
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_clock: tuple[float, float] | None = None
        self._clock_hold_until = 0.0
        self._snapshot: dict[str, dict[str, Any]] = {}

    @contextmanager
    def _budget_lock(self, name: str) -> Iterator[Path]:
        directory = self.root / "reroll-processes"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{name}.restarts.lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield directory / f"{name}.restarts.json"
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _reserve_episode(self, name: str, now: float) -> bool:
        with self._budget_lock(name) as path:
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                saved = []
            if (not isinstance(saved, list) or any(
                    not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in saved)):
                raise ValueError("restart budget unreadable")
            episodes = [value for value in saved
                        if value > now or now - value < self.restart_window]
            if len(episodes) >= self.max_restarts:
                return False
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump([*episodes, now], output)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
            return True

    def _incident(self, name: str, start: dict[str, Any], beat: dict[str, Any],
                  reason: str, *, outcome: str, incident_id: str) -> None:
        """Retain the recovery result when a later child rotates R1 records."""
        with self._budget_lock(name) as budget_path:
            path = budget_path.with_name(f"{name}.monitor-incidents.json")
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                rows = []
            if not isinstance(rows, list):
                raise ValueError("monitor incidents unreadable")
            row = next((item for item in rows if isinstance(item, dict)
                        and item.get("id") == incident_id), None)
            if row is None:
                row = {"id": incident_id, "worker_id": name,
                       "account_id": beat.get("account_id"),
                       "lease_id": beat["lease_id"],
                       "attempt_id": beat["attempt_id"],
                       "generation": beat["generation"], "boot_id": beat["boot_id"],
                       "pid": beat["pid"], "revision": start.get("revision"),
                       "source_hash": start.get("source_hash"),
                       "phase": beat["phase"], "reason": reason,
                       "at_utc": self.clock(),
                       "evidence_refs": [str(self.root / "workers" / name /
                                             "worker-heartbeat.json")]}
                rows.append(row)
            row["outcome"] = outcome
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(rows[-32:], output, sort_keys=True)
                    output.write("\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _endpoint_available(endpoint: str) -> bool:
        try:
            with reserve_endpoint(endpoint):
                return True
        except RuntimeIsolationError:
            return False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fleet-worker-monitor",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.interval + self.stop_grace + 1)
            if thread.is_alive():
                raise RuntimeError("fleet worker monitor did not stop")

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {name: row.copy() for name, row in self._snapshot.items()}

    def _held_incident(self, name: str, status: dict[str, Any], now: float) -> dict[str, Any] | None:
        path = self.root / "reroll-processes" / f"{name}.monitor-incidents.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"state": "quarantined", "reason": "monitor_incident_unreadable"}
        if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict):
            return {"state": "quarantined", "reason": "monitor_incident_unreadable"}
        latest = rows[-1]
        outcome = latest.get("outcome")
        held = {"recovery_started", "old_process_alive", "endpoint_lock_held", "restart_budget",
                "restart_unconfirmed", "termination_unconfirmed", "recovery_cancelled"}
        if outcome not in held | {"pid_changed", "process_changed"}:
            return None
        if status.get("state") == "identity_changed":
            return None
        try:
            process = self.supervisor._read(name)
        except (OSError, ValueError):
            return {"state": "quarantined", "reason": "process_state_unreadable"}
        if (process is None or process.get("pid") != latest.get("pid")
                or process.get("lease_id") != latest.get("lease_id")
                or process.get("attempt_id") != latest.get("attempt_id")):
            return None
        return {"state": "identity_conflict" if outcome in {"pid_changed", "process_changed"}
                else "quarantined", "reason": outcome,
                **{key: latest.get(key) for key in (
                    "worker_id", "account_id", "lease_id", "attempt_id",
                    "generation", "boot_id", "pid")},
                "observed_at_utc": now}

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("fleet monitor check failed")
            self._stop.wait(self.interval)

    def _clock_valid(self, now: float, mono: float) -> bool:
        previous = self._last_clock
        self._last_clock = (now, mono)
        if previous is not None and abs((now - previous[0]) - (mono - previous[1])) > 5:
            self._clock_hold_until = mono + 15
        return mono >= self._clock_hold_until

    def _evidence(self, name: str, status: dict[str, Any], now: float) -> tuple[dict[str, Any], dict[str, Any]]:
        root = self.root / "workers" / name
        process = self.supervisor._read(name)
        if process is None or not isinstance(process.get("pid"), int):
            raise ValueError("missing process identity")
        spawned = process.get("spawned_at")
        pending = (isinstance(spawned, (int, float)) and math.isfinite(spawned)
                   and 0 <= now - spawned <= 60)
        record = RuntimeRecords(root / "runtime-records.json").read()
        if record is None:
            if pending:
                raise StartupPending
            raise ValueError("missing runtime identity")
        start = record["start"]
        try:
            beat = json.loads((root / "worker-heartbeat.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            if pending:
                raise StartupPending from None
            raise
        if not isinstance(beat, dict) or not start["attempt_verified"]:
            raise ValueError("unverified worker evidence")
        if beat.get("pid") != process["pid"] or start.get("pid") != process["pid"]:
            if pending:
                raise StartupPending
            raise ValueError("worker process conflict")
        for key, value in (("worker_id", name), ("lease_id", process["lease_id"]),
                           ("attempt_id", process["attempt_id"]),
                           ("pid", process["pid"])):
            if beat.get(key) != value or start.get(key) != value:
                raise ValueError("worker identity conflict")
        for key in ("generation", "boot_id"):
            if not isinstance(beat.get(key), str) or beat[key] != start[key]:
                raise ValueError("heartbeat generation conflict")
        if start["endpoint"] != process["endpoint"] or status.get("pid") != process["pid"]:
            raise ValueError("process endpoint conflict")
        registration = root / "fleet-registration.json"
        if registration.exists():
            registered = json.loads(registration.read_text(encoding="utf-8"))
            if (registered.get("account_id") != beat.get("account_id")
                    or registered.get("lease_id") != beat["lease_id"]):
                raise ValueError("account binding conflict")
        checkpoint = root / "checkpoints" / "supervisor.json"
        if checkpoint.exists():
            device = json.loads(checkpoint.read_text(encoding="utf-8"))
            if (not isinstance(device, dict) or device.get("endpoint") != start["endpoint"]
                    or device.get("expected_account") != beat.get("account_id")):
                raise ValueError("device checkpoint identity conflict")
            if device.get("state") == "quarantined":
                beat["_device_quarantine_reason"] = str(device.get("reason") or "device_quarantined")
            elif device.get("state") == "blocked" and device.get("reason") == "device_unavailable":
                beat["_device_disconnected"] = True
            since = device.get("account_unverified_since")
            if (device.get("state") == "blocked" and isinstance(since, (int, float))
                    and math.isfinite(since)):
                beat["_account_unverified_since"] = float(since)
        return beat, start

    def _classify(self, beat: dict[str, Any], now: float) -> dict[str, Any]:
        phase = beat.get("phase")
        deadline = beat.get("phase_deadline_utc")
        completed = beat.get("last_completed_scan_utc")
        if (not isinstance(phase, str) or not isinstance(beat.get("scan_sequence"), int)
                or deadline is not None and (not isinstance(deadline, (int, float))
                                             or not math.isfinite(deadline))
                or completed is not None and (not isinstance(completed, (int, float))
                                              or not math.isfinite(completed))):
            return {"state": "identity_conflict", "reason": "heartbeat_invalid"}
        if beat.get("_device_quarantine_reason"):
            return {"state": "identity_conflict",
                    "reason": beat["_device_quarantine_reason"]}
        if beat.get("_device_disconnected"):
            return {"state": "device_disconnected", "reason": "device_unavailable"}
        wait = beat.get("wait_reason")
        wake = beat.get("next_wake_utc")
        # A capture/OCR hang can begin while paused (paused scans still
        # capture), so an exceeded phase deadline outranks the pause
        # exemption; only a stale completed scan is exempt while paused.
        if deadline is not None and now > deadline:
            return {"state": "blocked_scan", "reason": "phase_deadline", "phase": phase}
        if wait in {"pause", "operator_pause"}:
            return {"state": "paused", "reason": wait}
        unverified = beat.get("_account_unverified_since")
        if unverified is not None:
            if now - unverified > self.account_unverified_seconds:
                return {"state": "blocked_scan", "reason": "account_unverified", "phase": phase}
            return {"state": "expected_wait", "reason": "account_reverification"}
        if completed is not None and now - completed > self.scan_stale_seconds:
            return {"state": "blocked_scan", "reason": "scan_stale", "phase": phase}
        if beat.get("observation_required"):
            return {"state": "observation_required", "phase": phase}
        if beat["scan_sequence"] == 0:
            return {"state": "starting", "reason": "fresh_scan_pending"}
        if isinstance(wait, str) and isinstance(wake, (int, float)):
            return {"state": "wait_expired" if now > wake else "expected_wait",
                    "reason": wait}
        capabilities = beat.get("capabilities", {})
        if isinstance(capabilities, dict) and any(
            isinstance(row, dict) and row.get("outcome") == "actionable_unknown"
            and isinstance(row.get("deadline_utc"), (int, float))
            and now > row["deadline_utc"] for row in capabilities.values()
        ):
            return {"state": "capability_starved", "phase": phase}
        return {"state": "healthy", "phase": phase}

    def check_once(self) -> dict[str, dict[str, Any]]:
        now, mono = self.clock(), self.monotonic()
        clock_valid = self._clock_valid(now, mono)
        statuses = self.supervisor.reconcile()
        results: dict[str, dict[str, Any]] = {}
        for name, status in statuses.items():
            held = self._held_incident(name, status, now)
            if held is not None:
                results[name] = held
                continue
            if status["state"] == "identity_changed":
                results[name] = {"state": "identity_conflict", "reason": status.get(
                    "error", "process_identity_changed")}
                continue
            if status["state"] != "running":
                results[name] = {"state": status["state"]}
                continue
            try:
                from fleet.worker_intent import read_intent
                intent = read_intent(self.root / "workers" / name)
                if intent is not None and intent["desired_state"] == "stopped":
                    process = self.supervisor._read(name)
                    if (process is None or any(intent.get(key) != process.get(key) for key in (
                            "lease_id", "attempt_id"))
                            or intent.get("worker_id") != name
                            or intent.get("endpoint") != process.get("endpoint")):
                        raise ValueError("bot_intent_scope_changed")
                    registration = self.root / "workers" / name / "fleet-registration.json"
                    if registration.exists():
                        bound = json.loads(registration.read_text(encoding="utf-8"))
                        if (intent.get("account_id") is not None
                                and intent["account_id"] != bound.get("account_id")):
                            raise ValueError("bot_intent_account_changed")
                    results[name] = {"state": "paused", "reason": "operator_stop"}
                    continue
                beat, start = self._evidence(name, status, now)
                row = self._classify(beat, now)
                if intent is not None:
                    if (any(intent.get(key) != beat.get(key) for key in (
                            "worker_id", "lease_id", "attempt_id"))
                            or intent.get("endpoint") != start["endpoint"]):
                        row = {"state": "identity_conflict", "reason": "bot_intent_scope_changed"}
                    elif (intent.get("account_id") is not None
                          and intent["account_id"] != beat.get("account_id")):
                        row = {"state": "identity_conflict", "reason": "bot_intent_account_changed"}
                    elif intent["desired_state"] == "stopped":
                        row = {"state": "paused", "reason": "operator_stop"}
                if not clock_valid:
                    row = {"state": "observation_required", "reason": "clock_discontinuity"}
                elif row["state"] == "blocked_scan" and self.recover:
                    row = self._recover(name, beat, start, row)
                row.update({key: beat.get(key) for key in (
                    "worker_id", "account_id", "lease_id", "attempt_id",
                    "generation", "boot_id", "pid")})
                row["observed_at_utc"] = now
            except StartupPending:
                row = {"state": "starting", "reason": "child_evidence_pending"}
            except (OSError, ValueError, KeyError, TypeError, RuntimeRecordsError,
                    InputLeaseExpired) as exc:
                row = {"state": "identity_conflict", "reason": str(exc)[:200]}
            results[name] = row
        with self._lock:
            self._snapshot = results
        return results

    def _recover(self, name: str, beat: dict[str, Any], start: dict[str, Any],
                 problem: dict[str, Any]) -> dict[str, Any]:
        with self.supervisor._locked(name):
            return self._recover_locked(name, beat, start, problem)

    def _recover_locked(self, name: str, beat: dict[str, Any], start: dict[str, Any],
                        problem: dict[str, Any]) -> dict[str, Any]:
        from fleet.worker_intent import read_intent

        def operator_stopped() -> bool:
            intent = read_intent(self.root / "workers" / name)
            if intent is None:
                return False
            if (any(intent.get(key) != beat.get(key) for key in (
                    "worker_id", "lease_id", "attempt_id"))
                    or intent.get("endpoint") != start["endpoint"]
                    or intent.get("account_id") not in {None, beat.get("account_id")}):
                raise ValueError("bot_intent_scope_changed")
            return intent["desired_state"] == "stopped"

        root = self.root / "workers" / name
        records = RuntimeRecords(root / "runtime-records.json")
        records.incident(generation=beat["generation"],
                         fingerprint=f"blocked:{problem['reason']}:{beat['phase']}",
                         phase=beat["phase"], reason=problem["reason"],
                         outcome="recovery_started",
                         evidence_refs=[str(root / "worker-heartbeat.json")])
        incident_id = uuid4().hex
        self._incident(name, start, beat, problem["reason"],
                       outcome="recovery_started", incident_id=incident_id)

        def result(state: str, reason: str, *, pid: int | None = None) -> dict[str, Any]:
            self._incident(name, start, beat, problem["reason"],
                           outcome=reason, incident_id=incident_id)
            value: dict[str, Any] = {"state": state, "reason": reason}
            if pid is not None:
                value["pid"] = pid
            return value

        process = self.supervisor._read(name)
        if (process is None or process.get("pid") != beat["pid"]
                or process.get("input_generation") != beat["generation"]
                or process.get("attempt_id") != beat["attempt_id"]
                or process.get("lease_id") != beat["lease_id"]
                or process.get("endpoint") != start["endpoint"]):
            return result("identity_conflict", "process_changed")
        if operator_stopped():
            return result("paused", "operator_stop")
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        expected = tuple(process.get("args", ()))
        if tuple(self.supervisor.process_identity(beat["pid"]) or ()) != expected:
            return result("identity_conflict", "pid_changed")
        child = self.supervisor._owned_child(name, beat["pid"])
        if child is None or not child.alive():
            return result("identity_conflict", "owned_child_unavailable")
        lease = InputLease(root / "input-lease.json")
        lease.revoke(beat["generation"], problem["reason"])
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        if operator_stopped():
            return result("paused", "operator_stop")
        stopped = self.supervisor.pause(name, recovery=True, expected=process)
        if stopped["state"] == "identity_changed":
            return result("identity_conflict", "pid_changed")
        if stopped["state"] not in {"stopping", "paused"}:
            return result("quarantined", "termination_unconfirmed")
        def owned_exited() -> bool:
            # A graceful exit leaves an unreaped zombie ('<defunct>' in ps)
            # until polled. Confirm death through the owned Popen, which
            # reaps under its lock, instead of reading a zombie's argv as a
            # PID change.
            owned = self.supervisor._owned_child(name, beat["pid"])
            return owned is not None and not owned.alive()

        deadline = self.monotonic() + self.stop_grace
        while self.monotonic() < deadline and not self._stop.is_set():
            if owned_exited():
                break
            observed = self.supervisor.process_identity(beat["pid"])
            if observed is None:
                break
            if tuple(observed) != expected:
                return result("identity_conflict", "pid_changed")
            self.sleep(min(0.1, max(0, deadline - self.monotonic())))
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        observed = None if owned_exited() else self.supervisor.process_identity(beat["pid"])
        if observed is not None:
            if tuple(observed) != expected:
                return result("identity_conflict", "pid_changed")
            if self._stop.is_set():
                return result("quarantined", "recovery_cancelled")
            forced = self.supervisor.kill(name, recovery=True, expected=process)
            if forced["state"] == "identity_changed":
                return result("identity_conflict", "pid_changed")
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        observed = None if owned_exited() else self.supervisor.process_identity(beat["pid"])
        if observed is not None:
            return result("quarantined", "old_process_alive")
        if not self.endpoint_available(start["endpoint"]):
            return result("quarantined", "endpoint_lock_held")
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        if not self._reserve_episode(name, self.clock()):
            records.incident(generation=beat["generation"],
                             fingerprint=f"blocked:{problem['reason']}:{beat['phase']}",
                             phase=beat["phase"], reason=problem["reason"],
                             outcome="restart_budget_exhausted")
            return result("quarantined", "restart_budget")
        current = self.supervisor._read(name)
        if current is None or current.get("desired_state") == "paused":
            return result("paused", "operator_pause")
        if not self.supervisor._same_record(name, process):
            return result("identity_conflict", "process_changed")
        if self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        if operator_stopped():
            return result("paused", "operator_stop")
        restarted = self.supervisor.start(name, recovery=True, expected=process,
                                           cancelled=self._stop.is_set)
        if restarted["state"] == "cancelled" or self._stop.is_set():
            return result("quarantined", "recovery_cancelled")
        if restarted["state"] == "paused" and restarted.get("reason") == "operator_stop":
            return result("paused", "operator_stop")
        if restarted["state"] != "running":
            current_lease = lease._read()
            if current_lease is not None and current_lease["current"]:
                lease.revoke(current_lease["generation"], "restart_unconfirmed")
            return result("quarantined", "restart_unconfirmed")
        return result("starting", "fresh_scan_pending", pid=restarted["pid"])
