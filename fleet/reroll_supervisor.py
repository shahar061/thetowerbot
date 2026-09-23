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
from threading import Lock
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

from fleet.identity import Attempt
from fleet.runtime import WorkerRuntime


Member = dict[str, str]
Status = dict[str, Any]


def _spawn(args: Sequence[str]) -> int:
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
                                stdout=output, stderr=subprocess.STDOUT).pid


def _process_identity(pid: int) -> tuple[str, ...] | None:
    if pid <= 0:
        return None
    result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return tuple(shlex.split(result.stdout.strip()))
    except ValueError:
        return None


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
                 spawn: Callable[[Sequence[str]], int] = _spawn,
                 process_identity: Callable[[int], Sequence[str] | None] = _process_identity,
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
        self.terminate = terminate
        self.force_kill = force_kill
        self.state_root = self.root / "reroll-processes"
        self._enroll_lock = Lock()

    @contextmanager
    def _locked(self, name: str) -> Iterator[None]:
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_root / f"{name}.lock").open("a+") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

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
        if any(record.get(key) != member[key] for key in ("name", "endpoint", "lease_id")):
            return {"name": name, "state": "identity_changed"}
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 0:
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
            if record.get("state") == "stopping":
                return {"name": name, "state": "stopping", "pid": pid}
            return {"name": name, "state": "running", "pid": pid,
                    "attempt_id": record["attempt_id"]}
        if record.get("state") == "starting" and record.get("owner_pid") != os.getpid():
            return {"name": name, "state": "interrupted"}
        return {"name": name, "state": record.get("state", "paused")}

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
                "--web-port", str(runtime.web_port), "--web",
                "--game-package", "com.TechTreeGames.TheTower")

    def start(self, name: str) -> Status:
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            try:
                current = self._status(name, member)
                if current["state"] in {"running", "identity_changed", "starting", "unverified", "stopping"}:
                    return current
                if member["state"] not in {"ready", "start_required", "tower_already_opened"}:
                    return {"name": name, "state": "failed", "error": member["state"]}
                number = int(name.rsplit("_", 1)[-1])
                runtime = WorkerRuntime.for_worker(self.root / "workers", name, 10000 + number)
                attempt = Attempt.new(name, member["endpoint"], member["lease_id"], uuid4().hex)
                with self._capacity_locked():
                    occupied = sum(self._status(other_name, other)["state"] in {"running", "starting", "unverified", "identity_changed", "stopping"}
                                   for other_name, other in self._members().items() if other_name != name)
                    if occupied >= self.max_concurrent_workers:
                        return {"name": name, "state": "capacity_wait"}
                    self._save(name, {"name": name, "endpoint": member["endpoint"],
                                      "lease_id": member["lease_id"], "state": "starting",
                                      "pid": None, "attempt_id": attempt.attempt_id,
                                      "owner_pid": os.getpid()})
                if member["state"] == "start_required" and self.start_instance is not None:
                    with self._enroll_lock:
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
                                or registration.get("web_port") != runtime.web_port
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
                if registration is None and member["state"] != "ready":
                    raise ValueError("worker_registration_missing_for_opened_tower")
                if registration is None:
                    with self._enroll_lock:
                        registration = self.enroll(member, runtime, attempt)
                if (registration.get("state") != "registered"
                        or registration.get("instance") != name
                        or registration.get("endpoint") != member["endpoint"]
                        or registration.get("lease_id") != member["lease_id"]
                        or registration.get("web_port") != runtime.web_port
                        or not registration.get("account_id") or not registration.get("binding")):
                    raise ValueError("worker_registration_unverified")
                if not isinstance(registration.get("job_id"), str) or not registration["job_id"]:
                    raise ValueError("worker_registration_attempt_missing")
                attempt = Attempt.new(name, member["endpoint"], member["lease_id"],
                                      registration["job_id"])
                from fleet.reroll_strategy import ensure_reroll_strategy
                ensure_reroll_strategy(runtime)
                args = self._args(member, runtime, attempt)
                record = {"name": name, "endpoint": member["endpoint"],
                          "lease_id": member["lease_id"], "attempt_id": attempt.attempt_id,
                          "pid": None, "args": args, "state": "starting",
                          "owner_pid": os.getpid()}
                self._save(name, record)
                with self._launch_lock:
                    delay = self.start_stagger_seconds - (time.monotonic() - self._last_start)
                    if delay > 0:
                        time.sleep(delay)
                    pid = self.spawn(args)
                    self._last_start = time.monotonic()
                if not isinstance(pid, int) or pid <= 0:
                    raise ValueError("spawned_process_pid_unavailable")
                record["pid"] = pid
                record["state"] = "unverified"
                self._save(name, record)
                observed = self.process_identity(pid)
                if observed is None:
                    return {"name": name, "state": "unverified", "pid": pid}
                if tuple(observed) != args:
                    return {"name": name, "state": "identity_changed", "pid": pid}
                record["state"] = "running"
                self._save(name, record)
                return self._status(name, member)
            except Exception as exc:
                failed = {"name": name, "state": "failed", "error": str(exc)}
                self._save(name, {**member, **failed})
                return failed

    def start_all(self) -> dict[str, Status]:
        members = self._members()
        if not members:
            return {}
        with ThreadPoolExecutor(max_workers=min(len(members), self.max_concurrent_workers)) as workers:
            results = list(workers.map(self.start, members))
        return dict(zip(members, results))

    def pause(self, name: str) -> Status:
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            try:
                current = self._status(name, member)
                if current["state"] != "running":
                    return current
                record = self._read(name)
                assert record is not None
                pid = record["pid"]
                if tuple(self.process_identity(pid) or ()) != tuple(record["args"]):
                    return {"name": name, "state": "identity_changed", "pid": pid}
                self.terminate(pid)
                record["state"] = "stopping"
                self._save(name, record)
                return self._status(name, member)
            except Exception as exc:
                return {"name": name, "state": "failed", "error": str(exc)}

    def kill(self, name: str) -> Status:
        """SIGKILL a worker that ignored SIGTERM, after re-proving its identity."""
        members = self._members()
        if name not in members:
            return {"name": name, "state": "failed", "error": "instance_not_in_pool"}
        member = members[name]
        with self._locked(name):
            try:
                current = self._status(name, member)
                if current["state"] not in {"running", "stopping"}:
                    return current
                record = self._read(name)
                assert record is not None
                pid = record["pid"]
                if tuple(self.process_identity(pid) or ()) != tuple(record["args"]):
                    return {"name": name, "state": "identity_changed", "pid": pid}
                self.force_kill(pid)
                record["state"] = "stopping"
                self._save(name, record)
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
