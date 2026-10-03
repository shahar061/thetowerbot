"""Scoped Cards evidence, existing journal budgets, and durable ledger repair.

These adapters grant no reader or device capability. Callers must supply evidence
from calibrated readers and claim device input separately before dispatch.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
import logging
import sqlite3

from pydantic import BaseModel, ConfigDict, Field, field_validator

import card_catalog
import db
import events
import ledger
from card_models import CardOperation, CardSnapshot, Id, RewardItem
from card_store import CardStore
from evidence_scope import BalanceInterval, FactScope, ScopeContinuity
from transactions import Intent, RecoveryEvidence, Transaction, TransactionJournal


log = logging.getLogger(__name__)


class CardResultEvidence(BaseModel):
    """Purchase acquisition proof is independent of reward identity readability."""
    model_config = ConfigDict(extra='forbid', strict=True, frozen=True, allow_inf_nan=False)
    operation_id: Id
    action_sequence: int = Field(ge=1)
    scope: FactScope
    visit_id: Id
    observed_at: float = Field(ge=0)
    evidence_ref: Id
    frame_digest: Id
    layout_id: Id
    acquisition_observed: bool
    reward_flow_complete: bool
    rewards: tuple[RewardItem, ...] = ()

    @field_validator('rewards', mode='before')
    @classmethod
    def tuple_rewards(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True)
class CardBudgetPrecondition:
    store: CardStore
    operation_id: str
    program_cap: int
    route_remaining: int | None = None

    def check(self, conn: sqlite3.Connection, intent: Intent, scope: FactScope) -> bool:
        """Called ONLY under the journal's BEGIN IMMEDIATE lock."""
        from pathlib import Path
        path = conn.execute('PRAGMA database_list').fetchone()[2]
        if Path(path).resolve() != self.store.path.resolve():
            raise ValueError('journal and cards must share one database')
        if any(type(value) is not int or value < 0 for value in (self.program_cap,)
               + (() if self.route_remaining is None else (self.route_remaining,))):
            raise ValueError('invalid Cards budget precondition')
        op = self.store._operation(conn, self.operation_id)
        if op is None:
            raise KeyError(self.operation_id)
        before = intent.before
        if (op.cancel_requested or op.status != 'preflight' or op.command.scope != scope
                or intent.category != 'CARDS' or intent.currency != 'gems'
                or intent.operation != {'buy': 'card_buy', 'slot': 'card_slot_buy'}.get(op.command.kind)
                or before.get('card_operation_id') != op.operation_id
                or type(before.get('action_sequence')) is not int or before['action_sequence'] != 1
                or before.get('scope') != asdict(scope)
                or type(before.get('quantity')) is not int or before['quantity'] != op.command.quantity
                or not isinstance(before.get('visit_id'), str) or not before['visit_id']
                or not isinstance(before.get('evidence_digest'), str) or not before['evidence_digest']
                or op.command.kind == 'slot' and (type(before.get('source_capacity')) is not int
                                                 or before['source_capacity'] < 0)):
            return False
        if conn.execute(
            "SELECT 1 FROM card_operations WHERE account_id=? AND operation_id!=? "
            "AND status NOT IN ('confirmed','not_applied','canceled','blocked','queued') "
            "AND (status!='preflight' OR transaction_key IS NOT NULL) LIMIT 1",
            (scope.account_id, op.operation_id)).fetchone():
            return False
        budget = self.store.budget(op.command.budget_cycle_id, conn=conn)
        return (intent.price is not None
                and budget.spent + budget.pending + intent.price <= min(budget.cap, self.program_cap)
                and (self.route_remaining is None or intent.price <= self.route_remaining))

    def link(self, conn: sqlite3.Connection, intent: Intent) -> None:
        self.store.link_transaction(self.operation_id, transaction_key=intent.key,
                                    action_sequence=intent.before['action_sequence'], now=intent.ts, conn=conn)


def validated_journal_result(txn: Transaction, evidence: RecoveryEvidence, *,
                         conn: sqlite3.Connection, now: float) -> CardResultEvidence | None:
    """Return canonical authenticated evidence under the journal settlement lock."""
    try:
        result = CardResultEvidence.model_validate_json(json.dumps(evidence.card_result))
        op = CardStore._operation(conn, result.operation_id)
        if (op is None or op.transaction_key != txn.key or op.command.scope != txn.scope or now < op.updated_at
                or op.command.quantity != txn.before.get('quantity')
                or txn.operation != {'buy': 'card_buy', 'slot': 'card_slot_buy'}.get(op.command.kind)):
            return None
        merged = merge_rewards(op.rewards, result.rewards)
        if not (result.operation_id == txn.before.get('card_operation_id')
                and result.action_sequence == txn.before.get('action_sequence')
                and result.visit_id == txn.before.get('visit_id')
                and result.scope == evidence.scope and result.observed_at == evidence.observed_at
                and result.frame_digest == evidence.frame_digest and result.acquisition_observed
                and all(item.position < op.command.quantity and item.quantity == 1
                        and item.card_id in card_catalog.card_ids() for item in merged)):
            return None
        if txn.operation == 'card_slot_buy':
            snapshot = CardSnapshot.model_validate_json(json.dumps(evidence.card_snapshot))
            if merged or not _slot_growth_observed(txn, snapshot, result, now):
                return None
        return result.model_copy(update={'rewards': merged})
    except (ValueError, TypeError):
        return None


