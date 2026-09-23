"""Take one emulator out of play for good, without guessing about its worker."""

from __future__ import annotations

import time
from typing import Any, Callable, Literal, TypedDict

# States in which no worker process can be playing this emulator.
_SAFE = frozenset({"paused", "stopped", "interrupted", "capacity_wait"})
# States that resolve by themselves once SIGTERM (or SIGKILL) lands.
_WAIT = frozenset({"running", "stopping"})


class RetireError(ValueError):
    """The worker's exit could not be proven; the emulator stays in play."""


class RetireResult(TypedDict, total=False):
    name: str
    worker: Literal["stopped", "killed"]
    instance: Literal["stopped", "already_stopped", "stop_failed"]
    error: str


def retire(member: dict[str, str], *, supervisor: Any, remove_from_pool: Callable[[str], Any],
           stop_instance: Callable[[str, str, str], None],
           instance_state: Callable[[str], str | None], journal: Any,
           clock: Callable[[], float] = time.monotonic,
           sleep: Callable[[float], None] = time.sleep,
           timeout: float = 60.0, kill_grace: float = 10.0, poll: float = 1.0) -> RetireResult:
    name = member["name"]
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

    result: RetireResult = {"name": name, "worker": worker}
    journal.append(instance=name, level="info", kind="worker_retired",
                   message="Worker force-stopped and retired" if worker == "killed"
                   else "Worker stopped and retired")
    try:
        if instance_state(name) == "stopped":
            result["instance"] = "already_stopped"
        else:
            stop_instance(name, member["endpoint"], member["lease_id"])
            result["instance"] = "stopped"
    except Exception as exc:
        result["instance"] = "stop_failed"
        result["error"] = str(exc) or type(exc).__name__
    remove_from_pool(name)
    if result["instance"] == "stop_failed":
        journal.append(instance=name, level="error", kind="instance_stop_failed",
                       message=f"Emulator is still running: {result['error']}")
    else:
        journal.append(instance=name, level="info", kind="instance_stopped",
                       message="Emulator shut down")
    return result
