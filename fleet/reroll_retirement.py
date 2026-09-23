"""Take one emulator out of play without guessing about its worker."""

from __future__ import annotations

import time
from typing import Any, Callable, Literal, TypedDict

# States in which no worker process can be playing this emulator. "failed" is
# written only by a start that never launched one.
_SAFE = frozenset({"paused", "stopped", "interrupted", "capacity_wait", "failed"})
# States that resolve by themselves once SIGTERM (or SIGKILL) lands.
_WAIT = frozenset({"running", "stopping"})


class RetireError(ValueError):
    """The worker's exit could not be proven; the emulator stays in play."""


class RetireResult(TypedDict, total=False):
    name: str
    worker: Literal["stopped", "killed", "gone"]
    instance: Literal["stopped", "already_stopped", "missing"]
    error: str


def _stop_worker(name: str, *, supervisor: Any, clock: Callable[[], float],
                 sleep: Callable[[float], None], timeout: float, kill_grace: float,
                 poll: float) -> Literal["stopped", "killed"]:
    status = supervisor.pause(name)
    worker: Literal["stopped", "killed"] = "stopped"
    deadline = clock() + timeout
    while status["state"] not in _SAFE:
        if status["state"] not in _WAIT:
            raise RetireError(status["state"])
        if clock() >= deadline:
            if worker == "killed":
                raise RetireError("worker_did_not_exit")
            worker = "killed"
            status = supervisor.kill(name)
            deadline = clock() + kill_grace
            continue
        sleep(poll)
        status = supervisor.reconcile().get(name, {"state": "instance_not_in_pool"})
    return worker


def _stop_or_gone(name: str, *, supervisor: Any, instance_state: Callable[[str], str | None],
                  journal: Any, clock: Callable[[], float], sleep: Callable[[float], None],
                  timeout: float, kill_grace: float, poll: float) -> Literal["stopped", "killed", "gone"]:
    """Stop ``name``'s worker; an emulator deleted from the host has nothing left to play."""
    try:
        return _stop_worker(name, supervisor=supervisor, clock=clock, sleep=sleep,
                            timeout=timeout, kill_grace=kill_grace, poll=poll)
    except RetireError as exc:
        if instance_state(name) is not None:
            raise
        journal.append(instance=name, level="warn", kind="instance_missing",
                       message=f"Emulator no longer exists, so it was dropped ({exc})")
        return "gone"


def stop_bot(member: dict[str, str], *, supervisor: Any,
             instance_state: Callable[[str], str | None], journal: Any,
             clock: Callable[[], float] = time.monotonic,
             sleep: Callable[[float], None] = time.sleep,
             timeout: float = 60.0, kill_grace: float = 10.0, poll: float = 1.0) -> RetireResult:
    """Stop the worker and leave the emulator as it is. The caller takes it out of the pool."""
    name = member["name"]
    worker = _stop_or_gone(name, supervisor=supervisor, instance_state=instance_state,
                           journal=journal, clock=clock, sleep=sleep, timeout=timeout,
                           kill_grace=kill_grace, poll=poll)
    if worker != "gone":
        journal.append(instance=name, level="info", kind="worker_stopped",
                       message="Bot force-stopped; emulator left running" if worker == "killed"
                       else "Bot stopped; emulator left running")
    return {"name": name, "worker": worker}


def remove_from_run(member: dict[str, str], *, supervisor: Any,
                    stop_instance: Callable[[str, str, str], None],
                    instance_state: Callable[[str], str | None], journal: Any,
                    clock: Callable[[], float] = time.monotonic,
                    sleep: Callable[[float], None] = time.sleep,
                    timeout: float = 60.0, kill_grace: float = 10.0,
                    poll: float = 1.0) -> RetireResult:
    """Stop the worker and shut the emulator down so it can be added back later.

    A stop failure raises: a running emulator whose Tower is already opened
    can't be re-added, so the removal must not complete. An emulator deleted
    from the host is simply dropped. The caller takes the member out of the pool.
    """
    name = member["name"]
    worker = _stop_or_gone(name, supervisor=supervisor, instance_state=instance_state,
                           journal=journal, clock=clock, sleep=sleep, timeout=timeout,
                           kill_grace=kill_grace, poll=poll)
    result: RetireResult = {"name": name, "worker": worker}
    if worker == "gone":
        result["instance"] = "missing"
        return result
    journal.append(instance=name, level="info", kind="worker_removed",
                   message="Worker force-stopped for removal" if worker == "killed"
                   else "Worker stopped for removal")
    try:
        state = instance_state(name)
        if state is None:
            result["instance"] = "missing"
            journal.append(instance=name, level="warn", kind="instance_missing",
                           message="Emulator no longer exists, so it was dropped")
            return result
        if state == "stopped":
            result["instance"] = "already_stopped"
        else:
            stop_instance(name, member["endpoint"], member["lease_id"])
            result["instance"] = "stopped"
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        journal.append(instance=name, level="error", kind="instance_stop_failed",
                       message=f"Emulator is still running, so it stays in the reroll: {error}")
        raise RetireError(f"instance_stop_failed: {error}") from exc
    journal.append(instance=name, level="info", kind="instance_stopped",
                   message="Emulator shut down; it can be added back to the reroll")
    return result