def persist_journal_result(conn: sqlite3.Connection, result: CardResultEvidence, *, now: float) -> None:
    """Retain canonical rewards in the same transaction as journal and ledger settlement."""
    if not conn.in_transaction:
        raise ValueError('card settlement requires the journal write transaction')
    op = CardStore._operation(conn, result.operation_id)
    if op is None or now < op.updated_at:
        raise ValueError('card settlement operation changed')
    saved = op.model_copy(update={'rewards': result.rewards, 'updated_at': now, 'spent_gems': None})
    conn.execute('UPDATE card_operations SET updated_at=?,detail=? WHERE operation_id=?',
                 (now, saved.model_dump_json(), op.operation_id))


def _slot_growth_observed(txn: Transaction, snapshot: CardSnapshot | None,
                          result: CardResultEvidence, now: float) -> bool:
    return (snapshot is not None and txn.acted_at is not None
            and type(txn.before.get('source_capacity')) is int
            and snapshot.scope == result.scope and snapshot.visit_id == result.visit_id
            and snapshot.capacity == txn.before['source_capacity'] + 1
            and _fresh(snapshot.observed_at, txn.acted_at, now)
            and bool(snapshot.frame_digest) and bool(snapshot.layout_id))


def merge_rewards(current: tuple[RewardItem, ...], incoming: tuple[RewardItem, ...]) -> tuple[RewardItem, ...]:
    merged: dict[int, RewardItem] = {}
    for item in current + incoming:
        if item.position in merged and merged[item.position] != item:
            raise ValueError('conflicting card reward position')
        merged[item.position] = item
    return tuple(merged[position] for position in sorted(merged))


def pin_card_assignment(store: CardStore, operation_id: str, *, desired: tuple[str, ...],
                        visit_id: str, now: float) -> None:
    """Persist the immutable final target; per-toggle input claims belong to CardVisit."""
    if (not math.isfinite(now) or now < 0 or not visit_id or not isinstance(desired, tuple)
            or len(desired) != len(set(desired)) or set(desired) - card_catalog.card_ids()):
        raise ValueError('invalid assignment intent')
    with store._write() as conn:
        op = store._operation(conn, operation_id)
        if op is None:
            raise KeyError(operation_id)
        store._current(conn, op.command.scope)
        if op.command.kind not in ('apply', 'clear') or op.status != 'preflight' or now < op.updated_at:
            raise ValueError('assignment requires current preflight')
        if op.command.kind == 'clear' and desired:
            raise ValueError('clear requires empty equipment')
        detail = json.dumps({'desired': desired, 'visit_id': visit_id, 'scope': asdict(op.command.scope),
                             'pinned_at': now}, sort_keys=True)
        prior = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?', (operation_id,)).fetchone()
        if prior is not None:
            old = json.loads(prior[0])
            if old['desired'] != list(desired) or old['visit_id'] != visit_id:
                raise ValueError('assignment target is immutable')
            return
        conn.execute('INSERT INTO card_assignment_intents VALUES (?,?)', (operation_id, detail))


def assignment_target(store: CardStore, operation_id: str) -> tuple[str, ...] | None:
    if not store.path.exists():
        return None
    with db.reader(store.path) as conn:
        if store._operation(conn, operation_id) is None:
            return None
        row = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?', (operation_id,)).fetchone()
        return tuple(json.loads(row[0])['desired']) if row else None


def _scope_valid(journal: TransactionJournal, original: FactScope, observed: FactScope,
                 continuity: ScopeContinuity | None, now: float) -> bool:
    with db.reader(journal.path) as conn:
        return journal.currencies.scope_matches(observed, 'gems', conn) and (
            original == observed or continuity is not None and continuity.original == original
            and continuity.current == observed and continuity.valid(now=now))


def _fresh(observed_at: float, boundary: float, now: float) -> bool:
    return boundary < observed_at <= now and now - observed_at <= 30


