"""Acknowledged permanent account facts, isolated from transient battle observations."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
from typing import Any

import db
import ultimate_weapons
from concepts import REGISTRY
from modules import ModulesInventory
from perception import Observation
from account_screens import ScreenReadings
from evidence_scope import FactScope, BalanceInterval, ScopeContinuity, verified_continuity
from currencies import CurrencyRepository
from fleet.identity import IdentityEvidence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Evidence:
    observed_at: float
    confidence: float
    raw_name: str
    raw_value: str | None
    rect: tuple[int, int, int, int]
    frame_width: int
    frame_height: int
    frame_digest: str
    frame_ref: str | None = None


@dataclass(frozen=True)
class Fact:
    concept_id: str
    value: float | int | str | bool | None
    status: str
    evidence: Evidence
    scope: FactScope | None = None
    catalog_revision: str | None = None
    modifier_revision: str | None = None
    source: str = "observed"


@dataclass(frozen=True)
class AccountRevision:
    revision_id: int | None = None
    parent_revision_id: int | None = None
    created_at: float | None = None
    account_id: str | None = None
    game_version: str | None = None
    registry_version: str = REGISTRY.registry_version
    workshop_stats: tuple[Fact, ...] = ()
    workshop_levels: tuple[Fact, ...] | None = None
    lab_levels: tuple[Fact, ...] | None = None
    effective_account_stats: tuple[Fact, ...] | None = None
    inventory: tuple[Fact, ...] | None = None
    unlocks: tuple[Fact, ...] | None = None
    settings: tuple[Fact, ...] | None = None
    # One Fact per RUNNING research job, identified by the lab occupying the
    # slot and valued by the absolute second it completes. Kept apart from
    # `lab_levels` because a level is what the account owns and a job is what
    # it is spending: a job ends, the level it bought does not.
    lab_jobs: tuple[Fact, ...] | None = None
    # How many lab slots the account owns. A plain count, like `game_version`,
    # because a slot has no catalog identity to hang a Fact on. None means
    # nobody has counted them, never that the account owns none.
    lab_slots_owned: int | None = None
    modules: ModulesInventory | None = None
    # Card slot counts, read off the Cards page. None means that page has
    # never been read, which is not the same account fact as a card
    # collection that is empty - see cards.py.
    cards: tuple[Fact, ...] | None = None
    # Kept out of the Fact sections above on purpose. A UW reading holds raw
    # stone quantities and lab-adjusted ones in separately typed collections,
    # and a flat Fact - one concept_id, one value - has nowhere to carry that
    # difference, so storing UWs there would erase it.
    ultimate_weapons: ultimate_weapons.UltimateWeaponsRecord | None = None


@dataclass(frozen=True)
class RunObservation:
    run_id: int
    observed_at: float
    effective_stats: tuple[Fact, ...]
    purchased_levels: tuple[Fact, ...] | None = None
    registry_version: str = REGISTRY.registry_version
    game_version: str | None = None


def _encoded(value: Any) -> str:
    return json.dumps(asdict(value), allow_nan=False, sort_keys=True)


class AccountRepository:
    """Short transactions on the telemetry database, independent of its lossy queue."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        conn = db.connect(self.path)
        conn.close()

    def latest(self) -> AccountRevision | None:
        with db.reader(self.path) as conn:
            row = conn.execute('SELECT id, detail FROM account_revisions ORDER BY id DESC LIMIT 1').fetchone()
        if row is None:
            return None
        value = json.loads(row['detail'])
        value['revision_id'] = row['id']
        for section in ('workshop_stats', 'workshop_levels', 'lab_levels', 'lab_jobs',
                        'effective_account_stats', 'inventory', 'unlocks', 'settings',
                        'cards'):
            if value.get(section) is not None:
                value[section] = tuple(Fact(f['concept_id'], f['value'], f['status'],
                    Evidence(**{**f['evidence'], 'rect': tuple(f['evidence']['rect'])}),
                    FactScope(**f['scope']) if f.get('scope') else None,
                    f.get('catalog_revision'), f.get('modifier_revision'), f.get('source', 'observed')) for f in value[section])
        if value.get('modules') is not None:
            value['modules'] = ModulesInventory.from_payload(value['modules'])
        if value.get('ultimate_weapons') is not None:
            # Revalidated on the way out, so a row edited in the database
            # cannot smuggle a lab-adjusted value into a raw stone collection.
            value['ultimate_weapons'] = ultimate_weapons.decode(value['ultimate_weapons'])
        return AccountRevision(**value)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=1.)
        conn.execute('PRAGMA busy_timeout=1000')
        return conn

    def save_account(self, revision: AccountRevision, facts: tuple[Fact, ...]) -> AccountRevision:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute('INSERT INTO account_revisions(detail) VALUES (?)', (_encoded(revision),))
                revision_id = cursor.lastrowid
                conn.execute('INSERT INTO account_observations(revision_id, detail) VALUES (?, ?)',
                             (revision_id, json.dumps([asdict(f) for f in facts], allow_nan=False)))
            return replace(revision, revision_id=revision_id)
        finally:
            conn.close()

    def save_run(self, observation: RunObservation) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute('INSERT INTO run_observations(run_id, observed_at, detail) VALUES (?, ?, ?)',
                             (observation.run_id, observation.observed_at, _encoded(observation)))
        finally:
            conn.close()


