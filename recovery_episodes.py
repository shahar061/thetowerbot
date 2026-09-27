"""Durable recovery eligibility, with no provider or device authority.

Store this SQLite file at the shared fleet root. A validated stall fingerprint
belongs to a stable worker/account namespace, not a process boot. get_or_open
reuses its open incident forever (including exhausted/unresolved incidents).
Only explicit close followed by the cooldown permits a replacement incident.

scope_token is an opaque digest of the complete current RecoveryScope. Restart
or scope change requires explicit CAS rebind after scan-owner validation, keeping
the incident and action count. An unresolved action forbids rebind and close.
Neither this digest nor a successful reservation replaces fresh context/lease
checks and DeviceSupervisor arbitration immediately before dispatch.

Only reserve_action == True grants one attempt, and must precede dispatch.
Crashes after reservation conservatively consume the slot and block further
actions until authoritative outcome reconciliation; no automatic replay/refund.
Run this synchronous store off the scan thread; SQLite lock waits are bounded.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
from typing import Iterator, Literal
from uuid import uuid4


MAX_ACTIONS = 3
COOLDOWN_SECONDS = 600
ActionSource = Literal["deterministic", "model"]
ActionOutcome = Literal["confirmed", "no_effect", "invalidated", "not_dispatched", "unknown"]
EpisodeOutcome = Literal["recovered", "exhausted", "invalidated"]


@dataclass(frozen=True)
class EpisodeNamespace:
    worker: str
    account_id: str

    def __post_init__(self) -> None:
        _identifier(self.worker)
        _identifier(self.account_id)


@dataclass(frozen=True)
class RecoveryEpisode:
    incident_id: str
    namespace: EpisodeNamespace
    fingerprint: str
    scope_token: str
    revision: int
    opened_at: float
    closed_at: float | None
    cooldown_until: float | None
    outcome: EpisodeOutcome | None
    actions_used: int
    actions_remaining: int
    unresolved_action_ids: tuple[str, ...]
    status: Literal["open", "unresolved", "exhausted", "closed"]


@dataclass(frozen=True)
class EpisodeOpenResult:
    episode: RecoveryEpisode
    opened: bool
    blocker: Literal["scope_mismatch", "unresolved", "exhausted", "cooldown"] | None


class RecoveryEpisodes:
    def __init__(self, fleet_root: Path) -> None:
        self.path = Path(fleet_root) / "recovery-episodes.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._write() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS episode_clock (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                high_water REAL NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO episode_clock VALUES (1, 0)")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_episodes (
                incident_id TEXT PRIMARY KEY,
                worker TEXT NOT NULL, account_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, scope_token TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                opened_at REAL NOT NULL, closed_at REAL,
                cooldown_until REAL, outcome TEXT)""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS open_recovery_episode
                ON recovery_episodes(worker, account_id, fingerprint)
                WHERE closed_at IS NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS episode_fingerprint
                ON recovery_episodes(worker, account_id, fingerprint)""")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_actions (
                action_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL REFERENCES recovery_episodes(incident_id),
                scope_token TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source IN ('deterministic', 'model')),
                outcome TEXT CHECK(outcome IN
                    ('confirmed', 'no_effect', 'invalidated', 'not_dispatched', 'unknown'))
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS actions_by_episode "
                       "ON recovery_actions(incident_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_reconciliations (
                action_id TEXT NOT NULL REFERENCES recovery_actions(action_id),
                incident_id TEXT NOT NULL, previous TEXT, outcome TEXT NOT NULL,
                operator TEXT NOT NULL, evidence TEXT NOT NULL, at REAL NOT NULL)""")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def get(self, *, namespace: EpisodeNamespace, incident_id: str) -> RecoveryEpisode | None:
        """Read one incident and its actions from a consistent snapshot."""
        _identifier(incident_id)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return _get(db, namespace, incident_id)

    def lookup(self, *, namespace: EpisodeNamespace, fingerprint: str) -> RecoveryEpisode | None:
        """Read the latest incident, including closed or unresolved incidents."""
        _identifier(fingerprint)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            return _latest(db, namespace, fingerprint)

    def get_or_open(self, *, namespace: EpisodeNamespace, fingerprint: str,
                    scope_token: str, now: float) -> EpisodeOpenResult:
        """Atomically reuse an incident or create one after a closed cooldown.

        The caller derives fingerprint from validated semantic context. Do not
        include boot/lease IDs or volatile frame hashes to bypass episode reuse.
        This result is eligibility information, never authorization for input.
        """
        _identifier(fingerprint)
        _identifier(scope_token)
        with self._write() as db:
            effective_now = _clock(db, now)
            existing = _latest(db, namespace, fingerprint)
            if existing is not None:
                if existing.closed_at is None:
                    blocker = ("scope_mismatch" if existing.scope_token != scope_token
                               else existing.status if existing.status != "open" else None)
                    return EpisodeOpenResult(existing, False, blocker)
                assert existing.cooldown_until is not None
                if effective_now < existing.cooldown_until:
                    return EpisodeOpenResult(existing, False, "cooldown")
            incident_id = str(uuid4())
            db.execute("""INSERT INTO recovery_episodes
                (incident_id, worker, account_id, fingerprint, scope_token, opened_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (incident_id, namespace.worker, namespace.account_id,
                 fingerprint, scope_token, effective_now))
            return EpisodeOpenResult(_required(db, namespace, incident_id), True, None)

    def rebind_scope(self, *, namespace: EpisodeNamespace, incident_id: str,
                     expected_revision: int, scope_token: str) -> RecoveryEpisode:
        """Explicit scan-owner scope adoption, without resetting any allowance.

        Only call after independently proving current ownership and fresh safe
        context. This is a revision fence, not an implementation of lease checks.
        Unknown outcomes require manual/authoritative reconciliation first.
        """
        _identifier(scope_token)
        with self._write() as db:
            episode = _required(db, namespace, incident_id)
            _revision(episode, expected_revision)
            _mutable(episode)
            if episode.scope_token != scope_token:
                db.execute("UPDATE recovery_episodes SET scope_token = ?, revision = revision + 1 "
                           "WHERE incident_id = ?", (scope_token, incident_id))
            return _required(db, namespace, incident_id)

    def reserve_action(self, *, namespace: EpisodeNamespace, incident_id: str,
                       expected_revision: int, scope_token: str, action_id: str,
                       source: ActionSource) -> bool:
        """True grants exactly one bounded attempt; duplicate IDs never grant again."""
        _identifier(action_id)
        _identifier(scope_token)
        _identifier(incident_id)
        _valid_revision(expected_revision)
        if source not in ("deterministic", "model"):
            raise ValueError("unknown recovery action source")
        with self._write() as db:
            if db.execute("SELECT 1 FROM recovery_actions WHERE action_id = ?",
                          (action_id,)).fetchone() is not None:
                return False
            episode = _get(db, namespace, incident_id)
            if (episode is None or episode.status != "open"
                    or episode.revision != expected_revision or episode.scope_token != scope_token):
                return False
            db.execute("""INSERT INTO recovery_actions
                (action_id, incident_id, scope_token, source) VALUES (?, ?, ?, ?)""",
                (action_id, incident_id, scope_token, source))
            db.execute("UPDATE recovery_episodes SET revision = revision + 1 WHERE incident_id = ?",
                       (incident_id,))
            return True

    def record_outcome(self, *, namespace: EpisodeNamespace, incident_id: str,
                       action_id: str, outcome: ActionOutcome) -> RecoveryEpisode:
        """Record evidence, keeping every attempted slot; unknown stays unresolved.

        A later authoritative result may reconcile unknown. Known results are
        immutable/idempotent. No method retries or refunds a reserved action.
        """
        _identifier(action_id)
        if outcome not in ("confirmed", "no_effect", "invalidated", "not_dispatched", "unknown"):
            raise ValueError("unknown recovery action outcome")
        with self._write() as db:
            episode = _required(db, namespace, incident_id)
            action = db.execute("SELECT outcome FROM recovery_actions "
                                "WHERE incident_id = ? AND action_id = ?",
                                (incident_id, action_id)).fetchone()
            if action is None:
                raise ValueError("unknown recovery action")
            old = action["outcome"]
            if old == outcome:
                return episode
            if old not in (None, "unknown"):
                raise ValueError("conflicting recovery action outcome")
            db.execute("UPDATE recovery_actions SET outcome = ? WHERE action_id = ?",
                       (outcome, action_id))
            db.execute("UPDATE recovery_episodes SET revision = revision + 1 WHERE incident_id = ?",
                       (incident_id,))
            return _required(db, namespace, incident_id)

    def reconcile(self, *, namespace: EpisodeNamespace, incident_id: str, action_id: str,
                  outcome: ActionOutcome, operator: str, evidence: str,
                  now: float, allow_unrecorded: bool = False) -> RecoveryEpisode:
        """Operator reconciliation of an unresolved (unknown/unrecorded) action.

        Records a known outcome and an append-only audit row in one transaction.
        ``unknown`` actions are always eligible; unrecorded (NULL) ones only with
        ``allow_unrecorded`` after the operator confirmed the worker stopped.
        Known outcomes are never rewritten; this never closes the incident, so
        the scan owner still closes it with its cooldown on the next open.
        """
        _identifier(action_id)
        if outcome not in ("confirmed", "no_effect", "invalidated", "not_dispatched"):
            raise ValueError("reconciliation requires a known action outcome")
        for value, name in ((operator, "operator"), (evidence, "evidence")):
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{name} is required")
        with self._write() as db:
            effective_now = _clock(db, now)
            _required(db, namespace, incident_id)
            row = db.execute("SELECT outcome FROM recovery_actions "
                             "WHERE incident_id = ? AND action_id = ?",
                             (incident_id, action_id)).fetchone()
            if row is None:
                raise ValueError("unknown recovery action")
            if row["outcome"] not in (None, "unknown"):
                raise ValueError("recovery action outcome is already known")
            if row["outcome"] is None and not allow_unrecorded:
                # NULL may be a live worker's in-flight reservation; only an
                # operator who confirmed that worker is stopped may settle it.
                raise ValueError("unrecorded action: confirm the worker is stopped first")
            db.execute("UPDATE recovery_actions SET outcome = ? WHERE action_id = ?",
                       (outcome, action_id))
            db.execute("UPDATE recovery_episodes SET revision = revision + 1 WHERE incident_id = ?",
                       (incident_id,))
            db.execute("INSERT INTO recovery_reconciliations VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (action_id, incident_id, row["outcome"], outcome, operator.strip(),
                        evidence.strip(), effective_now))
            return _required(db, namespace, incident_id)

    def reconciliations(self, *, incident_id: str) -> tuple[tuple[str, str | None, str, str, str, float], ...]:
        _identifier(incident_id)
        with closing(self._connect()) as db:
            return tuple(tuple(row) for row in db.execute(
                "SELECT action_id, previous, outcome, operator, evidence, at "
                "FROM recovery_reconciliations WHERE incident_id = ? ORDER BY rowid",
                (incident_id,)))

    def action_states(self, *, incident_id: str) -> tuple[tuple[str, str | None], ...]:
        _identifier(incident_id)
        with closing(self._connect()) as db:
            return tuple(tuple(row) for row in db.execute(
                "SELECT action_id, outcome FROM recovery_actions WHERE incident_id = ? "
                "ORDER BY rowid", (incident_id,)))

    def unresolved(self) -> tuple[RecoveryEpisode, ...]:
        """Open incidents holding an action with no known outcome."""
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            rows = db.execute("SELECT * FROM recovery_episodes WHERE closed_at IS NULL "
                              "ORDER BY rowid").fetchall()
            return tuple(e for e in (_snapshot(db, row) for row in rows)
                         if e.status == "unresolved")

    def close(self, *, namespace: EpisodeNamespace, incident_id: str,
              expected_revision: int, outcome: EpisodeOutcome, now: float) -> RecoveryEpisode:
        """Close only with no unresolved action; start a durable ten-minute cooldown."""
        if outcome not in ("recovered", "exhausted", "invalidated"):
            raise ValueError("unknown recovery episode outcome")
        with self._write() as db:
            effective_now = _clock(db, now)
            episode = _required(db, namespace, incident_id)
            _revision(episode, expected_revision)
            if episode.closed_at is not None:
                if episode.outcome != outcome:
                    raise ValueError("conflicting recovery episode outcome")
                return episode
            _mutable(episode)
            db.execute("""UPDATE recovery_episodes SET closed_at = ?, cooldown_until = ?,
                outcome = ?, revision = revision + 1 WHERE incident_id = ?""",
                (effective_now, effective_now + COOLDOWN_SECONDS, outcome, incident_id))
            return _required(db, namespace, incident_id)


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("recovery identifier must be a nonempty bounded string")