def _pending(store: CardStore, op: CardOperation, now: float) -> CardOperation:
    if op.status == 'reconciliation_required':
        return store.operation(op.operation_id)
    if op.status in ('preflight', 'dispatched', 'verifying'):
        return store.transition(op.operation_id, expected_status=op.status, status='reconciliation_required',
                                reason='card_reconciliation_pending', now=now)
    return op


def card_operation_event(op: CardOperation, txn: Transaction | None = None, *,
                         requested_equipped: tuple[str, ...] | None = None) -> events.Event:
    """Projection derives spending from the journal-overlaid operation only."""
    common = dict(operation_id=op.operation_id, account_id=op.command.scope.account_id,
                  result=op.status, verified_amount=op.spent_gems, dry_run=False,
                  quantity=op.command.quantity, source=op.command.source,
                  program_revision=op.command.program_revision, goal_id=op.command.goal_id,
                  budget_cycle_id=op.command.budget_cycle_id or None,
                  loadout_id=op.command.loadout_id, requested_equipped=requested_equipped,
                  transaction_key=op.transaction_key, ts=op.updated_at,
                  gems_before=txn.wallet_before if txn else None,
                  gems_after=txn.reconciliation.get('wallet_after') if txn else None,
                  snapshot_before=op.snapshot_before.model_dump(mode='json') if op.snapshot_before else None,
                  snapshot_after=op.snapshot_after.model_dump(mode='json') if op.snapshot_after else None,
                  rewards=tuple(item.model_dump(mode='json') for item in op.rewards))
    if op.command.kind in ('apply', 'clear'):
        return events.CardAssignmentObserved(**common)
    cls = events.CardSlotPurchased if op.command.kind == 'slot' else events.CardPurchaseObserved
    return cls(**common)


def repair_card_ledger(store: CardStore, journal: TransactionJournal) -> int:
    """Repair/enrich from durable operations and journal, independent of event retention."""
    if store.path.resolve() != journal.path.resolve():
        raise ValueError('journal and cards must share one database')
    repaired = 0
    with store._write() as conn:
        records = conn.execute('SELECT operation_id FROM card_operations WHERE account_id=?',
                               (db.connection_account(conn),)).fetchall()
        writer = ledger.LedgerWriter(conn)
        for record in records:
            op = store._operation(conn, record[0])
            if (op.command.kind in ('buy', 'slot') and op.spent_gems is not None
                    or op.command.kind in ('apply', 'clear') and (op.status == 'confirmed'
                        or op.status == 'canceled' and op.cancel_requested and op.snapshot_after is not None)):
                exists = conn.execute("SELECT 1 FROM ledger WHERE json_extract(detail,'$.card_operation_id')=?",
                                      (op.operation_id,)).fetchone()
                txn = None
                if op.transaction_key:
                    from transactions import _transaction
                    journal_row = conn.execute('SELECT * FROM transactions WHERE key=?', (op.transaction_key,)).fetchone()
                    if journal_row['outcome'] not in ('bought', 'free'):
                        continue
                    txn = _transaction(journal_row)
                pinned = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?',
                                      (op.operation_id,)).fetchone()
                requested = tuple(json.loads(pinned[0])['desired']) if pinned else None
                for line in writer.lines_for(card_operation_event(op, txn, requested_equipped=requested)):
                    db.insert_ledger(conn, replace(line, seq=None).as_row(), commit=False)
                repaired += not bool(exists)
    return repaired


