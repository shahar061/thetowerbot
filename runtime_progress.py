"""Atomic, attempt-scoped evidence of completed scans and useful work."""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from fleet.identity import Attempt


@dataclass
class WorkerHeartbeat:
    worker_id: str
    account_id: str | None
    lease_id: str
    attempt_id: str
    generation: str
    boot_id: str
    pid: int
    run_id: int | None = None
    run_observation: dict[str, int | float | None] | None = None
    scan_sequence: int = 0
    phase: str = 'idle'
    phase_started_utc: float | None = None
    phase_deadline_utc: float | None = None
    last_completed_scan_utc: float | None = None
    last_semantic_progress_utc: float | None = None
    last_semantic_kind: str | None = None
    last_semantic_evidence_ref: str | None = None
    wait_reason: str | None = None
    wait_condition: str | None = None
    next_wake_utc: float | None = None
    frame_generation: int = 0
    frame_digest: str | None = None
    observed_utc: float | None = None
    clock_epoch: int = 0
    capture_epoch: int | None = None
    capture_utc: float | None = None
    observation_required: bool = False
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)


class ProgressRecorder:
    """Only the scan owner advances scan and semantic evidence.

    A monitor may read the atomic file but has no timer that mutates it.
    Phase transitions are flushed through replacement; completed scans and
    semantic transitions receive a durable fsync.
    """

    def __init__(self, path: Path, *, attempt: Attempt, account_id: str | None,
                 boot_id: str, pid: int, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if not attempt.generation or not boot_id or pid <= 0:
            raise ValueError('validated attempt, boot and PID required')
        self.path = Path(path)
        self.attempt = attempt
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._last_clock: tuple[float, float] | None = None
        self._capture_started_epoch: int | None = None
        self._pending_capture_epoch: int | None = None
        self._capture_tracking_enabled = False
        self._heartbeat = WorkerHeartbeat(
            worker_id=attempt.worker_id, account_id=account_id,
            lease_id=attempt.lease_id, attempt_id=attempt.attempt_id,
            generation=attempt.generation, boot_id=boot_id, pid=pid,
        )
        started = self._now()
        self._heartbeat.phase = 'startup'
        self._heartbeat.phase_started_utc = started
        self._heartbeat.phase_deadline_utc = started + 60
        self._publish(durable=True)

    def _now(self) -> float:
        wall, mono = self._clock(), self._monotonic()
        if not math.isfinite(wall) or not math.isfinite(mono):
            raise ValueError('invalid clock')
        previous = self._last_clock
        if previous is not None and abs((wall - previous[0]) - (mono - previous[1])) > 5:
            self._heartbeat.clock_epoch += 1
            self._heartbeat.observation_required = True
            self._heartbeat.wait_reason = None
            self._heartbeat.wait_condition = None
            self._heartbeat.next_wake_utc = None
            self._publish(durable=True)
        self._last_clock = (wall, mono)
        return wall

    def _publish(self, *, durable: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=f'.{self.path.name}.', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(asdict(self._heartbeat), stream, sort_keys=True)
                stream.flush()
                if durable:
                    os.fsync(stream.fileno())
            os.replace(name, self.path)
            if durable:
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @contextmanager
    def phase(self, name: str, deadline_seconds: float) -> Iterator[None]:
        if not name or deadline_seconds <= 0 or not math.isfinite(deadline_seconds):
            raise ValueError('phase and finite deadline required')
        with self._lock:
            started = self._now()
            previous = (self._heartbeat.phase, self._heartbeat.phase_started_utc,
                        self._heartbeat.phase_deadline_utc)
            self._heartbeat.phase = name
            self._heartbeat.phase_started_utc = started
            self._heartbeat.phase_deadline_utc = started + deadline_seconds
            if name == 'capture':
                self._capture_started_epoch = self._heartbeat.clock_epoch
            self._publish()
        try:
            yield
        except BaseException:
            # Keep the interrupted operation visible to an external monitor.
            raise
        else:
            with self._lock:
                self._now()
                if (self._heartbeat.phase == name
                        and self._heartbeat.phase_started_utc == started):
                    (self._heartbeat.phase, self._heartbeat.phase_started_utc,
                     self._heartbeat.phase_deadline_utc) = previous
                    self._publish()

    def extend_startup(self, deadline_seconds: float) -> None:
        """Re-arm the startup deadline after fresh evidence of a deliberate wait.

        Only the account walk waiting out a live battle calls this, after a
        completed capture, so a hung capture still trips its own deadline.
        """
        if deadline_seconds <= 0 or not math.isfinite(deadline_seconds):
            raise ValueError('finite deadline required')
        with self._lock:
            if self._heartbeat.phase != 'startup':
                return
            self._heartbeat.phase_deadline_utc = self._now() + deadline_seconds
            self._publish()

    def observe_capture(self) -> None:
        """Mark a completed physical capture in the current clock epoch."""
        with self._lock:
            now = self._now()
            self._capture_tracking_enabled = True
            epoch = self._heartbeat.clock_epoch
            self._pending_capture_epoch = (
                epoch if self._capture_started_epoch == epoch else None)
            self._heartbeat.capture_epoch = self._pending_capture_epoch
            self._heartbeat.capture_utc = now if self._pending_capture_epoch is not None else None
            if self._pending_capture_epoch is None:
                self._heartbeat.observation_required = True
            self._publish()

    def observe_run(self, run_id: int, *, wave: int | None,
                    game_speed: float | None, observed_at: float) -> None:
        """Retain same-frame run facts under the physical capture time and epoch."""
        if type(run_id) is not int or run_id <= 0:
            raise ValueError('observed run requires a positive run ID')
        with self._lock:
            now = self._now()
            if (type(observed_at) not in (int, float) or not math.isfinite(observed_at)
                    or observed_at > now or observed_at < 0
                    or self._pending_capture_epoch != self._heartbeat.clock_epoch
                    or self._heartbeat.capture_utc is None
                    or observed_at < self._heartbeat.capture_utc):
                return
            level = wave if type(wave) is int and wave > 0 else None
            speed = (float(game_speed) if type(game_speed) in (int, float)
                     and math.isfinite(game_speed) and game_speed > 0 else None)
            if level is None and speed is None:
                return
            self._heartbeat.run_observation = {
                'run_id': run_id, 'wave': level, 'game_speed': speed,
                'observed_at_utc': self._heartbeat.capture_utc,
                'clock_epoch': self._heartbeat.clock_epoch,
            }
            self._publish()

    def complete_scan(self, run_id: int | None, frame_digest: str) -> None:
        if not isinstance(frame_digest, str) or not frame_digest:
            raise ValueError('completed scan needs an observed frame digest')
        with self._lock:
            now = self._now()
            fresh_capture = (self._pending_capture_epoch == self._heartbeat.clock_epoch
                             if self._capture_tracking_enabled
                             else not self._heartbeat.observation_required)
            if fresh_capture:
                self._heartbeat.observation_required = False
            else:
                self._heartbeat.observation_required = True
            self._heartbeat.run_id = run_id
            if (not fresh_capture or self._heartbeat.run_observation is not None
                    and self._heartbeat.run_observation['run_id'] != run_id):
                self._heartbeat.run_observation = None
            self._heartbeat.scan_sequence += 1
            if fresh_capture:
                self._heartbeat.frame_generation += 1
                self._heartbeat.frame_digest = frame_digest
                self._heartbeat.observed_utc = now
            self._heartbeat.last_completed_scan_utc = now
            self._heartbeat.phase = 'idle'
            self._heartbeat.phase_started_utc = None
            self._heartbeat.phase_deadline_utc = None
            self._pending_capture_epoch = None
            self._publish(durable=True)

    def meaningful_progress(self, kind: str, evidence_ref: str) -> None:
        if not kind or not evidence_ref:
            raise ValueError('semantic progress needs kind and evidence')
        with self._lock:
            now = self._now()
            self._heartbeat.last_semantic_progress_utc = now
            self._heartbeat.last_semantic_kind = kind
            self._heartbeat.last_semantic_evidence_ref = evidence_ref
            self._publish(durable=True)

    def wait(self, reason: str, condition: str, next_wake_utc: float) -> None:
        if not reason or not condition or not math.isfinite(next_wake_utc):
            raise ValueError('expected wait needs reason, condition and deadline')
        with self._lock:
            now = self._now()
            if (self._heartbeat.wait_reason == reason
                    and self._heartbeat.wait_condition == condition
                    and self._heartbeat.next_wake_utc is not None
                    and (reason == 'settling'
                         or self._heartbeat.next_wake_utc > now + 10)):
                return
            self._heartbeat.wait_reason = reason
            self._heartbeat.wait_condition = condition
            self._heartbeat.next_wake_utc = next_wake_utc
            self._publish(durable=True)

    def clear_wait(self) -> None:
        with self._lock:
            self._now()
            if self._heartbeat.wait_reason is None:
                return
            self._heartbeat.wait_reason = None
            self._heartbeat.wait_condition = None
            self._heartbeat.next_wake_utc = None
            self._publish()

    def bind_account(self, account_id: str) -> None:
        if not account_id:
            raise ValueError('account evidence required')
        with self._lock:
            if self._heartbeat.account_id not in (None, account_id):
                raise ValueError('heartbeat account cannot change within an attempt')
            self._heartbeat.account_id = account_id
            self._publish(durable=True)

    def observe_capability(self, name: str, outcome: str, evidence_ref: str,
                           starvation_seconds: float) -> None:
        if not name or outcome not in {'actionable_unknown', 'progress', 'waiting'}:
            raise ValueError('invalid capability observation')
        with self._lock:
            now = self._now()
            current = self._heartbeat.capabilities.get(name)
            if outcome == 'actionable_unknown':
                since = (current['since_utc'] if current is not None
                         and current['outcome'] == outcome else now)
            else:
                since = now
            self._heartbeat.capabilities[name] = {
                'outcome': outcome, 'evidence_ref': evidence_ref,
                'since_utc': since, 'observed_utc': now,
                'deadline_utc': since + starvation_seconds,
                'observation_due': outcome == 'actionable_unknown',
            }
            self._publish(durable=(outcome == 'progress' or current is None
                                   or current['outcome'] != outcome
                                   or current['evidence_ref'] != evidence_ref))

    def observe_cards(self, status: dict[str, Any]) -> None:
        """Cards freshness/ownership shares the existing capability heartbeat."""
        outcome = 'actionable_unknown' if status.get('active') else 'progress' if status.get('fresh') else 'waiting'
        self.observe_capability('cards', outcome,
            str(status.get('operation_id') or status.get('reason') or 'inventory_unobserved'), 120)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._now()
            return asdict(self._heartbeat)

    def health(self) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            beat = self._heartbeat
            if beat.observation_required:
                status = 'observation_required'
            elif beat.phase_deadline_utc is not None and now > beat.phase_deadline_utc:
                status = 'blocked_scan'
            elif beat.wait_reason is not None and beat.next_wake_utc is not None:
                status = 'wait_expired' if now > beat.next_wake_utc else 'expected_wait'
            else:
                status = 'healthy'
            capabilities = {
                name: {
                    **value,
                    'status': ('starved' if value['outcome'] == 'actionable_unknown'
                               and now > value['deadline_utc'] else 'healthy'),
                    'observation_due': value['outcome'] == 'actionable_unknown',
                }
                for name, value in beat.capabilities.items()
            }
            return {'status': status, 'capabilities': capabilities,
                    'scan_sequence': beat.scan_sequence}
