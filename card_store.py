"""Account-local Cards persistence; the transaction journal owns all spending."""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
import hashlib
import json
import math
import logging
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

import db
from card_models import CardBudget, CardCommand, CardOperation, CardSnapshot, RewardItem
from card_screen import merge_snapshots
from evidence_scope import FactScope

logger = logging.getLogger(__name__)

TERMINAL = frozenset({'confirmed', 'not_applied', 'canceled', 'blocked'})
PAID = frozenset({'buy', 'slot'})
_TRANSITIONS = {
    'queued': {'preflight', 'canceled', 'blocked'},
    'preflight': {'dispatched', 'confirmed', 'not_applied', 'canceled', 'blocked', 'reconciliation_required'},
    'dispatched': {'verifying', 'confirmed', 'not_applied', 'reconciliation_required'},
    'verifying': {'confirmed', 'not_applied', 'reconciliation_required'},
    'reconciliation_required': {'verifying', 'confirmed', 'not_applied'},
}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _time(now: float) -> None:
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise ValueError('invalid timestamp')


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class CardStore:
    """Construction/reads never migrate. Mutations require an explicitly bound account.

    Calls are synchronous SQLite adapters; async API callers must offload them.
    All connection-aware hooks participate in the caller's existing transaction.
    """
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with closing(db.connect(self.path)) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            yield conn

    @staticmethod
    def _account(conn: sqlite3.Connection, account_id: str) -> None:
        if db.connection_account(conn) != account_id:
            raise ValueError('card database account binding mismatch')

    @classmethod
    def _current(cls, conn: sqlite3.Connection, scope: FactScope) -> None:
        cls._account(conn, scope.account_id)
        row = conn.execute('SELECT detail FROM fact_scope WHERE id=1').fetchone() if _table(conn, 'fact_scope') else None
        if row is None or FactScope(**json.loads(row[0])) != scope:
            raise ValueError('card scope is not current')

    @staticmethod
    def _snapshot(conn: sqlite3.Connection) -> CardSnapshot | None:
        if not _table(conn, 'card_observations'):
            return None
        row = conn.execute('SELECT detail FROM card_observations WHERE account_id=? ORDER BY id DESC LIMIT 1',
                           (db.connection_account(conn),)).fetchone()
        return CardSnapshot.model_validate_json(row[0]) if row else None

    def snapshot(self) -> CardSnapshot | None:
        if not self.path.exists():
            return None
        with db.reader(self.path) as conn:
            return self._snapshot(conn)

    def observe(self, snapshot: CardSnapshot) -> bool:
        with self._write() as conn:
            self._account(conn, snapshot.scope.account_id)
            try:
                self._current(conn, snapshot.scope)
            except ValueError:
                return False
            previous = self._snapshot(conn)
            if previous is not None and snapshot.observed_at < previous.observed_at:
                return False
            merged = merge_snapshots(previous, snapshot)
            if merged == previous:
                return False
            conn.execute('INSERT INTO card_observations(account_id,observed_at,detail) VALUES (?,?,?)',
                         (merged.scope.account_id, merged.observed_at, merged.model_dump_json()))
            from account_state import project_card_snapshot
            project_card_snapshot(conn, merged)
            return True

    @staticmethod
    def _operation(conn: sqlite3.Connection, operation_id: str) -> CardOperation | None:
        if not _table(conn, 'card_operations'):
            return None
        row = conn.execute('SELECT detail,transaction_key,status,updated_at FROM card_operations WHERE operation_id=? AND account_id=?',
                           (operation_id, db.connection_account(conn))).fetchone()
        if row is None:
            return None
        operation = CardOperation.model_validate_json(row['detail'])
        spent = None
        if row['transaction_key'] is not None:
            txn = conn.execute('SELECT stage,outcome,spent FROM transactions WHERE key=?', (row['transaction_key'],)).fetchone()
            if txn and txn['stage'] == 'resolved' and txn['outcome'] in ('bought', 'free', 'refuted'):
                spent = txn['spent']
        return operation.model_copy(update={'transaction_key': row['transaction_key'], 'spent_gems': spent,
                                             'status': row['status'], 'updated_at': row['updated_at']})

    def operation(self, operation_id: str) -> CardOperation | None:
        if not self.path.exists():
            return None
        with db.reader(self.path) as conn:
            return self._operation(conn, operation_id)

    def submit(self, command: CardCommand, *, now: float) -> CardOperation:
        _time(now)
        payload = _json(command.model_dump(mode='json'))
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self._write() as conn:
            self._account(conn, command.scope.account_id)
            row = conn.execute('SELECT operation_id,payload_hash FROM card_operations WHERE account_id=? AND idempotency_key=?',
                               (command.scope.account_id, command.idempotency_key)).fetchone()
            if row:
                if row['payload_hash'] != digest:
                    raise ValueError('card idempotency key reused with conflicting payload')
                return self._operation(conn, row['operation_id'])
            self._current(conn, command.scope)
            if command.kind in PAID:
                cycle = conn.execute('SELECT * FROM card_budget_cycles WHERE cycle_id=?', (command.budget_cycle_id,)).fetchone()
                if cycle is None or cycle['account_id'] != command.scope.account_id or cycle['closed_at'] is not None:
                    raise ValueError('paid command requires an active account budget cycle')
            result = CardOperation(operation_id=uuid4().hex, command=command, status='queued', created_at=now, updated_at=now)
            conn.execute('INSERT INTO card_operations(operation_id,account_id,idempotency_key,payload_hash,scope,program_revision,kind,budget_cycle_id,status,created_at,updated_at,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (result.operation_id, command.scope.account_id, command.idempotency_key, digest,
                          _json(asdict(command.scope)), command.program_revision, command.kind,
                          command.budget_cycle_id, result.status, now, now, result.model_dump_json()))
            if command.kind == 'cancel':
                self._request_cancel(conn, command.target_operation_id, now=now)
            logger.info('Card command queued', extra={'account_id': command.scope.account_id,
                        'operation_id': result.operation_id, 'card_kind': command.kind})
            return result

    def _request_cancel(self, conn: sqlite3.Connection, operation_id: str, *, now: float) -> CardOperation:
        operation = self._operation(conn, operation_id)
        if operation is None:
            raise KeyError(operation_id)
        if operation.status in TERMINAL or operation.cancel_requested:
            return operation
        # Independent of visit stop/recovery markers and status reason changes.
        result = operation.model_copy(update={'cancel_requested': True,
            'updated_at': max(now, operation.updated_at), 'spent_gems': None})
        conn.execute('UPDATE card_operations SET updated_at=?,detail=? WHERE operation_id=?',
                     (result.updated_at, result.model_dump_json(), operation_id))
        return self._operation(conn, operation_id)

    def request_cancel(self, operation_id: str, *, now: float) -> CardOperation:
        _time(now)
        with self._write() as conn:
            return self._request_cancel(conn, operation_id, now=now)

    def pending(self, scope: FactScope) -> tuple[CardOperation, ...]:
        """Only operations pinned to this scope; use unresolved() for recovery."""
        return tuple(op for op in self.unresolved() if op.command.scope == scope)

    def unresolved(self) -> tuple[CardOperation, ...]:
        """Account-wide work, including historical scope fences; read-only."""
        if not self.path.exists():
            return ()
        with db.reader(self.path) as conn:
            if not _table(conn, 'card_operations'):
                return ()
            rows = conn.execute("SELECT operation_id FROM card_operations WHERE account_id=? AND status NOT IN ('confirmed','not_applied','canceled','blocked') ORDER BY created_at,operation_id",
                                (db.connection_account(conn),)).fetchall()
            return tuple(self._operation(conn, row[0]) for row in rows)

    def current_visit(self) -> str | None:
        """Restore the current opportunity even when every operation has returned home."""
        if not self.path.exists():
            return None
        with db.reader(self.path) as conn:
            account_id = db.connection_account(conn)
            if _table(conn, 'card_runtime_visit'):
                row = conn.execute('SELECT visit_id FROM card_runtime_visit WHERE account_id=?',
                                   (account_id,)).fetchone()
                if row:
                    return row[0]
            # Conservative upgrade from runtimes that persisted only operation flows.
            if not _table(conn, 'card_visit_flows'):
                return None
            row = conn.execute('SELECT f.visit_id FROM card_visit_flows f JOIN card_operations o '
                'USING(operation_id) WHERE o.account_id=? ORDER BY f.started_at DESC LIMIT 1',
                (account_id,)).fetchone()
            return row[0] if row else None

    def observe_visit(self, scope: FactScope, visit_id: str, *,
                      in_run: bool | None = None, captured_at: float | None = None) -> str:
        """Persist one opportunity; only a newer verified new-run observation rotates it."""
        if (in_run is None) != (captured_at is None) or in_run is not None and type(in_run) is not bool:
            raise ValueError('run boundary requires capture evidence')
        if captured_at is not None:
            _time(captured_at)
        with self._write() as conn:
            self._current(conn, scope)
            conn.execute('INSERT OR IGNORE INTO card_runtime_visit(account_id,visit_id) VALUES (?,?)',
                         (scope.account_id, visit_id))
            row = conn.execute('SELECT * FROM card_runtime_visit WHERE account_id=?', (scope.account_id,)).fetchone()
            current = row['visit_id']
            if captured_at is not None and (row['captured_at'] is None or captured_at > row['captured_at']):
                if in_run and not row['in_run']:
                    current = 'cards:' + uuid4().hex
                conn.execute('UPDATE card_runtime_visit SET visit_id=?,in_run=?,captured_at=? WHERE account_id=?',
                             (current, int(in_run), captured_at, scope.account_id))
            return current

    def visit_progress(self, visit_id: str) -> int:
        """Stable successor identity for confirmed buys, slots and equipment phases."""
        if not self.path.exists():
            return 0
        with db.reader(self.path) as conn:
            if not _table(conn, 'card_visit_flows'):
                return 0
            return conn.execute("SELECT COUNT(*) FROM card_operations o JOIN card_visit_flows f "
                "USING(operation_id) WHERE o.account_id=? AND f.visit_id=? AND o.status='confirmed' "
                "AND o.kind IN ('buy','slot','apply','clear')",
                (db.connection_account(conn), visit_id)).fetchone()[0]

    def record_queue_policy(self, command: CardCommand, policy: Any) -> None:
        """Persist a legacy handoff's stricter gates before its command can exist."""
        from strategy import CardPolicy
        if not isinstance(policy, CardPolicy) or command.source != 'automatic':
            raise ValueError('queue policy requires an automatic Cards command')
        payload = _json(policy.to_dict())
        with self._write() as conn:
            self._current(conn, command.scope)
            row = conn.execute('SELECT policy FROM card_queue_policies WHERE account_id=? AND idempotency_key=?',
                               (command.scope.account_id, command.idempotency_key)).fetchone()
            if row and row[0] != payload:
                raise ValueError('Cards queue policy idempotency conflict')
            conn.execute('INSERT OR IGNORE INTO card_queue_policies VALUES (?,?,?)',
                         (command.scope.account_id, command.idempotency_key, payload))

    def queue_policies(self, scope: FactScope, visit_id: str) -> tuple[Any, ...]:
        """Original handoff gates survive queued work and all operations in its visit."""
        from strategy import CardPolicy
        if not self.path.exists():
            return ()
        with db.reader(self.path) as conn:
            if not _table(conn, 'card_queue_policies'):
                return ()
            rows = conn.execute("SELECT p.policy,o.scope,f.visit_id FROM card_queue_policies p "
                "JOIN card_operations o USING(account_id,idempotency_key) "
                "LEFT JOIN card_visit_flows f USING(operation_id) WHERE p.account_id=? "
                "AND (o.status NOT IN ('confirmed','not_applied','canceled','blocked') OR f.visit_id=?)",
                (scope.account_id, visit_id)).fetchall()
            return tuple(CardPolicy(**json.loads(row[0])) for row in rows
                         if FactScope(**json.loads(row[1])) == scope or row[2] == visit_id)

    def active_budget(self) -> CardBudget | None:
        """The account's open cycle; never migrate an absent or archived database."""
        if not self.path.exists():
            return None
        with db.reader(self.path) as conn:
            if not _table(conn, 'card_budget_cycles'):
                return None
            row = conn.execute('SELECT cycle_id FROM card_budget_cycles WHERE account_id=? AND closed_at IS NULL ORDER BY created_at DESC LIMIT 1',
                               (db.connection_account(conn),)).fetchone()
            return self.budget(row[0], conn=conn) if row else None

    def recent_operations(self, *, limit: int = 50) -> tuple[CardOperation, ...]:
        """Bounded durable history independent of runtime capabilities."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('operation limit must be between 1 and 200')
        if not self.path.exists():
            return ()
        with db.reader(self.path) as conn:
            if not _table(conn, 'card_operations'):
                return ()
            rows = conn.execute('SELECT operation_id FROM card_operations WHERE account_id=? ORDER BY created_at DESC,operation_id DESC LIMIT ?',
                                (db.connection_account(conn), limit)).fetchall()
            return tuple(self._operation(conn, row[0]) for row in rows)

    def transition(self, operation_id: str, *, expected_status: str, status: str,
                   reason: str | None, now: float) -> CardOperation:
        _time(now)
        with self._write() as conn:
            operation = self._operation(conn, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            if operation.status != expected_status:
                raise ValueError('card operation compare-and-set conflict')
            canceled_assignment = (status == 'canceled'
                and expected_status in ('dispatched', 'verifying', 'reconciliation_required')
                and operation.command.kind in ('apply', 'clear') and operation.cancel_requested
                and operation.snapshot_after is not None
                and not conn.execute('SELECT 1 FROM card_assignment_steps WHERE operation_id=? AND verified_at IS NULL',
                                     (operation_id,)).fetchone())
            if (status not in _TRANSITIONS.get(expected_status, ()) and not canceled_assignment
                    or now < operation.updated_at):
                raise ValueError('invalid card operation transition')
            if status in ('preflight', 'dispatched'):
                self._current(conn, operation.command.scope)
            if operation.command.kind in PAID:
                if status == 'dispatched' and operation.transaction_key is None:
                    raise ValueError('paid dispatch requires linked transaction')
                if status in TERMINAL and operation.transaction_key is not None:
                    if operation.spent_gems is None:
                        raise ValueError('unresolved paid transaction cannot be terminal')
                    if status in ('blocked', 'canceled', 'not_applied') and operation.spent_gems != 0:
                        raise ValueError('paid operation cannot discard confirmed spend')
                if status == 'confirmed' and operation.transaction_key is None:
                    raise ValueError('paid confirmation requires journal proof')
            if status == 'dispatched' and operation.command.kind in ('apply', 'clear'):
                row = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?',
                                   (operation_id,)).fetchone()
                if row is None:
                    raise ValueError('assignment dispatch requires a pinned target')
                assignment = json.loads(row[0])
                if now < assignment['pinned_at']:
                    raise ValueError('assignment dispatch predates pinned target')
                assignment.setdefault('dispatched_at', now)
                conn.execute('UPDATE card_assignment_intents SET detail=? WHERE operation_id=?',
                             (_json(assignment), operation_id))
            result = operation.model_copy(update={'status': status, 'reason': reason, 'updated_at': now, 'spent_gems': None})
            conn.execute('UPDATE card_operations SET status=?,updated_at=?,detail=? WHERE operation_id=? AND status=?',
                         (status, now, result.model_dump_json(), operation_id, expected_status))
            return self._operation(conn, operation_id)

    def start_budget_cycle(self, *, scope: FactScope, cycle_id: str, cap: int, now: float) -> CardBudget:
        _time(now)
        CardBudget(cycle_id=cycle_id, cap=cap, spent=0, pending=0)
        with self._write() as conn:
            self._current(conn, scope)
            previous = conn.execute('SELECT * FROM card_budget_cycles WHERE cycle_id=?', (cycle_id,)).fetchone()
            if previous:
                if previous['account_id'] != scope.account_id or previous['cap'] != cap:
                    raise ValueError('budget cycle identity and cap are immutable')
                return self.budget(cycle_id, conn=conn)
            unresolved = conn.execute("SELECT 1 FROM card_operations WHERE account_id=? AND kind IN ('buy','slot') AND status NOT IN ('confirmed','not_applied','canceled','blocked') LIMIT 1", (scope.account_id,)).fetchone()
            journal_pending = conn.execute("SELECT 1 FROM transactions WHERE category='CARDS' AND (stage!='resolved' OR outcome='unproven' OR spent IS NULL) LIMIT 1").fetchone()
            if unresolved or journal_pending:
                raise ValueError('unresolved paid action prevents a new budget cycle')
            conn.execute('UPDATE card_budget_cycles SET closed_at=? WHERE account_id=? AND closed_at IS NULL', (now, scope.account_id))
            conn.execute('INSERT INTO card_budget_cycles(cycle_id,account_id,scope,cap,created_at) VALUES (?,?,?,?,?)',
                         (cycle_id, scope.account_id, _json(asdict(scope)), cap, now))
            return self.budget(cycle_id, conn=conn)

    def budget(self, cycle_id: str, *, conn: sqlite3.Connection | None = None) -> CardBudget:
        """Journal is sole spend truth. Unknown outcomes retain their reserved quote."""
        if conn is None:
            if not self.path.exists():
                raise KeyError(cycle_id)
            with db.reader(self.path) as reader:
                return self.budget(cycle_id, conn=reader)
        if not _table(conn, 'card_budget_cycles'):
            raise KeyError(cycle_id)
        cycle = conn.execute('SELECT * FROM card_budget_cycles WHERE cycle_id=? AND account_id=?',
                             (cycle_id, db.connection_account(conn))).fetchone()
        if cycle is None:
            raise KeyError(cycle_id)
        rows = conn.execute('SELECT t.*, o.transaction_key AS linked_key FROM card_operations o LEFT JOIN transactions t ON t.key=o.transaction_key WHERE o.budget_cycle_id=? AND o.transaction_key IS NOT NULL', (cycle_id,)).fetchall()
        spent, pending = 0, 0
        for row in rows:
            if row['key'] is None:
                raise ValueError('linked card journal transaction missing')
            if row['stage'] == 'resolved' and row['outcome'] in ('bought', 'free', 'refuted') and row['spent'] is not None:
                spent += row['spent']
            else:
                reserved = conn.execute("SELECT amount FROM currency_commitments WHERE owner=? AND currency='gems'", (f"purchase:{row['key']}",)).fetchone() if _table(conn, 'currency_commitments') else None
                if row['price'] is None and reserved is None:
                    raise ValueError('unresolved card transaction has no conservative amount')
                pending += max(row['price'] or 0, reserved[0] if reserved else 0)
        return CardBudget(cycle_id=cycle_id, cap=cycle['cap'], spent=spent, pending=pending)

    def link_transaction(self, operation_id: str, *, transaction_key: str, action_sequence: int,
                         now: float, conn: sqlite3.Connection) -> CardOperation:
        """Task5 hook: call after journal INSERT under its SAME SQLite write lock.

        Caller must roll back on failure. Does not prepare/dispatch/settle a purchase.
        One operation links one immutable journal identity; no writable spend total.
        """
        _time(now)
        if not conn.in_transaction:
            raise ValueError('journal linking requires an active write transaction')
        database_path = conn.execute('PRAGMA database_list').fetchone()[2]
        if Path(database_path).resolve() != self.path.resolve():
            raise ValueError('journal and cards must share one database')
        operation = self._operation(conn, operation_id)
        if operation is None:
            raise KeyError(operation_id)
        self._current(conn, operation.command.scope)
        if operation.command.kind not in PAID or operation.status != 'preflight' or now < operation.updated_at:
            raise ValueError('only a paid preflight may link a journal transaction')
        if type(action_sequence) is not int or action_sequence < 1:
            raise ValueError('invalid card action sequence')
        prior = conn.execute('SELECT action_sequence FROM card_operations WHERE operation_id=?', (operation_id,)).fetchone()[0]
        if operation.transaction_key is not None and (operation.transaction_key != transaction_key or prior != action_sequence):
            raise ValueError('card journal identity is immutable')
        txn = conn.execute('SELECT * FROM transactions WHERE key=?', (transaction_key,)).fetchone()
        if (txn is None or txn['currency'] != 'gems' or txn['category'] != 'CARDS'
                or FactScope(**json.loads(txn['detail'])['scope']) != operation.command.scope):
            raise ValueError('card journal scope or currency mismatch')
        conn.execute('UPDATE card_operations SET transaction_key=?,action_sequence=?,updated_at=? WHERE operation_id=?',
                     (transaction_key, action_sequence, now, operation_id))
        self.budget(operation.command.budget_cycle_id, conn=conn)  # Cap enforced by model; rollback on excess.
        return self._operation(conn, operation_id)

    def record_evidence(self, operation_id: str, *, expected_status: str, now: float,
                        snapshot_before: CardSnapshot | None = None, snapshot_after: CardSnapshot | None = None,
                        rewards: tuple[RewardItem, ...] = ()) -> CardOperation:
        """Enrich one operation; no lifecycle change and no independent spending amount."""
        _time(now)
        with self._write() as conn:
            operation = self._operation(conn, operation_id)
            if operation is None:
                raise KeyError(operation_id)
            if operation.status != expected_status or now < operation.updated_at:
                raise ValueError('card evidence compare-and-set conflict')
            changes: dict[str, Any] = {'updated_at': now, 'spent_gems': None}
            for name, value in (('snapshot_before', snapshot_before), ('snapshot_after', snapshot_after)):
                if value is not None:
                    if value.scope.account_id != operation.command.scope.account_id:
                        raise ValueError('foreign card evidence')
                    previous = getattr(operation, name)
                    if previous is not None and previous != value:
                        raise ValueError('card snapshot evidence is immutable')
                    changes[name] = value
            merged = {item.position: item for item in operation.rewards}
            for item in rewards:
                if item.position in merged and merged[item.position] != item:
                    raise ValueError('conflicting card reward position')
                merged[item.position] = item
            changes['rewards'] = tuple(merged[position] for position in sorted(merged))
            result = CardOperation.model_validate_json(operation.model_copy(update=changes).model_dump_json())
            conn.execute('UPDATE card_operations SET updated_at=?,detail=? WHERE operation_id=?',
                         (now, result.model_dump_json(), operation_id))
            return self._operation(conn, operation_id)