def _valid_revision(value: int) -> None:
    if type(value) is not int or not 1 <= value < 2**63 - 1:
        raise ValueError("expected revision must be a positive integer")


def _revision(episode: RecoveryEpisode, expected: int) -> None:
    _valid_revision(expected)
    if episode.revision != expected:
        raise ValueError("recovery episode revision changed")


def _mutable(episode: RecoveryEpisode) -> None:
    if episode.status == "unresolved":
        raise ValueError("unresolved recovery action requires authoritative reconciliation")
    if episode.closed_at is not None:
        raise ValueError("recovery episode is closed")


def _clock(db: sqlite3.Connection, now: float) -> float:
    if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now < 2**52:
        raise ValueError("now must be a finite nonnegative UTC timestamp")
    previous = db.execute("SELECT high_water FROM episode_clock WHERE singleton = 1").fetchone()[0]
    effective = max(previous, now)
    db.execute("UPDATE episode_clock SET high_water = ? WHERE singleton = 1", (effective,))
    return effective


def _latest(db: sqlite3.Connection, namespace: EpisodeNamespace,
            fingerprint: str) -> RecoveryEpisode | None:
    row = db.execute("""SELECT * FROM recovery_episodes
        WHERE worker = ? AND account_id = ? AND fingerprint = ? ORDER BY rowid DESC LIMIT 1""",
        (namespace.worker, namespace.account_id, fingerprint)).fetchone()
    return None if row is None else _snapshot(db, row)


