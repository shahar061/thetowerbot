"""Durable, ownership checked process coordinator for manually selected workers."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock, local
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from fleet.identity import Attempt
from fleet.input_lease import InputLease
from fleet.runtime import WorkerRuntime, WORKER_PORT_REPLACEMENTS, worker_dashboard_port
from fleet.runtime import RuntimeIsolationError, reserve_endpoint
from runtime_identity import PROCESS_IDENTITY
from runtime_records import PROCESS_BOOT_ID


Member = dict[str, str]
Status = dict[str, Any]


def _spawn(args: Sequence[str]) -> subprocess.Popen[Any]:
    arguments = list(args)
    try:
        root = Path(arguments[arguments.index("--runtime-root") + 1])
        name = arguments[arguments.index("--worker-id") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError("worker_log_identity_unavailable") from exc
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("worker_log_identity_invalid")
    worker_root = root / name
    worker_root.mkdir(parents=True, exist_ok=True)
    with (worker_root / "worker.log").open("a", encoding="utf-8") as output:
        return subprocess.Popen(arguments, start_new_session=True,
                                stdout=output, stderr=subprocess.STDOUT)


class _OwnedChild:
    """Keep the child unreaped between its final poll and numeric signal.

    Every supervisor poll and signal uses this one lock. A child which exits
    after poll remains an unreaped zombie until the signal operation finishes,
    so the OS cannot assign its PID to another process in that interval.
    """

    def __init__(self, process: Any) -> None:
        self.process = process
        self.pid = process.pid
        self.lock = RLock()

    def alive(self) -> bool:
        with self.lock:
            return self.process.poll() is None

    def signal(self, callback: Callable[[int], None]) -> bool:
        with self.lock:
            if self.process.poll() is not None:
                return False
            callback(self.pid)
            return True


class _AdoptedChild:
    """A worker spawned by an earlier coordinator that outlived it.

    It is not our child, so it cannot be held as a zombie. Instead argv and
    birth time are re-proved under the lock immediately before every poll and
    signal: a reused PID would need the same argv and the same start second.
    """

    def __init__(self, pid: int, proven: Callable[[], bool]) -> None:
        self.pid = pid
        self.proven = proven
        self.lock = RLock()

    def alive(self) -> bool:
        with self.lock:
            return self.proven()

    def signal(self, callback: Callable[[int], None]) -> bool:
        with self.lock:
            if not self.proven():
                return False
            callback(self.pid)
            return True


def _process_identity(pid: int) -> tuple[str, ...] | None:
    if pid <= 0:
        return None
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                                capture_output=True, text=True, check=False, timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("process_identity_timeout") from exc
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return tuple(shlex.split(result.stdout.strip()))
    except ValueError:
        return None


def _process_birth(pid: int) -> str | None:
    """A second PID proof: argv alone cannot distinguish a reused PID."""
    if pid <= 0:
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, check=False, timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("process_birth_timeout") from exc
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _terminate(pid: int) -> None:
    os.kill(pid, 15)


def _force_kill(pid: int) -> None:
    os.kill(pid, 9)


class RerollSupervisor:
    """Start only exact pool members and retain evidence before controlling a PID.

    ``enroll`` must return a verified registration for the exact member. It is
    called serially, because BlueStacks Manager operations cannot overlap.
    ``process_identity`` must return the process's current full argument vector.
    """

    def __init__(self, root: Path, *, pool_snapshot: Callable[[], dict[str, Any]],
                 enroll: Callable[[Member, WorkerRuntime, Attempt], Status],
                 start_instance: Callable[[Member], None] | None = None,
                 wait_booted: Callable[[Member], None] | None = None,
                 spawn: Callable[[Sequence[str]], Any] = _spawn,
                 process_identity: Callable[[int], Sequence[str] | None] = _process_identity,
                 process_birth: Callable[[int], str | None] | None = None,
                 terminate: Callable[[int], None] = _terminate,
                 force_kill: Callable[[int], None] = _force_kill,
                 max_concurrent_workers: int = 2,
                 start_stagger_seconds: float = 0.5) -> None:
        if not 1 <= max_concurrent_workers <= 4 or start_stagger_seconds < 0:
            raise ValueError("invalid_reroll_capacity")
        self.max_concurrent_workers = max_concurrent_workers
        self.start_stagger_seconds = start_stagger_seconds
        self._last_start = 0.0
        self._launch_lock = Lock()
        self.root = Path(root)
        self.pool_snapshot = pool_snapshot
        self.enroll = enroll
        self.start_instance = start_instance
        # Called after the GUI lock is released: an Android boot takes minutes.
        self.wait_booted = wait_booted
        self.spawn = spawn
        self.process_identity = process_identity
        self.process_birth = (_process_birth if process_identity is _process_identity
                              and process_birth is None else process_birth)
        self.terminate = terminate
        self.force_kill = force_kill
        self.state_root = self.root / "reroll-processes"
        self._owned: dict[str, _OwnedChild | _AdoptedChild] = {}
        self._worker_mutexes: dict[str, RLock] = {}
        self._worker_mutex_guard = Lock()
        self._lock_depth = local()

    @contextmanager
    def _locked(self, name: str) -> Iterator[None]:
        with self._worker_mutex_guard:
            mutex = self._worker_mutexes.setdefault(name, RLock())
        with mutex:
            depth = getattr(self._lock_depth, name, 0)
            if depth:
                setattr(self._lock_depth, name, depth + 1)
                try:
                    yield
                finally:
                    setattr(self._lock_depth, name, depth)
                return
            self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (self.state_root / f"{name}.lock").open("a+") as file:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
                setattr(self._lock_depth, name, 1)
                try:
                    yield
                finally:
                    setattr(self._lock_depth, name, 0)
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _same_record(self, name: str, expected: Status | None) -> bool:
        if expected is None:
            return True
        current = self._read(name)
        return current is not None and all(current.get(key) == expected.get(key) for key in (
            "pid", "process_birth", "attempt_id", "lease_id", "endpoint",
            "input_generation", "args", "spawned_at"))

    def _owned_child(self, name: str, pid: int) -> _OwnedChild | _AdoptedChild | None:
        child = self._owned.get(name)
        if child is not None and child.pid == pid:
            return child
        return self._adopt(name, pid)

    def _adopt(self, name: str, pid: int) -> _AdoptedChild | None:
        """Take over a worker left running by a restarted coordinator.

        Adoption needs the birth-time proof: argv alone cannot tell a reused PID.
        """
        record = self._read(name)
        if (self.process_birth is None or record is None or record.get("pid") != pid
                or not isinstance(record.get("args"), list)):
            return None
        expected = tuple(record["args"])

        def proven() -> bool:
            return (self._birth_matches(record)
                    and tuple(self.process_identity(pid) or ()) == expected)

        if not proven():
            return None
        child = _AdoptedChild(pid, proven)
        self._owned[name] = child
        return child

    @contextmanager
    def _capacity_locked(self) -> Iterator[None]:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_root / ".capacity.lock").open("a+") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def pressure(self, statuses: dict[str, Status] | None = None) -> dict[str, int]:
        states = [row["state"] for row in (statuses if statuses is not None
                                          else self.reconcile()).values()]
        running = states.count("running")
        starting = sum(state in {"starting", "unverified", "stopping", "identity_changed"}
                       for state in states)
        return {"running": running, "starting": starting,
                "limit": self.max_concurrent_workers,
                "available": max(0, self.max_concurrent_workers - running - starting)}

    def _path(self, name: str) -> Path:
        return self.state_root / f"{name}.json"

    def _worker_error(self, name: str) -> str | None:
        path = self.root / "workers" / name / "worker.log"
        try:
            with path.open("rb") as file:
                file.seek(0, os.SEEK_END)
                file.seek(max(0, file.tell() - 8192))
                lines = file.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return None
        return next((line[-400:] for line in reversed(lines)
                     if "ERROR" in line or "identity incident" in line), None)

    def _read(self, name: str) -> Status | None:
        try:
            value = json.loads(self._path(name).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid state")
            return value
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise ValueError("reroll_process_state_unreadable") from exc

    def _save(self, name: str, row: Status) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(row, file, sort_keys=True)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _members(self, snapshot: dict[str, Any] | None = None) -> dict[str, Member]:
        rows = (snapshot if snapshot is not None else self.pool_snapshot())["members"]
        if not isinstance(rows, list):
            raise ValueError("pool_state_unreadable")
        members: dict[str, Member] = {}
        endpoints: set[str] = set()
        for row in rows:
            if (not isinstance(row, dict) or not all(isinstance(row.get(key), str)
                    and row[key] for key in ("name", "endpoint", "lease_id", "state"))
                    or not re.fullmatch(r"[A-Za-z0-9_]+", row["name"])
                    or row["name"] in members or row["endpoint"] in endpoints):
                raise ValueError("pool_identity_ambiguous")
            members[row["name"]] = row
            endpoints.add(row["endpoint"])
        return members

    def _status(self, name: str, member: Member) -> Status:
        record = self._read(name)
        if record is None:
            return {"name": name, "state": "paused"}
        if (self.root / "workers" / name / "generation-transition.json").exists():
            return {"name": name, "state": "identity_changed",
                    "error": "generation_transition_incomplete"}
        if any(record.get(key) != member[key] for key in ("name", "endpoint", "lease_id")):
            return {"name": name, "state": "identity_changed"}
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 0:
            child = self._owned_child(name, pid)
            if child is None:
                if self.process_identity(pid) is None:
                    return {"name": name, "state": "stopped", "pid": pid}
                return {"name": name, "state": "identity_changed", "pid": pid,
                        "error": "owned_child_unavailable"}
            if not child.alive():
                if self.process_identity(pid) is not None:
                    return {"name": name, "state": "identity_changed", "pid": pid}
                return {"name": name, "state": "paused" if record.get("state") == "stopping"
                        else "stopped", "pid": pid}
            observed = self.process_identity(pid)
            if observed is None:
                if record.get("state") == "stopping":
                    return {"name": name, "state": "paused", "pid": pid}
                status = {"name": name, "state": "stopped", "pid": pid}
                error = self._worker_error(name)
                if error:
                    status["error"] = error
                return status
            if tuple(observed) != tuple(record.get("args", ())):
                return {"name": name, "state": "identity_changed", "pid": pid}
            if not self._birth_matches(record):
                return {"name": name, "state": "identity_changed", "pid": pid}
            if record.get("state") == "stopping":
                return {"name": name, "state": "stopping", "pid": pid}
            return {"name": name, "state": "running", "pid": pid,
                    "attempt_id": record["attempt_id"]}
        if record.get("state") == "starting" and record.get("owner_pid") != os.getpid():
            return {"name": name, "state": "interrupted"}
        return {"name": name, "state": record.get("state", "paused")}

    def _birth_matches(self, record: Status) -> bool:
        if self.process_birth is None:
            return True
        expected = record.get("process_birth")
        return (isinstance(expected, str) and bool(expected)
                and self.process_birth(record["pid"]) == expected)

    def reconcile(self, snapshot: dict[str, Any] | None = None) -> dict[str, Status]:
        members = self._members(snapshot)
        result: dict[str, Status] = {}
        for name, member in members.items():
            try:
                # State files are atomically replaced. A snapshot must stay
                # responsive while first-launch enrollment holds the worker lock.
                result[name] = self._status(name, member)
            except Exception as exc:
                result[name] = {"name": name, "state": "failed", "error": str(exc)}
        return result

    def _args(self, member: Member, runtime: WorkerRuntime, attempt: Attempt) -> tuple[str, ...]:
        script = Path(__file__).resolve().parents[1] / "tower_bot.py"
        return (sys.executable, str(script), "--worker-id", member["name"],
                "--runtime-root", str(runtime.root.parent),
                "--bluestacks-instance", member["name"],
                "--reroll-pool", str(self.root / "reroll-pool.json"),
                "--host", member["endpoint"].rpartition(":")[0],
                "--port", member["endpoint"].rpartition(":")[2],
                "--lease-id", member["lease_id"], "--attempt-id", attempt.attempt_id,
                "--web-port", str(runtime.web_port), "--web", "--no-telegram",
                "--game-package", "com.TechTreeGames.TheTower")

    @staticmethod
    def _registration_generation(registration: Status) -> str | None:
        try:
            return json.loads(Path(registration["binding"]).read_text(encoding="utf-8"))["generation"]
        except FileNotFoundError:
            return None  # Synthetic enrollment harnesses only.

    def _rotated_registration(self, name: str, member: Member, runtime: WorkerRuntime,
                              registration: Status, expected: Status | None,
                              cancelled: Callable[[], bool] | None) -> Status:
        if expected is None or cancelled is not None and cancelled():
            raise ValueError("recovery_cancelled_or_unscoped")
        source_path = Path(registration["binding"])
        source = json.loads(source_path.read_text(encoding="utf-8"))
        old_generation = expected.get("input_generation")
        if ((source.get("generation") != old_generation
                and source.get("predecessor_generation") != old_generation)
                or source.get("account_id") != registration["account_id"]
                or any(source.get(key) != value for key, value in (
                    ("worker_id", name), ("endpoint", member["endpoint"]),
                    ("lease_id", member["lease_id"]),
                    ("attempt_id", registration["job_id"])))):
            raise ValueError("recovery_binding_changed")
        generation = uuid4().hex
        fresh_path = runtime.checkpoint_root / f"{generation}.json"
        payload = {**source, "generation": generation,
                   "predecessor_generation": old_generation,
                   "origin_generation": source.get("origin_generation", old_generation)}
        descriptor = os.open(fresh_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        updated = {**registration, "binding": str(fresh_path)}
        self._save_registration(runtime, updated)
        return updated

    @staticmethod
    def _save_registration(runtime: WorkerRuntime, registration: Status) -> None:
        path = runtime.root / "fleet-registration.json"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(registration, output, sort_keys=True)
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

    def start(self, name: str, *, recovery: bool = False,
              expected: Status | None = None,
              cancelled: Callable[[], bool] | None = None) -> Status:
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            old: Status | None = None
            try:
                if not self._same_record(name, expected):
                    return {"name": name, "state": "identity_changed", "error": "process_changed"}
                if cancelled is not None and cancelled():
                    return {"name": name, "state": "cancelled"}
                old = self._read(name)
                if recovery and old is not None and old.get("desired_state") == "paused":
                    return {"name": name, "state": "paused"}
                current = self._status(name, member)
                if current["state"] in {"running", "identity_changed", "starting", "unverified", "stopping"}:
                    return current
                try:
                    with reserve_endpoint(member["endpoint"]):
                        pass
                except RuntimeIsolationError as exc:
                    raise ValueError("endpoint_lock_held") from exc
                if member["state"] not in {"ready", "start_required", "tower_already_opened"}:
                    return {"name": name, "state": "failed", "error": member["state"]}
                number = int(name.rsplit("_", 1)[-1])
                runtime = WorkerRuntime.for_worker(self.root / "workers", name, worker_dashboard_port(number))
                allowed_ports = {runtime.web_port}
                legacy_port = 10_000 + number
                if WORKER_PORT_REPLACEMENTS.get(legacy_port) == runtime.web_port:
                    allowed_ports.add(legacy_port)
                attempt = Attempt.new(name, member["endpoint"], member["lease_id"], uuid4().hex)
                # Coordinator evidence is a pending launch, never the child's
                # runtime identity. The child writes its own hash and PID.
                launch_identity = {
                    "revision": PROCESS_IDENTITY.revision,
                    "source_hash": PROCESS_IDENTITY.source_hash,
                    "boot_id": PROCESS_BOOT_ID,
                    "coordinator_pid": os.getpid(),
                }
                with self._capacity_locked():
                    occupied = sum(self._status(other_name, other)["state"] in {"running", "starting", "unverified", "identity_changed", "stopping"}
                                   for other_name, other in self._members().items() if other_name != name)
                    if occupied >= self.max_concurrent_workers:
                        return {"name": name, "state": "capacity_wait"}
                    self._save(name, {"name": name, "endpoint": member["endpoint"],
                                      "lease_id": member["lease_id"], "state": "starting",
                                      "pid": None, "attempt_id": attempt.attempt_id,
                                      "owner_pid": os.getpid(),
                                      "launch_identity": launch_identity})
                # First launches run in parallel: the host driver serializes only the
                # Manager presses, and the first-launch staging lease is shared.
                if member["state"] == "start_required" and self.start_instance is not None:
                    self.start_instance(member)
                    if self.wait_booted is not None:
                        self.wait_booted(member)
                    refreshed = self._members().get(name)
                    if refreshed is None or any(refreshed.get(key) != member[key]
                            for key in ("name", "endpoint", "lease_id")):
                        raise ValueError("host_identity_changed_after_start")
                    member = refreshed
                if member["state"] not in {"ready", "tower_already_opened"}:
                    raise ValueError(member["state"])
                registration_path = runtime.root / "fleet-registration.json"
                registration = None
                if registration_path.exists():
                    try:
                        registration = json.loads(registration_path.read_text(encoding="utf-8"))
                        binding_path = Path(registration["binding"])
                        binding = json.loads(binding_path.read_text(encoding="utf-8"))
                        if (registration.get("state") != "registered"
                                or registration.get("instance") != name
                                or registration.get("web_port") not in allowed_ports
                                or binding.get("worker_id") != name
                                or binding.get("attempt_id") != registration.get("job_id")
                                or binding.get("account_id") != registration.get("account_id")
                                or binding.get("endpoint") != member["endpoint"]
                                or binding.get("lease_id") != member["lease_id"]
                                or not re.fullmatch(r"[0-9a-f]{32}\.json", binding_path.name)
                                or binding_path.parent.resolve() != runtime.checkpoint_root.resolve()):
                            raise ValueError("worker_identity_binding_changed")
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        raise ValueError("worker_registration_unverified") from exc
                # An opened Tower without a registration can only resume a first
                # launch whose account was already verified; enroll decides.
                if registration is None:
                    registration = self.enroll(member, runtime, attempt)
                if (registration.get("state") != "registered"
                        or registration.get("instance") != name
                        or registration.get("endpoint") != member["endpoint"]
                        or registration.get("lease_id") != member["lease_id"]
                        or registration.get("web_port") not in allowed_ports
                        or not registration.get("account_id") or not registration.get("binding")):
                    raise ValueError("worker_registration_unverified")
                if not isinstance(registration.get("job_id"), str) or not registration["job_id"]:
                    raise ValueError("worker_registration_attempt_missing")
                if registration["web_port"] != runtime.web_port:
                    # Only a stopped, fully verified legacy worker reaches here.
                    # Account, emulator endpoint, and saved binding stay intact.
                    registration = {**registration, "web_port": runtime.web_port}
                    self._save_registration(runtime, registration)
                restart = isinstance(old, dict) and isinstance(old.get("pid"), int)
                if restart and not old.get("input_generation"):
                    raise ValueError("restart_generation_unavailable")
                if restart and old.get("input_generation"):
                    prior_lease = InputLease(runtime.root / "input-lease.json")
                    prior = prior_lease._read()
                    if prior is not None and prior["current"]:
                        prior_lease.revoke(old["input_generation"], "worker_restarting")
                if recovery or restart:
                    registration = self._rotated_registration(name, member, runtime, registration,
                                                              expected or old, cancelled)
                attempt = Attempt.new(name, member["endpoint"], member["lease_id"],
                                      registration["job_id"])
                from fleet import reroll_timing
                from fleet.reroll_strategy import ensure_reroll_strategy
                ensure_reroll_strategy(runtime, reroll_timing.load(self.root))
                args = self._args(member, runtime, attempt)
                record = {"name": name, "endpoint": member["endpoint"],
                          "lease_id": member["lease_id"], "attempt_id": attempt.attempt_id,
                          "input_generation": self._registration_generation(registration),
                          "pid": None, "args": args, "state": "starting",
                          "owner_pid": os.getpid(), "launch_identity": launch_identity,
                          "desired_state": "running"}
                self._save(name, record)
                # A stopped PID does not prove that its ADB lock was released.
                try:
                    with reserve_endpoint(member["endpoint"]):
                        pass
                except RuntimeIsolationError as exc:
                    raise ValueError("endpoint_lock_held") from exc
                try:
                    bound = json.loads(Path(registration["binding"]).read_text(encoding="utf-8"))
                    generation = bound.get("generation")
                    if (bound.get("worker_id") != name or bound.get("endpoint") != member["endpoint"]
                            or bound.get("lease_id") != member["lease_id"]
                            or bound.get("attempt_id") != attempt.attempt_id
                            or not isinstance(generation, str) or not generation):
                        raise ValueError("input_generation_binding_changed")
                    input_lease = InputLease(runtime.root / "input-lease.json")
                    if not restart:
                        input_lease.grant(generation)
                except FileNotFoundError:
                    # Synthetic enrollment harnesses have no on-disk binding.
                    pass
                with self._launch_lock:
                    if cancelled is not None and cancelled():
                        return {"name": name, "state": "cancelled"}
                    delay = self.start_stagger_seconds - (time.monotonic() - self._last_start)
                    if delay > 0:
                        time.sleep(delay)
                    if cancelled is not None and cancelled():
                        if old is not None:
                            self._save(name, old)
                        return {"name": name, "state": "cancelled"}
                    if recovery:
                        from fleet.worker_intent import read_intent
                        intent = read_intent(runtime.root)
                        if intent is not None and intent["desired_state"] == "stopped":
                            if old is not None:
                                self._save(name, old)
                            return {"name": name, "state": "paused",
                                    "reason": "operator_stop"}
                    if restart:
                        input_lease.handoff(old["input_generation"], generation)
                    process = self.spawn(args)
                    self._last_start = time.monotonic()
                pid = getattr(process, "pid", None)
                if not isinstance(pid, int) or pid <= 0 or not callable(getattr(process, "poll", None)):
                    raise ValueError("spawned_process_pid_unavailable")
                self._owned[name] = _OwnedChild(process)
                record["pid"] = pid
                record["process_birth"] = (self.process_birth(pid)
                                           if self.process_birth is not None else None)
                record["spawned_at"] = time.time()
                record["state"] = "unverified"
                self._save(name, record)
                if not self._birth_matches(record):
                    return {"name": name, "state": "unverified", "pid": pid}
                observed = self.process_identity(pid)
                if observed is None:
                    return {"name": name, "state": "unverified", "pid": pid}
                if tuple(observed) != args:
                    return {"name": name, "state": "identity_changed", "pid": pid}
                record["state"] = "running"
                self._save(name, record)
                return self._status(name, member)
            except Exception as exc:
                if cancelled is not None and cancelled():
                    if old is not None:
                        self._save(name, old)
                    return {"name": name, "state": "cancelled"}
                failed = {"name": name, "state": "failed", "error": str(exc)}
                saved = self._read(name)
                if saved is None or not isinstance(saved.get("pid"), int):
                    self._save(name, {**member, **failed})
                return failed

    def start_all(self) -> dict[str, Status]:
        members = self._members()
        if not members:
            return {}
        with ThreadPoolExecutor(max_workers=min(len(members), self.max_concurrent_workers)) as workers:
            results = list(workers.map(self.start, members))
        return dict(zip(members, results))

    def pause(self, name: str, *, recovery: bool = False,
              expected: Status | None = None) -> Status:
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            try:
                if not self._same_record(name, expected):
                    return {"name": name, "state": "identity_changed", "error": "process_changed"}
                current = self._status(name, member)
                if current["state"] != "running":
                    if not recovery:
                        record = self._read(name)
                        if record is not None and record.get("state") != "starting":
                            record["desired_state"] = "paused"
                            self._save(name, record)
                    return current
                record = self._read(name)
                assert record is not None
                pid = record["pid"]
                child = self._owned_child(name, pid)
                if child is None:
                    return {"name": name, "state": "identity_changed", "error": "owned_child_unavailable"}
                if (tuple(self.process_identity(pid) or ()) != tuple(record["args"])
                        or not self._birth_matches(record)):
                    return {"name": name, "state": "identity_changed", "pid": pid}
                lease_path = self.root / "workers" / name / "input-lease.json"
                lease = InputLease(lease_path)
                current_lease = lease._read()
                if current_lease is not None and current_lease["current"]:
                    lease.revoke(current_lease["generation"], "worker_stopping")
                if not recovery:
                    record["desired_state"] = "paused"
                record["state"] = "stopping"
                self._save(name, record)
                child.signal(self.terminate)
                return self._status(name, member)
            except Exception as exc:
                return {"name": name, "state": "failed", "error": str(exc)}

    def kill(self, name: str, *, recovery: bool = False,
             expected: Status | None = None) -> Status:
        """SIGKILL a worker that ignored SIGTERM, after re-proving its identity."""
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            try:
                if not self._same_record(name, expected):
                    return {"name": name, "state": "identity_changed", "error": "process_changed"}
                current = self._status(name, member)
                if current["state"] not in {"running", "stopping"}:
                    if not recovery:
                        record = self._read(name)
                        if record is not None and record.get("state") != "starting":
                            record["desired_state"] = "paused"
                            self._save(name, record)
                    return current
                record = self._read(name)
                assert record is not None
                pid = record["pid"]
                child = self._owned_child(name, pid)
                if child is None:
                    return {"name": name, "state": "identity_changed", "error": "owned_child_unavailable"}
                if (tuple(self.process_identity(pid) or ()) != tuple(record["args"])
                        or not self._birth_matches(record)):
                    return {"name": name, "state": "identity_changed", "pid": pid}
                lease = InputLease(self.root / "workers" / name / "input-lease.json")
                current_lease = lease._read()
                if current_lease is not None and current_lease["current"]:
                    lease.revoke(current_lease["generation"], "worker_killed")
                if not recovery:
                    record["desired_state"] = "paused"
                record["state"] = "stopping"
                self._save(name, record)
                child.signal(self.force_kill)
                return self._status(name, member)
            except Exception as exc:
                return {"name": name, "state": "failed", "error": str(exc)}

    def pause_all(self) -> dict[str, Status]:
        members = self._members()
        if not members:
            return {}
        with ThreadPoolExecutor(max_workers=min(len(members), 4)) as workers:
            results = list(workers.map(self.pause, members))
        return dict(zip(members, results))