def reconcile_card_operation(store: CardStore, journal: TransactionJournal, operation_id: str, *,
                             snapshot: CardSnapshot | None = None, rewards: tuple[RewardItem, ...] | None = None,
                             wallet: BalanceInterval | None = None, result: CardResultEvidence | None = None,
                             continuity: ScopeContinuity | None = None, now: float) -> CardOperation:
    if store.path.resolve() != journal.path.resolve() or not math.isfinite(now):
        raise ValueError('invalid card reconciliation context')
    op = store.operation(operation_id)
    if op is None:
        raise KeyError(operation_id)
    if now < op.updated_at:
        raise ValueError('card reconciliation time moved backwards')
    if op.command.kind in ('apply', 'clear'):
        return _reconcile_assignment(store, journal, op, snapshot, continuity, now)
    if op.command.kind not in ('buy', 'slot') or not op.transaction_key:
        raise ValueError('paid reconciliation requires a linked transaction')
    txn = journal._require(op.transaction_key)
    if txn.acted_at is None:
        return _pending(store, op, now)
    if result is None:
        # Raw rewards and a wallet are observations without purchase attribution.
        return _pending(store, op, now)
    if (result.operation_id != operation_id or result.action_sequence != txn.before.get('action_sequence')
            or result.visit_id != txn.before.get('visit_id') or not result.acquisition_observed
            or not _fresh(result.observed_at, txn.acted_at, now)
            or not _scope_valid(journal, op.command.scope, result.scope, continuity, now)):
        raise ValueError('unattributed card result evidence')
    if snapshot is not None and (snapshot.scope != result.scope or snapshot.visit_id != result.visit_id
                                 or not _fresh(snapshot.observed_at, txn.acted_at, now)):
        raise ValueError('unattributed card snapshot')
    merged = merge_rewards(op.rewards, result.rewards)
    if any(item.position >= op.command.quantity or item.quantity != 1
           or item.card_id not in card_catalog.card_ids() for item in merged):
        raise ValueError('reward position or quantity exceeds pinned command')
    if op.command.kind == 'slot' and merged:
        raise ValueError('slot purchase cannot contain card rewards')
    complete = (result.reward_flow_complete and len(merged) == op.command.quantity)
    acquired = True
    if op.command.kind == 'slot':
        acquired = _slot_growth_observed(txn, snapshot, result, now)
        complete = acquired
    exact_wallet = (wallet is not None and wallet.scope == result.scope and wallet.currency == 'gems'
                    and wallet.source == 'observed' and wallet.lower is not None and wallet.lower == wallet.upper
                    and _fresh(wallet.observed_at, txn.acted_at, now)
                    and wallet.observed_at == result.observed_at)
    # Reject wallet conflicts before generic judge's tolerance rules can apply.
    exact_drop = exact_wallet and txn.wallet_before - wallet.lower == txn.price
    if op.spent_gems is not None and exact_wallet and not exact_drop:
        raise ValueError('card wallet evidence conflicts with settled spend')
    authenticated = acquired and exact_drop
    evidence = RecoveryEvidence(category='CARDS', currency='gems', wallet_after=wallet.lower if exact_wallet else None,
                                effect_changed=True if authenticated else None, observed_at=result.observed_at,
                                frame_digest=result.frame_digest, scope=result.scope, continuity=continuity,
                                operation=txn.operation, card_operation_id=operation_id,
                                card_result=result.model_dump(mode='json'),
                                card_snapshot=snapshot.model_dump(mode='json') if snapshot is not None else None)
    # Save authenticated reward identity first; interrupted reconciliation can safely retry it.
    op = store.record_evidence(operation_id, expected_status=op.status, now=now, rewards=merged)
    outcome = journal.reconcile(txn.key, evidence, now=now)
    op = store.operation(operation_id)
    if outcome.spent is not None and complete and acquired:
        if snapshot is not None and op.snapshot_after is None:
            op = store.record_evidence(operation_id, expected_status=op.status, now=now, snapshot_after=snapshot)
        if op.status != 'confirmed':
            op = store.transition(operation_id, expected_status=op.status, status='confirmed', reason=None, now=now)
    else:
        op = _pending(store, op, now)
    repair_card_ledger(store, journal)
    log.info('card reconciliation observed', extra={'account_id': op.command.scope.account_id,
             'generation': op.command.scope.generation, 'operation_id': op.operation_id,
             'card_status': op.status, 'spent_gems': op.spent_gems})
    return op


def _reconcile_assignment(store: CardStore, journal: TransactionJournal, op: CardOperation,
                          snapshot: CardSnapshot | None, continuity: ScopeContinuity | None,
                          now: float) -> CardOperation:
    with db.reader(store.path) as conn:
        row = conn.execute('SELECT detail FROM card_assignment_intents WHERE operation_id=?', (op.operation_id,)).fetchone()
    if row is None:
        raise ValueError('assignment has no pinned target')
    pinned = json.loads(row[0])
    evidence = snapshot.equipment_evidence if snapshot else None
    dispatched_at = pinned.get('dispatched_at')
    complete = (type(dispatched_at) in (float, int)
                and op.status in ('dispatched', 'verifying', 'reconciliation_required', 'confirmed')
                and snapshot is not None and snapshot.equipment_complete
                and snapshot.equipped is not None and set(snapshot.equipped) == set(pinned['desired'])
                and snapshot.visit_id == pinned['visit_id']
                and _scope_valid(journal, op.command.scope, snapshot.scope, continuity, now)
                and evidence is not None and evidence.scope == snapshot.scope and evidence.visit_id == snapshot.visit_id
                and bool(evidence.frame_digest) and bool(evidence.layout_id)
                and _fresh(snapshot.observed_at, dispatched_at, now)
                and _fresh(evidence.observed_at, dispatched_at, now))
    if not complete:
        return _pending(store, op, now)
    if op.status != 'confirmed':
        op = store.record_evidence(op.operation_id, expected_status=op.status, now=now, snapshot_after=snapshot)
        op = store.transition(op.operation_id, expected_status=op.status, status='confirmed', reason=None, now=now)
    repair_card_ledger(store, journal)
    return op
