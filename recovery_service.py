"""Nonblocking, bounded OpenRouter proposals; never owns or dispatches input.

The caller supplies the durable incident and actual foundation scope. This
service owns its event loop, client and accounting. A consumed proposal still
requires scan-owned fresh-frame validation and a durable action reservation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import fcntl
from hashlib import sha256
import os
from pathlib import Path
import threading
import time
from typing import Callable

import httpx2

from openrouter_recovery import get_model_capability, propose_recovery
from recovery_budget import BudgetOverchargeError, BudgetPolicyStatus, BudgetSummary, RecoveryBudget
from recovery_policy import RecoveryReply, RecoveryRequest, RecoverySettings


# One immutable fleet limit avoids independently configured workers creating
# different slot authorities. Lock files must never be removed or replaced.
FLEET_CONCURRENCY = 2


@dataclass(frozen=True)
class RecoveryServiceStatus:
    mode: str
    model: str
    configured: bool
    phase: str
    request_id: str | None
    incident_id: str | None
    last_outcome: str | None
    blocker: str | None
    in_flight: bool
    budget: BudgetSummary | None
    budget_policy: BudgetPolicyStatus | None
    budget_observed_at_monotonic: float | None
    shutdown_complete: bool
    shutdown_failed: bool


@dataclass
class _Job:
    request: RecoveryRequest
    deadline: float
    invalidated: threading.Event = field(default_factory=threading.Event)
    timed_out: bool = False
    cancelled_at: float | None = None
    worker_fd: int | None = None  # Owned/released only by the background thread.


class _FleetSlots:
    """POSIX process-wide locks; crash releases ownership, never accounting."""

    def __init__(self, fleet_root: Path) -> None:
        self.root = fleet_root / "recovery-network-locks"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _lock(self, name: str) -> int | None:
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return fd

    def acquire(self, worker: str) -> tuple[list[int], str | None]:
        worker_fd = self._lock("worker-" + sha256(worker.encode()).hexdigest())
        if worker_fd is None:
            return [], "worker_busy"
        try:
            for slot in range(FLEET_CONCURRENCY):
                fd = self._lock(f"fleet-{slot}")
                if fd is not None:
                    return [worker_fd, fd], None
        except BaseException:
            os.close(worker_fd)
            raise
        os.close(worker_fd)
        return [], "fleet_busy"

    @staticmethod
    def release(fds: list[int]) -> None:
        for fd in reversed(fds):
            os.close(fd)


class RecoveryService:
    """One outstanding request (including an unconsumed reply) per instance.

    No public method does filesystem or network I/O or joins a thread. The
    constructor starts a daemon thread only when configured. ``close`` starts
    cancellation; poll ``status().shutdown_complete`` for confirmation. A
    cancellation-resistant transport keeps its worker/fleet locks and reports
    ``shutdown_failed`` after the bounded grace period; it cannot free a slot
    while an old network operation still runs. Exceptions are classified only.

    Inject ``api_key`` from the authorized backend configuration. This module
    never reads an environment variable or any other credential source.
    """

    def __init__(self, fleet_root: Path, *, worker: str,
                 settings: RecoverySettings | None = None, api_key: str | None = None,
                 client_factory: Callable[[], httpx2.AsyncClient] | None = None,
                 shutdown_grace_seconds: float = 1.0) -> None:
        if not worker or len(worker) > 160:
            raise ValueError("invalid worker identity")
        if not 0 < shutdown_grace_seconds <= 5:
            raise ValueError("invalid shutdown grace")
        self._root = Path(fleet_root)
        self._worker = worker
        self._settings = settings or RecoverySettings()
        self._key = api_key or ""
        self._client_factory = client_factory or httpx2.AsyncClient
        self._grace = shutdown_grace_seconds
        self._lock = threading.Lock()
        self._job: _Job | None = None
        self._delivery: tuple[_Job, RecoveryReply] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[RecoveryReply] | None = None
        self._closed = False
        self._closed_at: float | None = None
        self._shutdown_complete = False
        self._fatal = False
        self._phase = "idle"
        self._outcome: str | None = None
        self._request_id: str | None = None
        self._incident_id: str | None = None
        self._budget_summary: BudgetSummary | None = None
        self._budget_policy: BudgetPolicyStatus | None = None
        self._budget_observed_at: float | None = None
        self._configured = self._settings.mode != "off" and bool(self._key)
        self._blocker = ("disabled" if self._settings.mode == "off" else
                         "credentials_missing" if not self._key else None)
        self._thread: threading.Thread | None = None
        if self._configured:
            self._thread = threading.Thread(target=self._thread_main, name="recovery-service", daemon=True)
            self._thread.start()
        else:
            self._phase = "disabled"

    def submit(self, request: RecoveryRequest) -> bool:
        now = time.monotonic()
        deadline = min(request.deadline_at_monotonic,
                       request.created_at_monotonic + self._settings.deadline_seconds, now + 15)
        context = request.context
        with self._lock:
            if (not self._configured or self._closed or self._fatal or self._job is not None
                    or self._delivery is not None or context.scope.worker != self._worker
                    or context.paused or context.identity_conflict or context.pending_transaction
                    or deadline <= now or request.created_at_monotonic > now):
                return False
            self._job = _Job(request, deadline)
            self._request_id, self._incident_id = request.request_id, request.incident_id
            self._phase, self._blocker, self._outcome = "queued", None, None
        return True

    def poll(self, *, incident_id: str, request_id: str | None = None) -> RecoveryReply | None:
        """Consume at most once; wrong incident/request cannot consume a reply."""
        with self._lock:
            if self._closed or self._fatal or self._delivery is None:
                return None
            job, reply = self._delivery
            if (job.request.incident_id != incident_id
                    or request_id is not None and reply.request_id != request_id):
                return None
            self._delivery = None
            self._phase = "idle"
            if job.invalidated.is_set():
                return None
            if reply.proposal is not None and time.monotonic() >= job.deadline:
                self._outcome, self._blocker = "stale_request", "stale_request"
                return None
            return reply

    def invalidate(self, *, reason: str) -> None:
        """Revoke delivery immediately; caller text is deliberately not retained."""
        with self._lock:
            self._delivery = None
            self._outcome, self._blocker = "invalidated", "invalidated"
            self._phase = "cancelling" if self._job is not None else "idle"
            if self._job is not None:
                self._job.invalidated.set()
                self._job.cancelled_at = self._job.cancelled_at or time.monotonic()
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass  # The owned loop already stopped.

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed, self._closed_at = True, time.monotonic()
            if self._thread is None:
                self._shutdown_complete = True
        self.invalidate(reason="closed")

    def status(self) -> RecoveryServiceStatus:
        with self._lock:
            cancelled_at = self._job.cancelled_at if self._job else None
            stalled_since = self._closed_at if self._closed else cancelled_at
            failed = (stalled_since is not None
                      and not self._shutdown_complete and time.monotonic() - stalled_since > self._grace)
            return RecoveryServiceStatus(
                mode=self._settings.mode, model=self._settings.model, configured=self._configured,
                phase="shutdown_failed" if failed else "failed" if self._fatal else
                      "closed" if self._shutdown_complete else self._phase,
                request_id=self._request_id, incident_id=self._incident_id,
                last_outcome=self._outcome, blocker="shutdown_failed" if failed else self._blocker,
                in_flight=self._job is not None, budget=self._budget_summary,
                budget_policy=self._budget_policy, budget_observed_at_monotonic=self._budget_observed_at,
                shutdown_complete=self._shutdown_complete,
                shutdown_failed=bool(failed))

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception:
            with self._lock:
                self._fatal, self._blocker, self._phase = True, "service_unavailable", "failed"
        finally:
            with self._lock:
                self._shutdown_complete = True
                self._loop, self._task = None, None
                self._job = None

    async def _serve(self) -> None:
        with self._lock:
            self._loop = asyncio.get_running_loop()
        budget = RecoveryBudget(self._root,
            incident_limit_microusd=self._settings.incident_limit_microusd,
            daily_limit_microusd=self._settings.daily_limit_microusd,
            max_calls=self._settings.max_calls)
        slots = _FleetSlots(self._root)
        client = self._client_factory()
        held_job: _Job | None = None
        try:
            self._refresh_budget(budget)
            while True:
                with self._lock:
                    job, closed = self._job, self._closed
                if closed and job is None:
                    return
                if job is None:
                    await asyncio.sleep(0.005)
                    continue
                held_job = job
                task = asyncio.create_task(self._execute(job, budget, slots, client))
                with self._lock:
                    self._task = task
                timer = asyncio.get_running_loop().call_later(
                    max(0, job.deadline - time.monotonic()), self._expire, job, task)
                try:
                    reply = await task
                except asyncio.CancelledError:
                    reply = RecoveryReply(request_id=job.request.request_id, error="cancelled")
                finally:
                    timer.cancel()
                with self._lock:
                    self._task, self._job = None, None
                    if not job.invalidated.is_set() and not self._closed:
                        if job.timed_out or time.monotonic() >= job.deadline:
                            reply = RecoveryReply(request_id=job.request.request_id, error="timeout")
                        self._delivery = job, reply
                        self._phase = "ready"
                        self._outcome = reply.error or "proposal"
                        self._blocker = self._blocker or reply.error
                    else:
                        self._phase = "idle"
                await self._hold_reply(job)
                self._release_worker(job)
                held_job = None
        finally:
            if held_job is not None:
                self._release_worker(held_job)
            await client.aclose()

    async def _hold_reply(self, job: _Job) -> None:
        """Keep cross-instance ownership after the network slot is free.

        Public consumption/invalidation only clears memory. This owner releases
        the descriptor, including expiry without a polling scan, in background.
        Failed replies may remain available for observation after expiry, but
        never retain action or worker ownership beyond the expired request.
        """
        while True:
            with self._lock:
                delivery = self._delivery
                if (delivery is None or delivery[0] is not job
                        or self._closed or self._fatal):
                    return
                if time.monotonic() >= job.deadline:
                    if delivery[1].proposal is not None:
                        self._delivery = None
                        self._phase = "idle"
                        self._outcome, self._blocker = "stale_request", "stale_request"
                    return
            await asyncio.sleep(0.005)

    @staticmethod
    def _release_worker(job: _Job) -> None:
        if job.worker_fd is not None:
            os.close(job.worker_fd)
            job.worker_fd = None

    def _expire(self, job: _Job, task: asyncio.Task[RecoveryReply]) -> None:
        with self._lock:
            if self._job is not job or task.done():
                return
            job.timed_out = True
            job.cancelled_at = job.cancelled_at or time.monotonic()
            if not job.invalidated.is_set():
                self._outcome, self._blocker, self._phase = "timeout", "timeout", "cancelling"
        task.cancel()

    def _check(self, job: _Job) -> None:
        if job.invalidated.is_set():
            raise asyncio.CancelledError
        if time.monotonic() >= job.deadline:
            job.timed_out = True
            raise TimeoutError

    def _phase_for(self, job: _Job, phase: str) -> None:
        with self._lock:
            if not job.invalidated.is_set() and not self._closed:
                self._phase = phase

    def _refresh_budget(self, budget: RecoveryBudget) -> None:
        summary = budget.summary(day=datetime.now(timezone.utc).date().isoformat())
        policy = budget.policy()
        with self._lock:
            self._budget_summary, self._budget_policy = summary, policy
            self._budget_observed_at = time.monotonic()

    async def _execute(self, job: _Job, budget: RecoveryBudget, slots: _FleetSlots,
                       client: httpx2.AsyncClient) -> RecoveryReply:
        # Quote and send the same immutable request with the *total* effective
        # deadline, so O2 cannot start a fresh shorter-settings window after GET.
        req = job.request.model_copy(update={"deadline_at_monotonic": job.deadline})
        fds: list[int] = []
        reserved = False
        sent = False
        actual_cost: int | None = None
        reply: RecoveryReply | None = None
        try:
            self._check(job)
            fds, blocker = slots.acquire(self._worker)
            if blocker:
                with self._lock:
                    self._blocker = blocker
                return RecoveryReply(request_id=req.request_id, error="disabled")
            job.worker_fd = fds.pop(0)
            self._check(job)
            self._phase_for(job, "capability")
            quote = await get_model_capability(req, self._settings, api_key=self._key,
                client=client, deadline_seconds=min(15.0, job.deadline - time.monotonic()))
            self._check(job)
            if quote is None:
                return RecoveryReply(request_id=req.request_id, error="unsupported_model")
            self._phase_for(job, "reserving")
            reserved = budget.reserve(request_id=req.request_id, incident_id=req.incident_id,
                day=datetime.now(timezone.utc).date().isoformat(), maximum_microusd=quote.worst_case_microusd)
            if not reserved:
                return RecoveryReply(request_id=req.request_id, error="budget_exhausted")
            self._check(job)
            # If marking the send is ambiguous, never optimistically release it.
            sent = True
            budget.mark_sent(req.request_id)
            self._refresh_budget(budget)
            self._check(job)
            self._phase_for(job, "requesting")
            reply = await propose_recovery(req, self._settings, api_key=self._key,
                client=client, reserved_capability=quote)
            actual_cost = reply.cost_microusd
        except TimeoutError:
            reply = RecoveryReply(request_id=req.request_id, error="timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            reply = RecoveryReply(request_id=req.request_id, error="transport")
        finally:
            try:
                if reserved:
                    if sent:
                        budget.settle(req.request_id, actual_microusd=actual_cost)
                    else:
                        budget.release_not_sent(req.request_id)
            except BudgetOverchargeError:
                reply = RecoveryReply(request_id=req.request_id, error="budget_exhausted")
            except Exception:
                with self._lock:
                    self._fatal, self._blocker = True, "accounting_unavailable"
                reply = RecoveryReply(request_id=req.request_id, error="budget_exhausted")
            finally:
                slots.release(fds)
                try:
                    self._refresh_budget(budget)
                except Exception:
                    with self._lock:
                        self._fatal, self._blocker = True, "accounting_unavailable"
        assert reply is not None
        return reply
