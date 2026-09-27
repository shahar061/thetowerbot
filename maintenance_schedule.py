"""Persistent account deadlines. Due means inspect, never completed research."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from uuid import uuid4

import lab_catalog
from lab_runtime import LabRuntimeSnapshot


@dataclass(frozen=True)
class DueAction:
    kind: str
    generation: str
    deadline: float
    reason: str
    target_speed: float | None = None
    slot: int | None = None

    @property
    def key(self) -> str:
        return f'{self.kind}:{self.generation}'


def _stamp(finish: float) -> str:
    """Exact, collision-free deadline identity (repr round-trips every float)."""
    return repr(float(finish))


class MaintenanceSchedule:
    def __init__(self, root: Path | None = None, account_id: str | None = None) -> None:
        self.account_id = account_id
        self.path = (Path(root) / ('maintenance-' + hashlib.sha256(account_id.encode()).hexdigest() + '.json')
                     if root is not None and account_id else None)
        self._actions: dict[str, DueAction] = {}
        self._claimed: set[str] = set()
        self._jobs: dict[str, dict[str, float | int]] = {}
        self._clock: tuple[float, float] | None = None
        self._paused = False
        # Read-only Labs inspection visit pacing: exponential backoff between
        # visits that did not acknowledge the due inspection, so BATTLE is
        # never starved. Reset when an inspection is acknowledged.
        self._inspect_attempts = 0
        self._inspect_next_at: float | None = None
        self._load()

    def request(self, kind: str, generation: str, deadline: float, *, reason: str,
                target_speed: float | None = None, slot: int | None = None) -> None:
        if kind not in {'speed_check', 'inspect_labs', 'claim_missions'}:
            raise ValueError('unknown maintenance action')
        if not math.isfinite(deadline) or deadline < 0 or not generation:
            raise ValueError('invalid maintenance deadline')
        action = DueAction(kind, generation, deadline, reason, target_speed, slot)
        if action.key not in self._claimed and self._actions.get(action.key) != action:
            self._actions[action.key] = action
            self._save()

    def observe(self, snapshot: LabRuntimeSnapshot) -> None:
        if snapshot.scope.account_id != self.account_id:
            return
        for job in snapshot.slots:
            if job.confirmed:
                cleared = [key for key, action in self._actions.items()
                           if action.reason == 'timer_correction_limit' and action.slot == job.slot
                           and (job.state == 'idle' or job.generation is not None
                                and not action.generation.startswith(job.generation + ':v'))]
                for key in cleared:
                    self._actions.pop(key)
                if cleared:
                    self._save()
            if job.state != 'researching' or not job.generation or job.expected_finish is None:
                continue
            # Only confirmed jobs may introduce a deadline. Loaded requests survive
            # restart independently and ask for reconciliation, not research proof.
            if not job.confirmed:
                continue
            previous = self._jobs.get(job.generation)
            revision = int(previous['revision']) if previous else 0
            finish = float(previous['finish']) if previous else job.expected_finish
            observed = float(previous['observed_at']) if previous else -1.
            if (previous is not None and job.observed_at is not None and job.observed_at > observed
                    and abs(job.expected_finish-finish) > 2.):
                old_generation = f'{job.generation}:v{revision}'
                if f'speed_check:{old_generation}' in self._claimed:
                    revision += 1
                self._actions.pop(f'inspect_labs:{old_generation}:at{_stamp(finish)}', None)
                # Keys persisted before exact stamps used six significant digits.
                self._actions.pop(f'inspect_labs:{old_generation}:at{finish:g}', None)
                self._actions.pop(f'speed_check:{old_generation}', None)
                finish = job.expected_finish
            row_state = {'revision': min(revision, 3), 'finish': finish,
                         'observed_at': max(observed, job.observed_at or 0.)}
            changed = self._jobs.get(job.generation) != row_state
            self._jobs[job.generation] = row_state
            generation = f'{job.generation}:v{min(revision, 3)}'
            if revision >= 3:
                self.request('inspect_labs', generation, job.observed_at or finish,
                             reason='timer_correction_limit', slot=job.slot)
                if changed:
                    self._save()
                continue
            self.request('inspect_labs', f'{generation}:at{_stamp(finish)}', finish,
                         reason='research_deadline', slot=job.slot)
            row = lab_catalog.level(job.research_id, job.target_level) if job.target_level is not None else None
            if job.research_id == lab_catalog.GAME_SPEED and row is not None:
                self.request('speed_check', generation, finish,
                             reason='game_speed_deadline', target_speed=row.max_speed, slot=job.slot)
            if changed:
                self._save()

    def acknowledge_observation(self, snapshot: LabRuntimeSnapshot, *, now: float) -> None:
        """Only fresh, complete two-capture runtime evidence satisfies inspection.

        Calling request or starting navigation never acknowledges this work. Timer
        corrections are processed first so a new future check is not swallowed.
        """
        if (snapshot.scope.account_id != self.account_id or snapshot.slots_owned is None
                or not snapshot.strip_complete or snapshot.observed_at is None
                or not 0 <= now-snapshot.observed_at <= 30.):
            return
        owned = snapshot.slots[:snapshot.slots_owned]
        if not all(j.confirmed and j.observed_at == snapshot.observed_at for j in owned):
            return
        for action in self.due(now):
            if (action.kind == 'inspect_labs' and action.reason != 'timer_correction_limit'
                    and snapshot.observed_at > action.deadline):
                self.claim(action)

    INSPECT_BACKOFF_BASE = 120.
    INSPECT_BACKOFF_MAX = 3600.

    def inspection_navigable(self, now: float) -> bool:
        """A due inspection may arm a read-only Labs visit now.

        ``timer_correction_limit`` is surfaced only (a visit cannot clear it);
        other inspections back off exponentially between unacknowledged visits.
        """
        if self._inspect_next_at is not None and now < self._inspect_next_at:
            return False
        return any(a.kind == 'inspect_labs' and a.reason != 'timer_correction_limit'
                   for a in self.due(now))

    def note_inspection_visit(self, now: float) -> None:
        self._inspect_attempts += 1
        self._inspect_next_at = now + min(
            self.INSPECT_BACKOFF_MAX, self.INSPECT_BACKOFF_BASE * 2 ** (self._inspect_attempts - 1))

    def due(self, now: float) -> tuple[DueAction, ...]:
        return tuple(sorted((a for a in self._actions.values() if a.deadline <= now),
                            key=lambda a: (a.deadline, a.key)))

    def next_deadline(self) -> float | None:
        return min((a.deadline for a in self._actions.values()), default=None)

    def claim(self, action: DueAction) -> bool:
        """Persist before rearming/dispatch. Same generation cannot claim twice."""
        if self._actions.get(action.key) != action or action.key in self._claimed:
            return False
        self._claimed.add(action.key)
        self._actions.pop(action.key)
        try:
            self._save()
        except OSError:
            self._claimed.remove(action.key)
            self._actions[action.key] = action
            raise
        if action.kind == 'inspect_labs':
            self._inspect_attempts, self._inspect_next_at = 0, None
        return True

    def tick(self, wall: float, monotonic: float, *, paused: bool) -> None:
        reason = None
        if self._clock is not None:
            old_wall, old_mono = self._clock
            if abs((wall-old_wall)-(monotonic-old_mono)) > 2.:
                reason = 'clock_changed'
        if self._paused and not paused:
            reason = 'resumed'
        self._paused, self._clock = paused, (wall, monotonic)
        if reason and (self._jobs or self._actions):
            self.request('inspect_labs', f'{reason}:{uuid4().hex}', wall, reason=reason)

    def wait_seconds(self, now: float, normal: float) -> float:
        # Overdue blocked work waits for the normal scan; it must not busy-loop.
        future = [a.deadline-now for a in self._actions.values() if a.deadline > now]
        return min(normal, min(future, default=normal))

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f'.{self.path.name}.{uuid4().hex}.tmp')
        try:
            with temporary.open('x') as output:
                json.dump({'account_id': self.account_id, 'actions': [asdict(a) for a in self._actions.values()],
                           'claimed': sorted(self._claimed), 'jobs': self._jobs}, output, allow_nan=False)
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

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text())
            if payload['account_id'] != self.account_id:
                return
            actions = [DueAction(**row) for row in payload['actions']]
            if any(a.kind not in {'speed_check', 'inspect_labs', 'claim_missions'}
                   or not isinstance(a.generation, str) or not a.generation
                   or type(a.deadline) not in (int, float) or not math.isfinite(a.deadline) or a.deadline < 0
                   or a.target_speed is not None and (type(a.target_speed) not in (int, float)
                       or not math.isfinite(a.target_speed) or a.target_speed <= 0)
                   for a in actions):
                return
            if not isinstance(payload['claimed'], list) or any(not isinstance(k, str) for k in payload['claimed']):
                return
            jobs = payload.get('jobs', {})
            if not isinstance(jobs, dict) or any(not isinstance(key, str) or not isinstance(row, dict)
                or set(row) != {'revision', 'finish', 'observed_at'}
                or type(row['revision']) is not int or not 0 <= row['revision'] <= 3
                or any(type(row[k]) not in (float, int) or not math.isfinite(row[k]) or row[k] < 0
                       for k in ('finish', 'observed_at')) for key, row in jobs.items()):
                return
            self._jobs = jobs
            self._claimed = set(payload['claimed'])
            self._actions = {a.key: a for a in actions if a.key not in self._claimed}
            if self._jobs or self._actions:
                # No clock evidence is carried across a process restart.
                action = DueAction('inspect_labs', f'restart:{uuid4().hex}', 0., 'restart')
                self._actions[action.key] = action
        except (OSError, ValueError, TypeError, KeyError):
            return