def _get(db: sqlite3.Connection, namespace: EpisodeNamespace,
         incident_id: str) -> RecoveryEpisode | None:
    _identifier(incident_id)
    row = db.execute("""SELECT * FROM recovery_episodes
        WHERE worker = ? AND account_id = ? AND incident_id = ?""",
        (namespace.worker, namespace.account_id, incident_id)).fetchone()
    return None if row is None else _snapshot(db, row)


def _required(db: sqlite3.Connection, namespace: EpisodeNamespace,
              incident_id: str) -> RecoveryEpisode:
    episode = _get(db, namespace, incident_id)
    if episode is None:
        raise ValueError("unknown recovery incident in this namespace")
    return episode


def _snapshot(db: sqlite3.Connection, row: sqlite3.Row) -> RecoveryEpisode:
    actions = db.execute("SELECT action_id, outcome FROM recovery_actions "
                         "WHERE incident_id = ? ORDER BY rowid", (row["incident_id"],)).fetchall()
    unresolved = tuple(action["action_id"] for action in actions
                       if action["outcome"] in (None, "unknown"))
    status = ("closed" if row["closed_at"] is not None else "unresolved" if unresolved
              else "exhausted" if len(actions) >= MAX_ACTIONS else "open")
    return RecoveryEpisode(
        incident_id=row["incident_id"], namespace=EpisodeNamespace(row["worker"], row["account_id"]),
        fingerprint=row["fingerprint"], scope_token=row["scope_token"], revision=row["revision"],
        opened_at=row["opened_at"], closed_at=row["closed_at"], cooldown_until=row["cooldown_until"],
        outcome=row["outcome"], actions_used=len(actions),
        actions_remaining=max(0, MAX_ACTIONS - len(actions)),
        unresolved_action_ids=unresolved, status=status,
    )