def completed_lab_level(status: Any, value: Any) -> int | None:
    """The one reading of a Lab picker level (Task5 ruling).

    An ``available`` picker Lv.N names the next target, so N-1 is completed;
    every other status keeps its level as read (callers filter statuses).
    """
    if type(value) is not int:
        return None
    if value < 0:
        return None
    if status == 'available':
        return max(0, value - 1)  # A Lv.0 picker reading cannot mean -1.
    return value


class AccountState:
    def __init__(self, repository: AccountRepository | None = None) -> None:
        self.repository = repository
        self.screen_readings = ScreenReadings()
        self._scope: FactScope | None = None
        self._identity: IdentityEvidence | None = None
        self._runtime_root: Path | None = None
        self.currencies = CurrencyRepository(repository.path) if repository else None
        self._lock = threading.RLock()
        self._revision: AccountRevision | None = None
        self._error: str | None = None
        self._run_error: str | None = None
        self._restored = repository is None
        self._pending: dict[str, Fact] = {}
        self._run_id: int | None = None
        self._run_values: dict[str, float] = {}
        self._run_saved_at: float | None = None
        if repository is not None:
            try:
                self._revision = repository.latest()
                self._restored = True
            except (sqlite3.Error, ValueError, TypeError) as exc:
                self._error = str(exc)

    @property
    def safety_path(self) -> Path | None:
        return self.currencies.path if self.currencies else None

    def attach_safety_storage(self, path: Path) -> None:
        """The spending journal persists even when optional event/history storage is off."""
        with self._lock:
            if self.currencies is not None:
                if self.currencies.path.resolve() != Path(path).resolve():
                    raise ValueError('account facts and purchases must share one database')
                return
            self.currencies = CurrencyRepository(path)

    @property
    def scope(self) -> FactScope | None:
        return self._scope

    @property
    def verified_scope(self) -> FactScope | None:
        return self._scope if self._identity is not None else None

    @property
    def persisted_epoch(self) -> int:
        previous = self.currencies.current_scope() if self.currencies else None
        return previous.epoch if previous else 0

    def bind_scope(self, scope: FactScope, *, identity: IdentityEvidence,
                   runtime_root: Path | None = None) -> None:
        if identity.account_id != scope.account_id:
            raise ValueError('account identity does not match facts')
        with self._lock:
            if self.currencies:
                previous = self.currencies.current_scope()
                if previous is not None and previous.account_id == scope.account_id and scope.epoch < previous.epoch:
                    raise ValueError('fact epoch cannot move backwards')
                self.currencies.bind_scope(scope)
            if self._scope != scope:
                self._pending.clear()
                self._run_values.clear()
                self._run_saved_at = None
            self._scope, self._identity, self._runtime_root = scope, identity, runtime_root

    def invalidate_scope(self, reason: str) -> None:
        """Explicit manual/reconnect invalidation; OCR misses only clear candidates."""
        if not reason:
            raise ValueError('invalidation reason required')
        with self._lock:
            scope = self._scope or (self.currencies.current_scope() if self.currencies else None)
            if scope is not None:
                self._scope = replace(scope, epoch=scope.epoch + 1)
                if self.currencies:
                    self.currencies.bind_scope(self._scope)
            self._identity = None
            self._pending.clear()
            self._run_values.clear()

    def continuity(self, original: FactScope, *, now: float) -> ScopeContinuity | None:
        if self._scope is None or self._identity is None or self._runtime_root is None:
            return None
        current = replace(self._scope, run_id=self._run_id) if original.run_id is not None else self._scope
        proof = verified_continuity(self._runtime_root, original, current,
                                    identity=self._identity, now=now)
        if proof is None and original.epoch < current.epoch:
            # Ruling: an intent invalidated only by an epoch bump reconciles
            # read-only once the re-verified identity matches its account.
            proof = verified_continuity(self._runtime_root, original, current,
                                        identity=self._identity, now=now, epoch_advance=True)
        return proof

    def actionable_facts(self) -> tuple[Fact, ...]:
        with self._lock:
            if self._scope is None or self._identity is None or self._revision is None:
                return ()
            return tuple(f for f in self._revision.workshop_stats
                         if f.scope == self._scope and f.status == 'verified'
                         and f.catalog_revision == REGISTRY.registry_version)

    def observe_balance(self, balance: BalanceInterval) -> bool:
        with self._lock:
            expected = replace(self._scope, run_id=self._run_id) if self._scope and balance.currency == 'cash' else self._scope
            if (not self.currencies or self._identity is None or expected != balance.scope
                    or balance.observed_at < self._identity.observed_at):
                return False
            return self.currencies.observe(balance)

    def accepts_capture(self, scope: FactScope, observed_at: float) -> bool:
        """A producer may tag only evidence captured under the still-verified identity."""
        with self._lock:
            return (self._identity is not None and self._scope == scope
                    and observed_at >= self._identity.observed_at)

    def identity_fresh(self, now: float, max_age: float = 30.) -> bool:
        """Bounded inspection may navigate only under a recent account identity."""
        with self._lock:
            return (self._scope is not None and self._identity is not None
                    and self._identity.account_id == self._scope.account_id
                    and 0 <= now-self._identity.observed_at <= max_age)

    def lab_facts(self, runtime: Any, *, now: float) -> Any | None:
        """Adapt only current confirmed facts to L3; timers never imply completion."""
        from fleet.resource_blocks import LabFacts
        from lab_runtime import LabScope, _catalog_revision
        scope = self.verified_scope
        if scope is None or self.currencies is None:
            return None
        lab_scope = LabScope(scope.account_id, scope.lease_id, scope.generation, scope.epoch)
        if (getattr(runtime, 'scope', None) != lab_scope
                or type(getattr(runtime.scope, 'epoch', None)) is not int):
            return None
        slots = {r.slot: asdict(r) for r in runtime.slots if r.scope == lab_scope and r.confirmed
                 and r.observed_at is not None and 0 <= now-r.observed_at <= 30}
        balance = self.currencies.balance('coins', scope=scope, now=now)
        available = self.currencies.available(balance) if balance is not None else None
        gems = self.currencies.balance('gems', scope=scope, now=now)
        completed = {f.concept_id: completed_lab_level(f.status, f.value)
                     for f in (self._revision.lab_levels or ())
                     if f.scope == scope and f.status in ('verified', 'available', 'maxed')
                     and type(f.value) is int and f.value >= (1 if f.status == 'available' else 0)
                     and f.catalog_revision == _catalog_revision()} if self._revision else {}
        from transactions import TransactionJournal
        pending = TransactionJournal(self.currencies.path).open_transactions()
        reserved = frozenset(t.before['research_id'] for t in pending
                             if t.scope is not None and t.scope.account_id == scope.account_id
                             and isinstance(t.before.get('research_id'), str))
        return LabFacts(now=now, wallet_coins=balance.lower if balance else None,
                        wallet_gems=self.currencies.available(gems) if gems is not None else None,
                        available_coins=available, completed_levels=completed, slots=slots,
                        running_research=frozenset(r['research_id'] for r in slots.values()
                            if r['state'] == 'researching' and r.get('research_id')),
                        reserved_research=reserved, account_id=scope.account_id, scope=lab_scope)

    def lab_action(self, plan: Any, runtime: Any, *, revision: int, now: float) -> Any | None:
        from fleet.resource_blocks import choose_lab_action
        facts = self.lab_facts(runtime, now=now)
        if (facts is None or type(revision) is not int or revision < 0
                or plan.strategy_revision != revision or plan.scope != facts.scope
                or type(plan.evaluated_at) not in (float, int)
                or not 0 <= now-plan.evaluated_at <= 30):
            return None
        action = choose_lab_action(plan, runtime, available_coins=facts.available_coins)
        if (action is None or action.slot not in facts.slots
                or action.research in facts.reserved_research or action.research in facts.running_research):
            return None
        return action

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {'persistence_available': self.repository is not None,
                    'scope': asdict(self._scope) if self._scope else None,
                    'screen_readings': self.screen_readings.snapshot(),
                    'error': self._error or self._run_error,
                    'errors': {'account': self._error, 'run': self._run_error},
                    'revision': json.loads(_encoded(self._revision)) if self._revision else None,
                    'unknown_state': json.loads(_encoded(AccountRevision())) if self._revision is None else None}

    def _facts(self, observation: Observation, context: str) -> tuple[Fact, ...]:
        if observation.context != context or any(row.context != context for row in observation.rows):
            raise ValueError(f'Expected {context} observation')
        if (observation.category is None or not observation.frame_digest
            or not math.isfinite(observation.observed_at)):
            return ()
        if self._identity is not None and observation.observed_at < self._identity.observed_at:
            return ()
        facts = []
        for row in observation.rows:
            if (row.concept_id is None or row.value is None or not math.isfinite(row.value)
                or not math.isfinite(row.confidence) or not .9 <= row.confidence <= 1
                or row.status not in ('available', 'maxed')
                or row.category != observation.category):
                continue
            rect = row.rect
            facts.append(Fact(row.concept_id, row.value, 'observed', Evidence(
                observation.observed_at, row.confidence, row.raw_name, row.raw_value,
                (rect.x, rect.y, rect.w, rect.h), observation.frame_width,
                observation.frame_height, observation.frame_digest),
                replace(self._scope, run_id=self._run_id) if self._scope and context == "battle" else self._scope,
                REGISTRY.registry_version, None, "observed"))
        # Duplicate identities are ambiguous, even when their values agree.
        return tuple(f for f in facts if sum(other.concept_id == f.concept_id for other in facts) == 1)

    def reset_confirmation(self) -> None:
        """Discard candidates when a screen or bot lifetime changes."""
        with self._lock:
            self._pending.clear()

    def observe_account(self, observation: Observation) -> None:
        with self._lock:
            facts = self._facts(observation, 'workshop')
            previous = self._pending
            self._pending = {f.concept_id: f for f in facts}
            if self.repository is None:
                return
            if not self._restored:
                try:
                    self._revision = self.repository.latest()
                    self._restored = True
                    self._error = None
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._error = str(exc)
                    return
            known = {f.concept_id: f for f in self._revision.workshop_stats} if self._revision else {}
            changed = tuple(replace(f, status='verified') for f in facts
                if f.concept_id in previous and previous[f.concept_id].value == f.value
                and 0 < f.evidence.observed_at - previous[f.concept_id].evidence.observed_at <= 30
                and (f.concept_id not in known or known[f.concept_id].value != f.value
                     or known[f.concept_id].scope != f.scope))
            if not changed:
                return
            known.update({f.concept_id: f for f in changed})
            candidate = replace(self._revision or AccountRevision(), revision_id=None,
                parent_revision_id=self._revision.revision_id if self._revision else None,
                created_at=observation.observed_at,
                account_id=self._scope.account_id if self._scope else None,
                workshop_stats=tuple(known[k] for k in sorted(known)))
            try:
                self._revision = self.repository.save_account(candidate, changed)
                self._error = None
            except sqlite3.Error as exc:
                self._error = str(exc)
                logger.error('Account persistence failed: %s', exc)

    def observe_run(self, observation: Observation, run_id: int | None) -> None:
        with self._lock:
            if run_id != self._run_id:
                self._run_id, self._run_values, self._run_saved_at = run_id, {}, None
                if self.currencies:
                    self.currencies.set_run(run_id)
            if (self.verified_scope is not None and observation.frame_digest
                    and observation.cash is not None and run_id is not None):
                self.observe_balance(BalanceInterval.from_reading('cash', observation.cash,
                    replace(self.verified_scope, run_id=run_id), observation.observed_at, observation.frame_digest))
            facts = self._facts(observation, 'battle')
            self._pending.clear()
            if self.repository is None or run_id is None or not facts:
                return
            if run_id != self._run_id:
                self._run_id, self._run_values, self._run_saved_at = run_id, {}, None
            values = {f.concept_id: f.value for f in facts}
            if all(self._run_values.get(k) == v for k, v in values.items()):
                return
            if self._run_saved_at is not None and observation.observed_at - self._run_saved_at < 10:
                return
            try:
                self.repository.save_run(RunObservation(run_id, observation.observed_at, facts))
                self._run_values.update(values)
                self._run_saved_at = observation.observed_at
                self._run_error = None
            except sqlite3.Error as exc:
                self._run_error = str(exc)
                logger.error('Run persistence failed: %s', exc)

    def record_labs(self, levels: tuple[Fact, ...] = (), jobs: tuple[Fact, ...] | None = None,
                    slots_owned: int | None = None, *, observed_at: float) -> bool:
        """Merge confirmed lab facts into a revision; say whether one was written.

        Every argument is already confirmed by its caller, which is why these
        Facts carry the state the game showed rather than a persistence
        status: nothing provisional reaches this method.

        Levels MERGE by concept id, because the Labs page shows a scrolled
        window and a lab that fell off the top has not gone anywhere. Jobs
        REPLACE wholesale, so a finished slot actually empties - but only
        when the caller read the whole strip, since a completion nobody
        overwrote would outlive the research it described. `None` for jobs or
        for the slot count means no evidence this time, which leaves the
        stored answer standing rather than erasing it.
        """
        with self._lock:
            if self.repository is None:
                return False
            if any(f.scope is not None and not self.accepts_capture(f.scope, f.evidence.observed_at)
                   for f in levels + tuple(jobs or ())):
                return False
            if not self._restored:
                try:
                    self._revision, self._restored, self._error = self.repository.latest(), True, None
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._error = str(exc)
                    return False
            current = self._revision or AccountRevision()
            known = {f.concept_id: f for f in current.lab_levels or ()}
            # Compared by value, not by Fact: a re-reading of a level we
            # already hold is the same fact with newer evidence, and writing
            # a revision for it would fill the history with nothing.
            fresh = tuple(f for f in levels
                          if known.get(f.concept_id) is None or known[f.concept_id].value != f.value
                          or known[f.concept_id].scope != f.scope
                          or known[f.concept_id].catalog_revision != f.catalog_revision)
            known.update({f.concept_id: f for f in fresh})
            merged = (current.lab_jobs if jobs is None
                      else tuple(sorted(jobs, key=lambda f: f.concept_id)))
            same_jobs = jobs is None or ([(f.concept_id, f.value) for f in current.lab_jobs or ()]
                                         == [(f.concept_id, f.value) for f in merged])
            if not fresh and same_jobs and slots_owned in (None, current.lab_slots_owned):
                return False
            candidate = replace(current, revision_id=None,
                parent_revision_id=current.revision_id, created_at=observed_at,
                lab_levels=tuple(known[k] for k in sorted(known)) or None, lab_jobs=merged,
                lab_slots_owned=current.lab_slots_owned if slots_owned is None else slots_owned)
            try:
                self._revision = self.repository.save_account(candidate, fresh + tuple(jobs or ()))
                self._error = None
                return True
            except sqlite3.Error as exc:
                self._error = str(exc)
                logger.error('Lab persistence failed: %s', exc)
                return False
    def observe_modules(self, inventory: ModulesInventory) -> None:
        """Save a module inventory a reader actually resolved.

        `unknown` and `unreadable` inventories say nothing about the account
        and must never replace what an earlier reading proved, so they are
        dropped here rather than written as an empty loadout. An unchanged
        inventory writes no revision, exactly as an unchanged workshop value
        does. Confirming a reading across two frames belongs with the reader
        that produces one; there is no recorded Modules capture yet.
        """
        with self._lock:
            if self.repository is None or inventory.status not in ('observed', 'locked', 'unavailable'):
                return
            if not self._restored:
                try:
                    self._revision = self.repository.latest()
                    self._restored = True
                    self._error = None
                except (sqlite3.Error, ValueError, TypeError) as exc:
                    self._error = str(exc)
                    return
            if self._revision is not None and self._revision.modules == inventory:
                return
            candidate = replace(self._revision or AccountRevision(), revision_id=None,
                parent_revision_id=self._revision.revision_id if self._revision else None,
                created_at=inventory.observed_at, modules=inventory)
            try:
                self._revision = self.repository.save_account(candidate, ())
                self._error = None
            except sqlite3.Error as exc:
                self._error = str(exc)
                logger.error('Module persistence failed: %s', exc)
