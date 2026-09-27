"""Scan-owned recovery state machine; all recovery persistence is background work.

The scan thread calls ``RecoveryCoordinator.step`` once per completed scan. It
never waits for SQLite or the network: durable episode/action/budget work runs
on ``RecoveryStorage``'s single background thread through a one-slot
command/reply boundary, and provider calls run inside ``RecoveryService``.
Only the caller's ``dispatch`` callback (the serial DeviceSupervisor) sends
input, and it re-runs the supplied guard after its durable input checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import tempfile
from pathlib import Path
import queue
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from recovery_budget import RecoveryBudget
from recovery_capabilities import HOST_CONTRACT, CapabilityRegistry
from recovery_episodes import EpisodeNamespace, EpisodeOpenResult, RecoveryEpisode, RecoveryEpisodes
from recovery_policy import (RecoveryCandidate, RecoveryContext, RecoveryImage, RecoveryProposal, RecoveryRequest,
                             RecoverySettings, proposal_is_current, validate_proposal)
from recovery_service import RecoveryService
from recovery_settings import RecoverySettingsStore
from recovery_status import SCHEMA_VERSION, STATUS_FILE, read_config_epoch, redact
from supervisor import RecoveryPreflightBlocked

SNAPSHOT_REFRESH_SECONDS = .25
SNAPSHOT_MAX_AGE_SECONDS = 2.
# Model input additionally needs a snapshot at most this old at the input
# boundary, plus an unchanged config epoch (see recovery_status).
MODEL_INPUT_SNAPSHOT_SECONDS = .5
FRESH_SCAN_SECONDS = 5.
POSTCONDITION_SECONDS = 5.
SUPPORTED_ACTIONS = frozenset({'close_overlay'})
KEY_ENV = 'CLAUDE_OPENROUTER_API_KEY'  # Read only by the backend key loader, never here.


@dataclass(frozen=True)
class StorageReply:
    operation: str
    value: Any = None
    failed: bool = False


@dataclass(frozen=True)
class Stores:
    episodes: RecoveryEpisodes
    budget: RecoveryBudget


class RecoveryStorage:
    """One command/reply slot plus replaceable status publication, no scan I/O.

    The slot stays occupied until the reply is consumed, so a blocked SQLite
    call cannot create a backlog. ``close`` never joins; queued work and an
    optional finalizer still run, and ``shutdown_complete`` reports the end.
    """
    def __init__(self, root: Path, *, worker: str, journal: Any = None,
                 status_path: Path | None = None) -> None:
        self.root, self.worker, self.journal = Path(root), worker, journal
        self.status_path = status_path
        self._lock = threading.Lock()
        self._commands: queue.Queue[tuple[str, Callable[[Stores], Any]]] = queue.Queue(maxsize=1)
        self._busy = False
        self._reply: StorageReply | None = None
        self._snapshot: tuple[Any, tuple[Any, ...], float, str | None] | None = None
        self._publication: dict[str, Any] | None = None
        self._final: Callable[[Stores], Any] | None = None
        self.failed = False
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, name='recovery-storage', daemon=True)
        self._thread.start()

    def set_journal(self, journal: Any) -> None:
        """Swap the account journal whose pending-purchase cache is refreshed."""
        with self._lock:
            self.journal = journal

    def submit(self, operation: str, command: Callable[[Stores], Any]) -> bool:
        with self._lock:
            if self._busy or self._closed.is_set() or self.failed:
                return False
            self._busy = True
            self._commands.put_nowait((operation, command))
            return True

    def poll(self) -> StorageReply | None:
        with self._lock:
            reply = self._reply
            if reply is not None:
                self._reply = None
                self._busy = False
            return reply

    def snapshot(self) -> tuple[Any, tuple[Any, ...], float, str | None] | None:
        with self._lock:
            return self._snapshot

    def publish(self, document: dict[str, Any]) -> None:
        with self._lock:
            self._publication = document

    def close(self, final: Callable[[Stores], Any] | None = None) -> None:
        with self._lock:
            self._final = final
        self._closed.set()

    @property
    def shutdown_complete(self) -> bool:
        return not self._thread.is_alive()

    def _refresh(self, settings: RecoverySettingsStore, registry: CapabilityRegistry) -> None:
        try:
            epoch = read_config_epoch(self.root)  # Before the reads it vouches for.
            state, grants = settings.read(), registry.active(now=time.time())
            snapshot = state, grants, time.monotonic(), epoch
        except Exception:
            return  # Leave the old snapshot to age out; the scan treats it as unavailable.
        with self._lock:
            self._snapshot = snapshot
            journal = self.journal
        if journal is not None:
            try:
                journal.refresh_recovery_snapshot()
            except Exception:
                pass  # The journal's cached snapshot ages into "pending".

    def _write_status(self) -> None:
        with self._lock:
            publication, self._publication = self._publication, None
        if publication is None or self.status_path is None:
            return
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix='.recovery-', dir=self.status_path.parent)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump(publication, stream, allow_nan=False)
                os.replace(name, self.status_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        except (OSError, ValueError):
            pass

    def _run(self) -> None:
        try:
            stores = Stores(RecoveryEpisodes(self.root), RecoveryBudget(self.root))
        except Exception:
            with self._lock:
                self.failed = True
            return
        settings, registry = RecoverySettingsStore(self.root), CapabilityRegistry(self.root)
        refreshed = float('-inf')
        while not self._closed.is_set() or not self._commands.empty():
            if time.monotonic() - refreshed >= SNAPSHOT_REFRESH_SECONDS:
                self._refresh(settings, registry)
                refreshed = time.monotonic()
            try:
                operation, command = self._commands.get(timeout=.02)
            except queue.Empty:
                pass
            else:
                try:
                    reply = StorageReply(operation, command(stores))
                except Exception:
                    reply = StorageReply(operation, failed=True)
                with self._lock:
                    self._reply = reply
            self._write_status()
        with self._lock:
            final = self._final
        if final is not None:
            try:
                final(stores)
            except Exception:
                pass
        self._write_status()


def scope_token(context: RecoveryContext) -> str:
    return hashlib.sha256(context.scope.model_dump_json().encode()).hexdigest()


def semantic_fingerprint(context: RecoveryContext) -> str:
    """Stable across restarts: the stalled screen only.

    Never a frame hash, clock, boot/lease/attempt ID or UUID, and not the
    flickering candidate set, so no replacement incident regains allowances.
    Worker and account are the episode namespace.
    """
    raw = json.dumps(['responsive_stall', 1, context.screen])
    return hashlib.sha256(raw.encode()).hexdigest()


# Phases waiting on the one storage slot; each has exactly one pending command.
_WAITING = {'opening', 'reserving', 'recording', 'closing'}


class RecoveryCoordinator:
    """Scan-thread recovery owner. See ``step``; no method blocks on I/O.

    ``key_loader`` is the backend runtime's reader of ``CLAUDE_OPENROUTER_API_KEY``;
    it is called only when the effective mode is shadow/assist. Off mode never
    opens incidents or constructs a transport, so the existing deterministic
    watchdog path is unchanged by default.
    """
    def __init__(self, root: Path, *, worker: str, journal: Any = None,
                 status_path: Path | None = None, key_loader: Callable[[], str | None] | None = None,
                 service_factory: Callable[..., Any] = RecoveryService) -> None:
        self.root, self.worker = Path(root), worker
        self.storage = RecoveryStorage(self.root, worker=worker, journal=journal,
                                       status_path=status_path)
        self.key_loader, self.service_factory = key_loader, service_factory
        self.service: Any = None
        self._retiring: Any = None
        self.settings = RecoverySettings()
        self._settings_token: Any = None
        self._snapshot: Any = None
        self._closed = False
        self._scope: dict[str, Any] | None = None
        self._last_status_at: float | None = None
        self._outcome: str | None = None
        self._last_proposal: dict[str, Any] | None = None
        self._image_provider: Callable[[], RecoveryImage | None] | None = None
        self._calls_remaining: int | None = None
        self._calls_observed: float | None = None
        self._config_blocker: str | None = None
        self._clear()

    # -- lifecycle -----------------------------------------------------------
    def _clear(self) -> None:
        self._phase = 'idle'
        self._blocker: str | None = None
        self._invalidated = False
        self._episode: RecoveryEpisode | None = None
        self._namespace: EpisodeNamespace | None = None
        self._origin: RecoveryContext | None = None
        self.request: RecoveryRequest | None = None
        self._proposal: RecoveryProposal | None = None
        self._action: str | None = None
        self._source = 'deterministic'
        self._dispatched = False
        self._dispatched_at = 0.
        self._reserved_at = float('inf')
        self._next: str | None = None
        self._pending: tuple[str, Callable[[Stores], Any]] | None = None
        self._pause_requested = False
        self._released = False

    def reset(self) -> None:
        """Operator resume: forget local terminal state. Durable state is kept;
        an unresolved/cooling incident blocks again on the next open."""
        if self._phase in {'idle', 'recovered', 'paused'} and self._pending is None:
            self._clear()

    def set_journal(self, journal: Any) -> None:
        self.storage.set_journal(journal)

    @property
    def owns_lane(self) -> bool:
        """An episode is in progress or awaiting its terminal handling."""
        return self._origin is not None or self._phase != 'idle'

    @property
    def last_outcome(self) -> str | None:
        return self._outcome

    @property
    def enabled(self) -> bool:
        """Nonblocking hint for the host: the latest settings snapshot could make
        this worker shadow/assist. Exact per-account gating happens in step."""
        snapshot = self.storage.snapshot()
        if self._closed or snapshot is None:
            return False
        state, grants, *_ = snapshot
        mode = state.settings.mode
        if mode == 'shadow':
            return state.shadow_worker == self.worker
        return mode == 'assist' and any(
            g.worker == self.worker and g.model == state.settings.model for g in grants)

    def close(self) -> None:
        """Start shutdown without joining. Poll ``shutdown_complete``."""
        if self._closed:
            return
        self._closed = True
        self._invalidated = True
        for service in (self.service, self._retiring):
            if service is not None:
                service.invalidate(reason='stop')
                service.close()
        episode, namespace, action = self._episode, self._namespace, self._action
        undispatched = action is not None and not self._dispatched and self._phase in {
            'reserving', 'dispatch'}
        pending, self._pending = self._pending, None
        if pending is not None and pending[0] not in {'outcome', 'close'}:
            pending = None  # Never open an incident or reserve a slot during shutdown.

        def final(stores: Stores) -> None:
            if pending is not None:
                try:
                    pending[1](stores)  # An outcome/close the slot had not yet accepted.
                except Exception:
                    pass
            # A reservation that provably never reached input is released as
            # not_dispatched (its slot stays consumed). Dispatched actions
            # without a classified postcondition stay unresolved.
            if undispatched and episode is not None and namespace is not None:
                try:
                    stores.episodes.record_outcome(namespace=namespace,
                        incident_id=episode.incident_id, action_id=action, outcome='not_dispatched')
                except ValueError:
                    pass  # Reservation was never granted.
        self.storage.publish(self.status())
        self.storage.close(final)

    @property
    def shutdown_complete(self) -> bool:
        return self.storage.shutdown_complete and all(
            s is None or s.status().shutdown_complete for s in (self.service, self._retiring))

    # -- configuration -------------------------------------------------------
    def _assist_grants(self, grants: tuple[Any, ...], model: str) -> tuple[Any, ...]:
        """Activated grants for this worker, the current scan's account and model,
        on the supported host contract/action classes; none before any scan."""
        account = self._scope['account_id'] if self._scope else None
        return tuple(g for g in grants if account is not None
                     and g.host_contract == HOST_CONTRACT and g.action in SUPPORTED_ACTIONS
                     and (g.worker, g.account_id, g.model) == (self.worker, account, model))

    def _effective(self, state: Any, grants: tuple[Any, ...]) -> RecoverySettings:
        effective = state.settings
        self._config_blocker = None
        if effective.mode == 'shadow' and state.shadow_worker != self.worker:
            effective = effective.model_copy(update={'mode': 'off'})
        if effective.mode == 'assist' and not self._assist_grants(grants, effective.model):
            # No calibration for this worker/account/model: no provider calls.
            # An account switch changes this and retires the service.
            effective = effective.model_copy(update={'mode': 'off'})
            self._config_blocker = 'assist_uncalibrated'
        return effective

    def _configuration(self) -> bool:
        snapshot = self.storage.snapshot()
        if (snapshot is None or self.storage.failed
                or time.monotonic() - snapshot[2] > SNAPSHOT_MAX_AGE_SECONDS):
            self._config_blocker = 'storage_unavailable'
            return False
        state, grants, *_ = snapshot
        effective = self._effective(state, grants)
        token = (state.settings_revision, state.policy.active.revision, effective,
                 tuple(g.model_dump_json() for g in grants))
        if self._settings_token is not None and token != self._settings_token:
            # Mode/model/config/policy/capability change: a reply computed under
            # the old token is never used, and the old instance is retained
            # until its shutdown is confirmed.
            if self._origin is not None:
                self._invalidated = True
            self._retire('settings_changed')
        self._settings_token, self._snapshot, self.settings = token, snapshot, effective
        if self._retiring is not None:
            status = self._retiring.status()
            if not status.shutdown_complete:
                self._config_blocker = 'shutdown_failed' if status.shutdown_failed else 'service_retiring'
                return True
            self._retiring = None
        if self.service is None:
            # Constructor and public methods do no storage/network I/O; off or
            # missing-key configuration starts no transport.
            try:
                key = self.key_loader() if effective.mode != 'off' and self.key_loader else None
                self.service = self.service_factory(self.root, worker=self.worker,
                                                    settings=effective, api_key=key)
            except Exception:
                self._config_blocker = 'service_unavailable'  # Never breaks the scan.
        return True

    def _retire(self, reason: str) -> None:
        if self.service is not None:
            self.service.invalidate(reason=reason)
            self.service.close()
            if self._retiring is None:
                self._retiring = self.service
            self.service = None

    # -- status --------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """Redacted document: no keys, provider text, geometry or images."""
        service = self.service.status() if self.service is not None else None
        state = self._snapshot[0] if self._snapshot else None
        budget = service.budget if service else None
        observed = service.budget_observed_at_monotonic if service else None
        retiring = self._retiring.status() if self._retiring is not None else None
        failed = any(s is not None and s.shutdown_failed for s in (service, retiring))
        document = dict(
            mode=self.settings.mode, model=self.settings.model,
            configured=bool(service and service.configured), phase=self._phase,
            incident_id=self._episode.incident_id if self._episode else None,
            last_outcome=self._outcome, last_proposal=self._last_proposal,
            calls_remaining=self._calls_remaining, calls_observed_at=self._calls_observed,
            cost_used_microusd=budget.settled_microusd if budget else None,
            cost_reserved_microusd=budget.reserved_microusd if budget else None,
            budget_observed_at=(time.time() - (time.monotonic() - observed)
                                if observed is not None else None),
            policy_revision=state.policy.active.revision if state else None,
            policy_conflict=state.policy.requested_policy_conflict if state else None,
            settings_revision=state.settings_revision if state else None,
            blocker=('shutdown_failed' if failed else self._blocker or self._config_blocker),
            observed_at=self._last_status_at)
        return {'schema_version': SCHEMA_VERSION, 'scope': self._scope, **redact(document)}

    def _observe(self, context: RecoveryContext) -> None:
        # Observation time is the scan's own capture time, not publish time.
        self._last_status_at = time.time() - max(0., time.monotonic() - context.observed_at_monotonic)
        scope = context.scope
        self._scope = dict(worker=scope.worker, account_id=scope.account_id,
                           lease_id=scope.lease_id, attempt_id=scope.attempt_id,
                           attempt_generation=scope.attempt_generation)

    # -- gates ---------------------------------------------------------------
    def _snapshot_current(self) -> bool:
        snapshot = self.storage.snapshot()
        if (snapshot is None or self.storage.failed or self._snapshot is None
                or time.monotonic() - snapshot[2] > SNAPSHOT_MAX_AGE_SECONDS):
            return False
        state, grants, *_ = snapshot
        old_state, old_grants, *_ = self._snapshot
        return ((state.settings_revision, state.policy.active, grants)
                == (old_state.settings_revision, old_state.policy.active, old_grants))

    def _safe(self, current: RecoveryContext, *, request: bool = False) -> bool:
        now = time.monotonic()
        if (self._closed or self._invalidated or current.paused or current.identity_conflict
                or current.pending_transaction
                or not 0 <= now - current.observed_at_monotonic <= FRESH_SCAN_SECONDS
                or self._origin is None or current.scope != self._origin.scope
                or not self._snapshot_current()):
            return False
        if request:
            return (self.request is not None and now < self.request.deadline_at_monotonic
                    and proposal_is_current(self.request, current))
        return True

    def _capable(self, context: RecoveryContext, candidate: RecoveryCandidate) -> bool:
        return bool(self._snapshot and any(g.allows(worker=self.worker,
            account_id=context.scope.account_id, model=self.settings.model, screen=context.screen,
            target=candidate.target, action=candidate.action_id, now=time.time())
            for g in self._snapshot[1]))

    def _candidate(self, current: RecoveryContext) -> RecoveryCandidate | None:
        """Fresh-validation guard: scope, freshness, pause/identity/pending,
        expiry, offered and current candidate, mode and capability."""
        if self._proposal is None or not self._safe(current, request=self._source == 'model'):
            return None
        previous = self.request.context if self._source == 'model' and self.request else self._origin
        if previous is None or current.screen != previous.screen:
            return None
        try:
            offered = validate_proposal(self._proposal, previous)
            fresh = validate_proposal(self._proposal, current)
        except ValueError:
            return None
        if offered is None or fresh != offered:
            return None
        if self._source == 'model' and (self.settings.mode != 'assist'
                                        or not self._capable(current, fresh)
                                        or not self._model_authority_current()):
            return None
        return fresh

    def _model_authority_current(self) -> bool:
        """Input-boundary authority check without SQLite on this thread.

        Supported writers (settings save, policy update, capability
        activate/revoke) replace the config epoch right after committing, so an
        unchanged epoch means no such commit finished since the accepted
        snapshot, up to that writer's commit-to-replace gap. Writers that bypass
        the epoch are bounded by the snapshot age limit.
        """
        snapshot = self.storage.snapshot()
        if snapshot is None or self._snapshot is None:
            return False
        accepted = self._snapshot[3]
        return (time.monotonic() - snapshot[2] <= MODEL_INPUT_SNAPSHOT_SECONDS
                and snapshot[3] == accepted and read_config_epoch(self.root) == accepted)

    # -- storage commands ----------------------------------------------------
    def _schedule(self, operation: str, command: Callable[[Stores], Any], phase: str) -> None:
        """State moves now; the command is retried until the slot accepts it."""
        self._pending, self._phase = (operation, command), phase
        self._flush()

    def _flush(self) -> None:
        if self._pending is not None and self.storage.submit(*self._pending):
            self._pending = None

    def _with_calls(self, stores: Stores, episode: RecoveryEpisode | None) -> int | None:
        return (stores.budget.calls_remaining(incident_id=episode.incident_id)
                if episode is not None else None)

    def _write_outcome(self, outcome: str, then: str) -> None:
        episode, namespace, action = self._episode, self._namespace, self._action
        assert episode is not None and namespace is not None and action is not None
        def record(stores: Stores) -> Any:
            updated = stores.episodes.record_outcome(namespace=namespace,
                incident_id=episode.incident_id, action_id=action, outcome=outcome)
            return updated, self._with_calls(stores, updated)
        self._next = then
        self._schedule('outcome', record, 'recording')

    def _finish(self, outcome: str) -> None:
        episode, namespace = self._episode, self._namespace
        if episode is None or namespace is None:
            self._outcome, self._phase = outcome, 'paused'
            return
        def finish(stores: Stores) -> Any:
            current = stores.episodes.get(namespace=namespace, incident_id=episode.incident_id)
            closed = stores.episodes.close(namespace=namespace, incident_id=episode.incident_id,
                expected_revision=current.revision, outcome=outcome, now=time.time())
            return closed, self._with_calls(stores, closed)
        self._schedule('close', finish, 'closing')

    def _open(self, context: RecoveryContext) -> None:
        self._origin = context
        self._namespace = namespace = EpisodeNamespace(self.worker, context.scope.account_id)
        fingerprint, token = semantic_fingerprint(context), scope_token(context)
        cooldown = self.settings.cooldown_seconds
        def open_episode(stores: Stores) -> Any:
            now = time.time()
            previous = stores.episodes.lookup(namespace=namespace, fingerprint=fingerprint)
            if previous and previous.closed_at is not None and now < previous.closed_at + cooldown:
                result = EpisodeOpenResult(previous, False, 'cooldown')  # Stricter configured cooldown.
            else:
                result = stores.episodes.get_or_open(namespace=namespace, fingerprint=fingerprint,
                                                     scope_token=token, now=now)
                if result.blocker == 'scope_mismatch':
                    if result.episode.status != 'open':
                        # Unresolved/exhausted incidents are never rebound or replayed.
                        result = EpisodeOpenResult(result.episode, False, result.episode.status)
                    else:
                        # Restart/new lease: adopt the incident, keeping its allowance.
                        adopted = stores.episodes.rebind_scope(namespace=namespace,
                            incident_id=result.episode.incident_id,
                            expected_revision=result.episode.revision, scope_token=token)
                        result = EpisodeOpenResult(adopted, False, None)
            if (result.episode.status == 'exhausted' and result.episode.closed_at is None
                    and not result.episode.unresolved_action_ids):
                # Every outcome is known but the close never committed (crash or
                # failed reply): close it now so the cooldown starts.
                closed = stores.episodes.close(namespace=namespace,
                    incident_id=result.episode.incident_id,
                    expected_revision=result.episode.revision, outcome='exhausted', now=now)
                result = EpisodeOpenResult(closed, False, 'cooldown')
            return result, self._with_calls(stores, result.episode)
        self._schedule('open', open_episode, 'opening')

    def _reserve(self, context: RecoveryContext) -> None:
        candidate = self._candidate(context)
        if candidate is None:
            self._finish('invalidated')
            return
        episode, namespace = self._episode, self._namespace
        assert episode is not None and namespace is not None
        action, token, source = uuid4().hex, scope_token(context), self._source
        state, grants, *_ = self._snapshot
        expected = (state.settings_revision, state.policy.active.revision)
        root = self.root
        def reserve(stores: Stores) -> Any:
            # Durable re-check at reservation: a settings, policy or capability
            # change since this reply was accepted denies the reservation before
            # any allowance is consumed. Later changes are caught at the input
            # boundary by _model_authority_current (config epoch + age bound).
            revision, _raw, policy = stores.budget.settings_snapshot()
            current = (revision, policy.revision) == expected and (
                source != 'model' or CapabilityRegistry(root).active(now=time.time()) == grants)
            granted = current and stores.episodes.reserve_action(namespace=namespace,
                incident_id=episode.incident_id, expected_revision=episode.revision,
                scope_token=token, action_id=action, source=source)
            updated = stores.episodes.get(namespace=namespace, incident_id=episode.incident_id)
            return granted, current, updated, self._with_calls(stores, updated), time.monotonic()
        self._action = action
        self._schedule('reserve', reserve, 'reserving')

    def _receive(self, reply: Any) -> None:
        if reply.failed:
            self._phase, self._blocker = 'paused', 'storage_unavailable'
            return
        if reply.operation == 'open':
            result, calls = reply.value
            self._episode, self._phase = result.episode, 'choosing'
            if result.blocker:
                self._phase, self._blocker = 'paused', result.blocker
        elif reply.operation == 'reserve':
            granted, current, self._episode, calls, self._reserved_at = reply.value
            if granted:
                self._phase = 'dispatch'
            elif not current:
                self._action, self._invalidated = None, True
                self._phase, self._blocker = 'validate', 'settings_changed'
            else:
                self._action, self._phase, self._blocker = None, 'paused', 'action_denied'
        elif reply.operation == 'outcome':
            self._episode, calls = reply.value
            if self._next == 'retry':
                self._phase, self._action, self._dispatched = 'choosing', None, False
            elif self._next == 'unknown':
                self._phase, self._outcome, self._blocker = 'paused', 'unknown', 'unresolved'
            else:
                self._finish(self._next or 'invalidated')
        elif reply.operation == 'close':
            self._episode, calls = reply.value
            self._outcome = self._episode.outcome
            # A stall that cleared by itself closes (cooldown applies) and
            # returns the lane without pausing the worker.
            self._phase = ('recovered' if self._outcome == 'recovered' or self._released
                           else 'paused')
        else:
            return
        if calls is not None:
            self._calls_remaining, self._calls_observed = calls, time.time()

    # -- scan entry point ----------------------------------------------------
    def step(self, context: RecoveryContext, *, stalled: bool, deterministic: bool = False,
             dispatch: Callable[[RecoveryCandidate, Callable[[], bool]], Any] | None = None,
             live_context: Callable[[], RecoveryContext] | None = None,
             semantic_postcondition: bool = False, pause: Callable[[], None] | None = None,
             image: Callable[[], RecoveryImage | None] | None = None) -> bool:
        """Advance one scan. True means recovery owns the action lane.

        ``image`` lazily supplies this scan's bounded, redacted frame; it is
        called only when a provider request is about to be built.

        ``context`` is this completed scan (fresh, scan-owned). ``stalled``
        is the watchdog's deterministic-exhaustion signal for a responsive
        scan. ``deterministic`` offers context's first host candidate as a
        deterministic action. ``dispatch(candidate, guard)`` must send one
        input through DeviceSupervisor.recovery_tap(..., guard=guard).
        ``live_context`` rebuilds a context from current scope/gates for the
        final guard. ``semantic_postcondition`` is the host's verified check of
        the chosen candidate's expected postcondition on this scan. ``pause``
        takes the existing deterministic pause path.
        """
        self._observe(context)
        self._image_provider = image
        try:
            return self._step(context, stalled=stalled, deterministic=deterministic,
                              dispatch=dispatch, live_context=live_context,
                              semantic_postcondition=semantic_postcondition, pause=pause)
        finally:
            self._flush()
            if not self._closed:
                self.storage.publish(self.status())

    def _step(self, context: RecoveryContext, *, stalled: bool, deterministic: bool,
              dispatch: Any, live_context: Any, semantic_postcondition: bool, pause: Any) -> bool:
        if self._closed:
            return False
        if not self._configuration():
            if self._origin is None:
                return False  # Nothing owned: the existing watchdog path decides.
            if self.storage.failed:
                self._phase, self._blocker = 'paused', 'storage_unavailable'
        if self._origin is not None and not self._safe(context) and self._phase not in {
                'paused', 'recovered'}:
            self._invalidated = True
            if self.service is not None:
                self.service.invalidate(reason='scope_changed')
        reply = self.storage.poll()
        if reply is not None:
            self._receive(reply)
        if self._phase == 'recovered':
            self._clear()
            return False
        if self._phase == 'paused':
            if self._pause_requested:
                if not context.paused:
                    self._clear()  # Operator resumed; durable state still gates reopening.
                    return False
                return True
            if pause is not None:
                if not context.paused:
                    pause()  # Existing deterministic pause path, requested once.
                self._pause_requested = True  # An operator pause already counts.
                return True
            if not stalled:
                self._clear()
                return False
            return True
        if self._origin is None:
            if not stalled or self.settings.mode == 'off' or self.service is None:
                return False
            if context.paused or context.pending_transaction or context.identity_conflict:
                return False  # Safety gates: no mutation; pause/pending paths are the host's.
            self._open(context)
            return True
        if self._phase in _WAITING:
            return True
        if not stalled and self._phase in {'choosing', 'provider'}:
            self._invalidated = self._released = True  # The stall cleared before any action.
        if self._invalidated:
            if self._phase == 'dispatch':
                self._write_outcome('not_dispatched', 'invalidated')
            elif self._phase == 'postcondition':
                self._write_outcome('unknown', 'unknown')
            else:
                if self.service is not None and self._phase == 'provider':
                    self.service.invalidate(reason='invalidated')
                self._finish('invalidated')
            return True
        if self._phase == 'choosing':
            self._choose(context, deterministic)
        elif self._phase == 'provider':
            self._provider(context)
        elif self._phase == 'validate':
            self._reserve(context)
        elif self._phase == 'dispatch' and dispatch is not None:
            self._dispatch(context, dispatch, live_context)
        elif self._phase == 'postcondition':
            if semantic_postcondition and context.observed_at_monotonic > self._dispatched_at:
                self._write_outcome('confirmed', 'recovered')
            elif time.monotonic() - self._dispatched_at >= POSTCONDITION_SECONDS:
                self._write_outcome('no_effect', 'retry')
        return True

    def _choose(self, context: RecoveryContext, deterministic: bool) -> None:
        assert self._episode is not None
        status = self.service.status() if self.service is not None else None
        if self._episode.actions_used >= self.settings.max_actions:
            self._finish('exhausted')
        elif self._episode.actions_used == 0 and deterministic and context.candidates:
            candidate = context.candidates[0]
            self._source = 'deterministic'
            self._proposal = RecoveryProposal(action_id=candidate.action_id,
                candidate_id=candidate.candidate_id,
                expected_postcondition=candidate.expected_postcondition,
                explanation='host verified candidate')
            self._reserve(context)
        elif self._retiring is not None and not self._retiring.status().shutdown_failed:
            return  # Wait for confirmed shutdown before a replacement service.
        elif status is None or not status.configured or self.settings.mode == 'off':
            self._blocker = self._config_blocker or (status.blocker if status else None)
            self._finish('exhausted')
        elif self._calls_remaining == 0:
            self._blocker = 'budget_exhausted'
            self._finish('exhausted')
        elif self.settings.mode == 'assist' and not context.candidates:
            # Nothing host-verified to act on: a paid call could not lead to input.
            self._blocker = 'no_candidates'
            self._finish('exhausted')
        else:
            image = None
            if self._image_provider is not None:
                try:
                    image = self._image_provider()
                except Exception:  # noqa: BLE001 - no frame means no request
                    image = None
                if image is None:
                    self._blocker = 'image_unavailable'
                    self._finish('exhausted')
                    return
            now = time.monotonic()
            self.request = RecoveryRequest(request_id=uuid4().hex,
                incident_id=self._episode.incident_id, fingerprint=self._episode.fingerprint,
                context=context, created_at_monotonic=now,
                deadline_at_monotonic=now + self.settings.deadline_seconds,
                objective='Recover a responsive stalled navigation without spending.',
                image=image)
            if self.service.submit(self.request):
                self._phase = 'provider'
            else:
                self._blocker = self.service.status().blocker or 'service_unavailable'
                self._finish('exhausted')

    def _provider(self, context: RecoveryContext) -> None:
        assert self.request is not None
        result = self.service.poll(incident_id=self.request.incident_id,
                                   request_id=self.request.request_id)
        if result is None:
            if time.monotonic() >= self.request.deadline_at_monotonic:
                self.service.invalidate(reason='deadline')
                self._blocker = 'timeout'
                self._finish('exhausted')
            return
        if self._calls_remaining is not None:
            self._calls_remaining = None  # Durable count is re-read with the next command.
        if result.proposal is None:
            self._blocker = result.error
            self._finish('exhausted')
            return
        proposal = result.proposal
        # Only the host-owned identifiers are kept; explanation text is not.
        self._last_proposal = dict(action_id=proposal.action_id,
                                   candidate_id=proposal.candidate_id,
                                   expected_postcondition=proposal.expected_postcondition)
        self._proposal = proposal
        if self.settings.mode == 'shadow':
            self._outcome, self._blocker = 'shadow_proposal', 'shadow_mode'
            self._finish('exhausted')  # Shadow follows the deterministic pause path.
        elif not self._safe(context, request=True):
            self._finish('invalidated')
        else:
            self._source, self._phase = 'model', 'validate'
            self._reserve(context)

    def _dispatch(self, context: RecoveryContext, dispatch: Any, live_context: Any) -> None:
        snapshot = self.storage.snapshot()
        if snapshot is None or snapshot[2] <= self._reserved_at:
            return  # Wait for a settings/capability snapshot newer than the reservation.
        def fresh_candidate() -> RecoveryCandidate | None:
            try:
                return self._candidate(live_context() if live_context else context)
            except Exception:
                return None  # A failing live read refuses input; it never raises.
        candidate = fresh_candidate()
        if candidate is None:
            self._write_outcome('not_dispatched', 'invalidated')
            return
        def guard() -> bool:
            return fresh_candidate() == candidate
        self._dispatched, self._dispatched_at = True, time.monotonic()
        try:
            # DeviceSupervisor.recovery_tap re-invokes guard after its durable
            # input checkpoint; a refusal raises before any device input.
            dispatch(candidate, guard)
        except RecoveryPreflightBlocked:
            self._dispatched = False  # Provably no input: the slot stays consumed.
            self._write_outcome('not_dispatched', 'invalidated')
            return
        except Exception:
            self._write_outcome('unknown', 'unknown')
            return
        self._phase = 'postcondition'
        if live_context is not None and self._origin is not None:
            # Adopt only the command epoch our own input advanced; any other
            # scope change leaves the postcondition unclassifiable.
            try:
                after = live_context()
            except Exception:
                after = None
            expected = self._origin.scope.device_command_generation + 1
            same = after is not None and after.scope.device_command_generation == expected and (
                after.scope.model_copy(update={'device_command_generation': expected - 1})
                == self._origin.scope)
            if same:
                self._origin = self._origin.model_copy(update={'scope': after.scope})
            else:
                self._invalidated = True
