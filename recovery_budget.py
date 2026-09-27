"""Fleet-wide durable ceiling for paid recovery calls.

The caller stores this database at the shared fleet root, not a worker root.
Reserve a request-bound, verified price ceiling before any network send. Every
method opens its own connection so independent workers share SQLite's write lock.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sqlite3
from typing import Iterator

from recovery_status import bump_config_epoch


HARD_INCIDENT_LIMIT_MICROUSD = 50_000
HARD_DAILY_LIMIT_MICROUSD = 1_000_000
HARD_MAX_CALLS = 2


class BudgetOverchargeError(RuntimeError):
    """A reported charge exceeded its preflight reservation; calls are disabled."""


@dataclass(frozen=True)
class BudgetLimits:
    incident_limit_microusd: int
    daily_limit_microusd: int
    max_calls: int


@dataclass(frozen=True)
class BudgetPolicy(BudgetLimits):
    revision: int
    disabled_request_id: str | None


@dataclass(frozen=True)
class BudgetPolicyStatus:
    active: BudgetPolicy
    requested: BudgetLimits
    requested_policy_conflict: bool


class BudgetPolicyConflictError(RuntimeError):
    """A stale policy editor must reload and explicitly reconsider its change."""

    def __init__(self, active: BudgetPolicy) -> None:
        super().__init__("recovery budget policy revision changed")
        self.active = active


class BudgetSettingsConflictError(RuntimeError):
    """Settings or monetary policy changed since the operator reviewed them."""

    def __init__(self, settings_revision: int, active: BudgetPolicy) -> None:
        super().__init__("recovery settings or budget policy revision changed")
        self.settings_revision = settings_revision
        self.active = active


@dataclass(frozen=True)
class BudgetSummary:
    day: str
    reserved_microusd: int
    settled_microusd: int
    total_microusd: int
    remaining_microusd: int
    disabled: bool
    budget_incident_request_id: str | None


class RecoveryBudget:
    """SQLite accounting for one shared fleet root.

    A duplicate request ID always returns False from ``reserve``: an
    idempotent retry must never become authorization for a second POST.
    ``mark_sent`` must precede handing a request to the transport. A timed-out
    or cancelled request remains charged at its reserved ceiling until actual
    usage is reconciled. Only ``release_not_sent`` before ``mark_sent`` can
    remove a ceiling.
    """

    def __init__(self, fleet_root: Path, *,
                 incident_limit_microusd: int = HARD_INCIDENT_LIMIT_MICROUSD,
                 daily_limit_microusd: int = HARD_DAILY_LIMIT_MICROUSD,
                 max_calls: int = HARD_MAX_CALLS) -> None:
        self.requested_limits = _limits(incident_limit_microusd,
                                        daily_limit_microusd, max_calls)
        self.path = Path(fleet_root) / "recovery-budget.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._write() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_policy (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                incident_limit_microusd INTEGER NOT NULL,
                daily_limit_microusd INTEGER NOT NULL,
                max_calls INTEGER NOT NULL,
                disabled_request_id TEXT
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_reservations (
                request_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL,
                day TEXT NOT NULL,
                maximum_microusd INTEGER NOT NULL CHECK(maximum_microusd >= 0),
                actual_microusd INTEGER CHECK(actual_microusd >= 0),
                state TEXT NOT NULL CHECK(state IN
                    ('reserved', 'sent', 'uncertain', 'settled', 'released'))
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS recovery_by_incident "
                       "ON recovery_reservations(incident_id)")
            db.execute("CREATE INDEX IF NOT EXISTS recovery_by_day "
                       "ON recovery_reservations(day)")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(recovery_policy)")}
            if "revision" not in columns:
                db.execute("ALTER TABLE recovery_policy ADD COLUMN revision "
                           "INTEGER NOT NULL DEFAULT 1")
            if "latest_day" not in columns:
                db.execute("ALTER TABLE recovery_policy ADD COLUMN latest_day TEXT")
                db.execute("UPDATE recovery_policy SET latest_day = "
                           "(SELECT MAX(day) FROM recovery_reservations)")
            db.execute("""CREATE TABLE IF NOT EXISTS recovery_settings (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                revision INTEGER NOT NULL,
                settings_json TEXT
            )""")
            db.execute("INSERT OR IGNORE INTO recovery_settings "
                       "(singleton, revision, settings_json) VALUES (1, 1, NULL)")
            policy = db.execute("SELECT 1 FROM recovery_policy WHERE singleton = 1").fetchone()
            if policy is None:
                db.execute("""INSERT INTO recovery_policy
                    (singleton, incident_limit_microusd, daily_limit_microusd, max_calls)
                    VALUES (1, ?, ?, ?)""",
                    (incident_limit_microusd, daily_limit_microusd, max_calls))

    def policy(self) -> BudgetPolicyStatus:
        """Read authoritative limits; construction never edits an existing policy."""
        with closing(self._connect()) as db:
            active = _policy(db)
        limits = BudgetLimits(active.incident_limit_microusd,
                              active.daily_limit_microusd, active.max_calls)
        return BudgetPolicyStatus(active, self.requested_limits,
                                  limits != self.requested_limits)

    def update_policy(self, *, expected_revision: int, incident_limit_microusd: int,
                      daily_limit_microusd: int, max_calls: int) -> BudgetPolicy:
        """Explicit fleet policy CAS, preserving every charge and the disabled flag.

        A cap decrease never cancels an already granted send. Existing commitments
        may exceed new caps; further reservations fail until affordable. Only the
        settings owner calls this after reading a revision; workers must not auto
        retry a conflict with their old settings or call it during startup.
        """
        _limits(incident_limit_microusd, daily_limit_microusd, max_calls)
        if type(expected_revision) is not int or not 1 <= expected_revision < 2**63 - 1:
            raise ValueError("expected revision must be a positive integer")
        with self._write() as db:
            active = _policy(db)
            if active.revision != expected_revision:
                raise BudgetPolicyConflictError(active)
            db.execute("""UPDATE recovery_policy SET incident_limit_microusd = ?,
                daily_limit_microusd = ?, max_calls = ?, revision = revision + 1
                WHERE singleton = 1""",
                (incident_limit_microusd, daily_limit_microusd, max_calls))
            result = _policy(db)
        bump_config_epoch(self.path.parent)  # After commit; see recovery_status.
        return result

    def settings_snapshot(self) -> tuple[int, str | None, BudgetPolicy]:
        """Read both authorities from one SQLite snapshot."""
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            row = db.execute("SELECT revision, settings_json FROM recovery_settings "
                             "WHERE singleton = 1").fetchone()
            assert row is not None
            return row["revision"], row["settings_json"], _policy(db)

    def update_settings(self, *, expected_settings_revision: int,
                        expected_policy_revision: int, settings_json: str,
                        incident_limit_microusd: int, daily_limit_microusd: int,
                        max_calls: int) -> tuple[int, BudgetPolicy]:
        """Commit validated nonsecret settings and budget caps atomically.

        Only a coordinated settings owner calls this. A transaction failure
        rolls back both revisions and caps; reservations and disabled state are
        never reset. Workers only read the result.
        """
        _limits(incident_limit_microusd, daily_limit_microusd, max_calls)
        if (type(expected_settings_revision) is not int or expected_settings_revision < 1
                or type(expected_policy_revision) is not int or expected_policy_revision < 1):
            raise ValueError("expected revisions must be positive integers")
        with self._write() as db:
            row = db.execute("SELECT revision FROM recovery_settings WHERE singleton = 1").fetchone()
            assert row is not None
            active = _policy(db)
            if (row["revision"] != expected_settings_revision
                    or active.revision != expected_policy_revision):
                raise BudgetSettingsConflictError(row["revision"], active)
            if (active.incident_limit_microusd != incident_limit_microusd
                    or active.daily_limit_microusd != daily_limit_microusd
                    or active.max_calls != max_calls):
                db.execute("""UPDATE recovery_policy SET incident_limit_microusd = ?,
                    daily_limit_microusd = ?, max_calls = ?, revision = revision + 1
                    WHERE singleton = 1""", (incident_limit_microusd, daily_limit_microusd,
                                              max_calls))
            db.execute("UPDATE recovery_settings SET revision = revision + 1, "
                       "settings_json = ? WHERE singleton = 1", (settings_json,))
            result = row["revision"] + 1, _policy(db)
        bump_config_epoch(self.path.parent)  # After commit; see recovery_status.
        return result

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
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

    def reserve(self, *, request_id: str, incident_id: str, day: str,
                maximum_microusd: int) -> bool:
        """Atomically reserve an O2 verified upper bound and one incident call slot."""
        _identifier(request_id, "request ID")
        _identifier(incident_id, "incident ID")
        _day(day)
        _amount(maximum_microusd, "maximum charge")
        with self._write() as db:
            if db.execute("SELECT 1 FROM recovery_reservations WHERE request_id = ?",
                          (request_id,)).fetchone() is not None:
                return False
            policy = db.execute("SELECT * FROM recovery_policy WHERE singleton = 1").fetchone()
            assert policy is not None
            if policy["disabled_request_id"] is not None:
                return False
            if policy["latest_day"] is not None and day < policy["latest_day"]:
                return False
            db.execute("UPDATE recovery_policy SET latest_day = ? WHERE singleton = 1", (day,))
            incident = db.execute("""SELECT COUNT(*) AS calls,
                COALESCE(SUM(COALESCE(actual_microusd, maximum_microusd)), 0) AS cost
                FROM recovery_reservations
                WHERE incident_id = ? AND state != 'released'""", (incident_id,)).fetchone()
            daily = db.execute("""SELECT
                COALESCE(SUM(COALESCE(actual_microusd, maximum_microusd)), 0) AS cost
                FROM recovery_reservations
                WHERE day = ? AND state != 'released'""", (day,)).fetchone()
            assert incident is not None and daily is not None
            if (incident["calls"] >= policy["max_calls"]
                    or incident["cost"] + maximum_microusd
                    > policy["incident_limit_microusd"]
                    or daily["cost"] + maximum_microusd
                    > policy["daily_limit_microusd"]):
                return False
            db.execute("""INSERT INTO recovery_reservations
                (request_id, incident_id, day, maximum_microusd, state)
                VALUES (?, ?, ?, ?, 'reserved')""",
                (request_id, incident_id, day, maximum_microusd))
            return True

    def mark_sent(self, request_id: str) -> None:
        """Conservatively record that the transport may have sent the request."""
        _identifier(request_id, "request ID")
        with self._write() as db:
            row = _request(db, request_id)
            if row["state"] == "reserved":
                db.execute("UPDATE recovery_reservations SET state = 'sent' "
                           "WHERE request_id = ?", (request_id,))
            elif row["state"] == "released":
                raise ValueError("a released request cannot be sent")

    def release_not_sent(self, request_id: str) -> None:
        """Refund only a request whose send marker was never written."""
        _identifier(request_id, "request ID")
        with self._write() as db:
            row = _request(db, request_id)
            if row["state"] == "released":
                return
            if row["state"] != "reserved":
                raise ValueError("request may have been sent; reservation retained")
            db.execute("UPDATE recovery_reservations SET state = 'released' "
                       "WHERE request_id = ?", (request_id,))

    def settle(self, request_id: str, *, actual_microusd: int | None) -> None:
        """Settle once; unknown usage retains its full reservation."""
        _identifier(request_id, "request ID")
        if actual_microusd is not None:
            _amount(actual_microusd, "actual charge")
        overcharged = False
        with self._write() as db:
            row = _request(db, request_id)
            if row["state"] == "released":
                raise ValueError("released request cannot be settled")
            old_actual = row["actual_microusd"]
            if old_actual is not None:
                if actual_microusd is not None and old_actual != actual_microusd:
                    raise ValueError("conflicting reported charge for request")
                if old_actual > row["maximum_microusd"]:
                    raise BudgetOverchargeError("reported charge exceeded reservation")
                return
            if actual_microusd is None:
                db.execute("UPDATE recovery_reservations SET state = 'uncertain' "
                           "WHERE request_id = ?", (request_id,))
                return
            db.execute("""UPDATE recovery_reservations
                SET actual_microusd = ?, state = 'settled' WHERE request_id = ?""",
                (actual_microusd, request_id))
            if actual_microusd > row["maximum_microusd"]:
                db.execute("""UPDATE recovery_policy SET disabled_request_id = ?
                    WHERE singleton = 1 AND disabled_request_id IS NULL""", (request_id,))
                overcharged = True
        if overcharged:
            raise BudgetOverchargeError("reported charge exceeded reservation")

    def calls_remaining(self, *, incident_id: str) -> int:
        """Authoritative durable per-incident allowance; read off the scan thread."""
        _identifier(incident_id, "incident_id")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            limit = _policy(db).max_calls
            used = db.execute("SELECT COUNT(*) FROM recovery_reservations WHERE incident_id=? AND state != 'released'", (incident_id,)).fetchone()[0]
            return max(0, limit - used)

    def summary(self, *, day: str) -> BudgetSummary:
        """Read one UTC day with outstanding and committed charge separated."""
        _day(day)
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            policy = db.execute("SELECT * FROM recovery_policy WHERE singleton = 1").fetchone()
            assert policy is not None
            reserved = 0
            settled = 0
            for row in db.execute("""SELECT maximum_microusd, actual_microusd
                    FROM recovery_reservations
                    WHERE day = ? AND state != 'released'""", (day,)):
                if row["actual_microusd"] is None:
                    reserved += row["maximum_microusd"]
                else:
                    settled += row["actual_microusd"]
            total = reserved + settled
            return BudgetSummary(
                day=day, reserved_microusd=reserved,
                settled_microusd=settled, total_microusd=total,
                remaining_microusd=max(0, policy["daily_limit_microusd"] - total),
                disabled=policy["disabled_request_id"] is not None,
                budget_incident_request_id=policy["disabled_request_id"],
            )


def _request(db: sqlite3.Connection, request_id: str) -> sqlite3.Row:
    row = db.execute("SELECT * FROM recovery_reservations WHERE request_id = ?",
                     (request_id,)).fetchone()
    if row is None:
        raise ValueError("unknown recovery request")
    return row


def _policy(db: sqlite3.Connection) -> BudgetPolicy:
    row = db.execute("SELECT * FROM recovery_policy WHERE singleton = 1").fetchone()
    assert row is not None
    return BudgetPolicy(row["incident_limit_microusd"], row["daily_limit_microusd"],
                        row["max_calls"], row["revision"], row["disabled_request_id"])


def _limits(incident: int, daily: int, calls: int) -> BudgetLimits:
    return BudgetLimits(_limit(incident, HARD_INCIDENT_LIMIT_MICROUSD, "incident limit"),
                        _limit(daily, HARD_DAILY_LIMIT_MICROUSD, "daily limit"),
                        _limit(calls, HARD_MAX_CALLS, "call limit"))


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError(f"{label} must be a nonempty bounded string")


def _day(value: str) -> None:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("day must be a UTC ISO date")
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError("day must be a UTC ISO date") from None


def _amount(value: int, label: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise ValueError(f"{label} must be nonnegative integer microdollars")


def _limit(value: int, maximum: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} exceeds hard safety limit")
    return value
