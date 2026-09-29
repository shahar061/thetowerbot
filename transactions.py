"""Durable intent for anything that spends.

The buy path has one window that nothing else covers. Between the tap that
spends coins and the event that records the purchase, the only record that
a tap was ever sent lives in Python memory. A process that dies in that
window has spent the money and kept no evidence of it, so the next start is
free to tap the same row again.

This module closes that window by writing the intent to sqlite BEFORE the
device action, and resolving it afterwards. A row in `transactions` is a
question the bot has put to the game and not yet had answered; a restart
finds those rows still open and refuses to spend again until they are.

`ledger` and this module answer different questions and must not be merged:
the ledger records what provably happened to the account, this records what
was attempted. The gap between them is exactly what a crash creates.
"""

from __future__ import annotations

import hashlib
import functools
import threading
import time
from contextlib import closing
import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

import db
import events
import ledger
import upgrades
from fleet import workshop_prices
from evidence_scope import BalanceInterval, FactScope, ScopeContinuity
from currencies import CurrencyRepository, _amount


# Process-local mutation fence shared by journal instances for the same file.
# Durable pending facts are populated only by the background recovery reader.
_recovery_lock = threading.Lock()
_recovery_fences: dict[str, dict[str, Any]] = {}

# A lab-slot tap is judged unlanded only on a read at least this long after it.
UNLANDED_SETTLE_SECONDS = 2.


def _recovery_mutation(method: Any) -> Any:
    @functools.wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with _recovery_lock:
            row = _recovery_fences[self._recovery_key]
            row['epoch'] += 1
            row['active'] += 1
            row['observed'] = None
        try:
            return method(self, *args, **kwargs)
        finally:
            with _recovery_lock:
                row['epoch'] += 1
                row['active'] -= 1
                row['observed'] = None
    return wrapped


def abbreviation_slack(value: int) -> int:
    """How far the real balance behind a header reading can be from it.

    The header shows three significant digits once a balance reaches four:
    "2.61K" parses to exactly 2610 but stands for anything near it. A
    reading under 1000 is shown in full and hides nothing.
    """
    digits = len(str(abs(value)))
    return 0 if digits <= 3 else 10 ** (digits - 3)


def reading_tolerance(*readings: int, rounded_amounts: int = 0) -> int:
    """How far apart balance figures may be while describing one real balance.

    Each header reading hides its abbreviation_slack. Each amount the game
    rounded before showing it adds one more coin: a run payout is fractional
    coins shown whole, so the next reading can land a coin either side of a
    running total that added the shown figure.
    """
    return sum(abbreviation_slack(reading) for reading in readings) + rounded_amounts


class Stage(str, Enum):
    """Where a transaction stands between intent and proof.

    `INTENDED` and `ACTED` are the two open stages, and the difference is
    the one a crash turns on: `INTENDED` means no device action was ever
    sent. `ACTED` means dispatch was durably claimed before input; it may
    have been sent, so semantic proof is required before further spending.
    """

    INTENDED = "intended"
    ACTED = "acted"
    RESOLVED = "resolved"


class TransactionInFlight(RuntimeError):
    """An attempt was opened while an earlier one is still unanswered.

    Raised rather than returned because there is no safe way to continue:
    the open transaction may already have spent the money, and the caller
    asking to spend again cannot tell. It has to reconcile first.
    """


class ActionAlreadyTaken(RuntimeError):
    """A second device action was claimed for one transaction.

    One transaction is one question put to the game. Two taps against a
    single record cannot be told apart afterwards - the wallet moved once
    or twice and nothing says which - so the second is refused.
    """


class Verdict(str, Enum):
    """What the evidence after the action actually supports.

    The two that are easy to conflate are kept apart on purpose. `BOUGHT`
    means currency provably left the wallet for this item. `FREE` means the
    item provably changed and nothing left the wallet. A free upgrade
    recorded as `BOUGHT` would invent a debit that never happened and leave
    the running balance permanently wrong.
    """

    BOUGHT = "bought"
    FREE = "free"
    REFUTED = "refuted"
    UNPROVEN = "unproven"


@dataclass(frozen=True, kw_only=True)
class Intent:
    """What the bot is about to attempt, recorded before it attempts it.

    `price` keeps `None` for an unreadable price rather than collapsing to
    zero: an unreadable price is a refusal to spend, not a free item.
    """

    item: str
    category: str | None = None
    currency: str | None = None
    price: int | None = None
    wallet_before: int | None = None
    evidence: frozenset[str] = field(default_factory=frozenset)
    ts: float = 0.0
    before: dict[str, Any] = field(default_factory=dict)
    operation: str | None = None

    @property
    def key(self) -> str:
        """A deterministic name for this attempt.

        Derived from the intent rather than randomly generated, so the same
        attempt replayed addresses the same row instead of quietly opening
        a second one beside it.
        """
        parts = (
            self.item,
            self.category or "",
            self.currency or "",
            "" if self.price is None else str(self.price),
            "" if self.wallet_before is None else str(self.wallet_before),
            repr(sorted(self.evidence)),
            repr(self.ts),
        )
        if self.operation in ('lab_start', 'lab_unlock'):
            parts += (self.operation, json.dumps(self.before, sort_keys=True))
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


@dataclass(frozen=True, kw_only=True)
class Outcome:
    """The answer to one transaction.

    `spent` is deliberately three-valued: a number that provably moved, 0
    for provably nothing, and None for moved-by-an-unknown-amount. Only the
    first of those may ever be reported as a price actually paid.
    """

    key: str
    verdict: Verdict
    spent: int | None
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class RecoveryEvidence:
    category: str | None
    currency: str | None
    wallet_after: int | None
    effect_changed: bool | None
    observed_at: float
    frame_digest: str
    effect_value: float | None = None
    scope: FactScope | None = None
    continuity: ScopeContinuity | None = None
    operation: str | None = None
    slot: int | None = None
    research_id: str | None = None
    target_level: int | None = None
    completes_at: float | None = None


@dataclass(frozen=True, kw_only=True)
class Transaction:
    """One persisted attempt, as it currently stands."""

    key: str
    stage: Stage
    item: str
    category: str | None = None
    currency: str | None = None
    price: int | None = None
    wallet_before: int | None = None
    evidence: frozenset[str] = field(default_factory=frozenset)
    ts: float = 0.0
    acted_at: float | None = None
    before: dict[str, Any] = field(default_factory=dict)
    scope: FactScope | None = None
    operation: str = 'workshop_buy'
    reconciliation: dict[str, Any] = field(default_factory=dict)


class TransactionJournal:
    """The durable half of every spend.

    Takes a path and opens a short connection per operation, the same way
    AccountRepository does and for the same reason: the session that calls
    this is built on one thread and runs on the scan loop's, and a sqlite
    connection belongs to the thread that created it.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._recovery_key = str(self.path.absolute())
        with _recovery_lock:
            _recovery_fences.setdefault(self._recovery_key,
                dict(epoch=0, active=0, observed=None, pending=True))
        # Creates the file and the schema when this is the first use.
        db.connect(self.path).close()
        self.currencies = CurrencyRepository(self.path)

    def recovery_snapshot(self) -> tuple[int, bool]:
        """Nonblocking cached facts; missing, stale or mutating means pending."""
        with _recovery_lock:
            row = _recovery_fences[self._recovery_key]
            stale = row['observed'] is None or time.monotonic() - row['observed'] > 2
            return row['epoch'], bool(stale or row['active'] or row['pending'])

    def refresh_recovery_snapshot(self) -> None:
        """Background only. Never clears or reconciles a durable purchase."""
        with _recovery_lock:
            epoch = _recovery_fences[self._recovery_key]['epoch']
        pending = bool(self.open_transactions())
        with _recovery_lock:
            row = _recovery_fences[self._recovery_key]
            if row['epoch'] == epoch and not row['active']:
                row.update(pending=pending, observed=time.monotonic())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=1.)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    @_recovery_mutation
    def prepare(self, intent: Intent, *, scope: FactScope, balance: BalanceInterval,
                reserve: int = 0) -> Transaction | None:
        """Reserve and record under one write lock; all spenders share commitments."""
        if intent.operation not in (None, 'workshop_buy', 'card_buy', 'lab_start', 'lab_unlock'):
            return None
        if intent.operation in ('lab_start', 'lab_unlock'):
            before = intent.before
            if (intent.category != 'LABS' or type(before.get('slot')) is not int
                    or not 1 <= before['slot'] <= 5 or not before.get('evidence_digest')
                    or intent.currency != ('coins' if intent.operation == 'lab_start' else 'gems')):
                return None
            if intent.operation == 'lab_start' and (
                    not isinstance(before.get('research_id'), str)
                    or not before['research_id'].startswith('labs.')
                    or type(before.get('target_level')) is not int or before['target_level'] < 1
                    or type(before.get('source_level')) is not int
                    or before['source_level'] != before['target_level'] - 1):
                return None
        if (balance.scope != scope or balance.currency != intent.currency
                or balance.lower is None or balance.source == 'estimated'
                or balance.source == 'derived' and (not balance.catalog_revision or not balance.modifier_revision)
                or not _amount(intent.price) or not _amount(reserve)
                or not 0 <= intent.ts - balance.observed_at <= 30
                or any(intent.before.get(key) is not None and intent.before[key] != getattr(balance, key)
                       for key in ('catalog_revision', 'modifier_revision'))):
            return None
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.currencies.scope_matches(scope, balance.currency, conn):
                return None
            existing = conn.execute("SELECT * FROM transactions WHERE key=?", (intent.key,)).fetchone()
            if existing is not None:
                txn = _transaction(existing)
                return txn if txn.scope == scope else None
            pending = conn.execute("SELECT key FROM transactions WHERE stage != ? LIMIT 1",
                                   (Stage.RESOLVED.value,)).fetchone()
            if pending:
                raise TransactionInFlight('reconcile pending intent before spending again')
            changed_since = conn.execute(
                "SELECT 1 FROM transactions WHERE currency=? AND acted_at>=? LIMIT 1",
                (intent.currency, balance.observed_at)).fetchone()
            if changed_since:
                return None
            committed = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM currency_commitments WHERE currency=?",
                                     (intent.currency,)).fetchone()[0]
            if balance.lower < intent.price + reserve + committed:
                return None
            conn.execute("INSERT INTO currency_commitments VALUES (?, ?, ?)",
                         (f'purchase:{intent.key}', intent.currency, intent.price))
            conn.execute("INSERT INTO transactions (key,ts,stage,item,category,currency,price,wallet_before,evidence,detail) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?)", (
                             intent.key, intent.ts, Stage.INTENDED.value, intent.item, intent.category,
                             intent.currency, intent.price, intent.wallet_before, json.dumps(sorted(intent.evidence)),
                             json.dumps({'before': intent.before, 'scope': asdict(scope), 'balance': asdict(balance),
                                         'operation': intent.operation or ('card_buy' if intent.category == 'CARDS' else 'workshop_buy')})))
            conn.execute("INSERT INTO currency_observations VALUES (?,?) ON CONFLICT(currency) DO UPDATE SET detail=excluded.detail",
                         (balance.currency, json.dumps(asdict(balance))))
        return self._require(intent.key)

    @_recovery_mutation
    def open(self, intent: Intent) -> Transaction:
        """Record the intent. Must be called before the device action.

        Refuses while any earlier attempt is still open, which is what
        stops a crash between tap and confirmation from being paid for
        twice: the row a dead process left behind is still unanswered, so
        the next attempt never gets as far as tapping.
        """
        if intent.operation in ('lab_start', 'lab_unlock'):
            raise ValueError('Lab spending requires scoped atomic prepare')
        already = self.open_transactions()
        if already:
            raise TransactionInFlight(
                f"{already[0].item} is still open at stage "
                f"{already[0].stage.value}; reconcile it before spending again"
            )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO transactions "
                    "(key, ts, stage, item, category, currency, price, "
                    "wallet_before, evidence, detail) "
                    "VALUES (:key, :ts, :stage, :item, :category, :currency, "
                    ":price, :wallet_before, :evidence, :detail)",
                    {
                        "key": intent.key,
                        "ts": intent.ts,
                        "stage": Stage.INTENDED.value,
                        "item": intent.item,
                        "category": intent.category,
                        "currency": intent.currency,
                        "price": intent.price,
                        "wallet_before": intent.wallet_before,
                        "evidence": json.dumps(sorted(intent.evidence)),
                        "detail": json.dumps({"before": intent.before, 'operation': intent.operation or
                                              ('card_buy' if intent.category == 'CARDS' else 'workshop_buy')}),
                    },
                )
        finally:
            conn.close()
        return self._require(intent.key)

    @_recovery_mutation
    def record_action(self, key: str, *, at: float | None = None) -> Transaction:
        """Mark that the one device action this transaction owns was sent.

        Called before dispatch: a crash may leave a possibly-sent action, but
        can never leave a sent action looking safe to replay.
        """
        current = self._row(key)
        if current is None:
            raise KeyError(f"no transaction {key}")
        if current["stage"] != Stage.INTENDED.value:
            raise ActionAlreadyTaken(
                f"{current['item']} already acted at {current['acted_at']}"
            )
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "UPDATE transactions SET stage = ?, acted_at = ? WHERE key = ? AND stage = ?",
                    (Stage.ACTED.value, at, key, Stage.INTENDED.value),
                )
                if cursor.rowcount != 1:
                    raise ActionAlreadyTaken(key)
        finally:
            conn.close()
        return self._require(key)

    @_recovery_mutation
    def resolve(
        self,
        key: str,
        *,
        wallet_after: int | None,
        effect_changed: bool | None,
        ts: float | None = None,
        scope: FactScope | None = None,
        evidence_ref: str = '',
    ) -> Outcome:
        """Close a transaction against the evidence that followed it.

        `effect_changed` is three-valued like everything else here: True
        means the item provably changed, False means it provably did not,
        and None means nothing readable said either way.
        """
        row = self._row(key)
        if row is None:
            raise KeyError(f"no transaction {key}")

        txn = _transaction(row)
        if txn.scope is not None:
            return self.reconcile(key, RecoveryEvidence(
                category=txn.category, currency=txn.currency, wallet_after=wallet_after,
                effect_changed=effect_changed, observed_at=ts or 0., frame_digest=evidence_ref,
                scope=scope), now=ts or 0.)

        outcome = judge(
            key,
            price=row["price"],
            wallet_before=row["wallet_before"],
            wallet_after=wallet_after,
            effect_changed=effect_changed,
            price_in_catalog=_price_in_catalog(row),
        )
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE transactions SET stage = ?, resolved_at = ?, "
                    "outcome = ?, spent = ?, detail = ? WHERE key = ?",
                    (
                        Stage.RESOLVED.value,
                        ts,
                        outcome.verdict.value,
                        outcome.spent,
                        json.dumps({**json.loads(row['detail'] or '{}'), "reason": outcome.reason}),
                        key,
                    ),
                )
        finally:
            conn.close()
        return outcome

    @_recovery_mutation
    def cancel_before_input(self, key: str, *, scope: FactScope | None,
                            reason: str, now: float) -> Outcome:
        """Only the dispatch owner may report an explicit preflight refusal."""
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM transactions WHERE key=?', (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            txn = _transaction(row)
            if txn.scope != scope or (scope is not None and self.currencies.current_scope(conn) != scope):
                return Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None, reason='scope changed')
            if txn.stage == Stage.RESOLVED:
                return Outcome(key=key, verdict=Verdict(row['outcome']), spent=row['spent'])
            detail = json.loads(row['detail'] or '{}')
            detail['reason'] = reason
            conn.execute('UPDATE transactions SET stage=?, outcome=?, spent=0, resolved_at=?, detail=? WHERE key=?',
                         (Stage.RESOLVED.value, Verdict.REFUTED.value, now, json.dumps(detail), key))
            conn.execute('DELETE FROM currency_commitments WHERE owner=? AND currency=?',
                         (f'purchase:{key}', txn.currency))
            return Outcome(key=key, verdict=Verdict.REFUTED, spent=0, reason=reason)

    @_recovery_mutation
    def refute_unlanded_unlock(self, key: str, evidence: RecoveryEvidence, *, now: float) -> Outcome:
        """Settle a lab-slot tap that provably did nothing as not charged (spent 0).

        Only for an acted, scoped `lab_unlock` whose own visit read the same slot
        still locked and the gem wallet unchanged, at least UNLANDED_SETTLE_SECONDS
        after the tap. Anything less returns UNPROVEN and leaves the row open. No
        ledger line is written, because nothing was spent.
        """
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM transactions WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            detail = json.loads(row["detail"] or "{}")
            if row["stage"] == Stage.RESOLVED.value:
                return Outcome(key=key, verdict=Verdict(row["outcome"]), spent=row["spent"],
                               reason=detail.get("reason"))
            txn = _transaction(row)
            acted = txn.acted_at
            proven = (
                txn.operation == 'lab_unlock' and txn.stage == Stage.ACTED and txn.scope is not None
                and acted is not None and evidence.scope is not None and evidence.scope == txn.scope
                and self.currencies.scope_matches(evidence.scope, txn.currency, conn)
                and evidence.operation == 'lab_unlock' and evidence.slot == txn.before.get('slot')
                and evidence.category == txn.category and txn.currency == evidence.currency == 'gems'
                and evidence.effect_changed is False and bool(evidence.frame_digest)
                and evidence.wallet_after is not None and evidence.wallet_after == txn.wallet_before
                and acted + UNLANDED_SETTLE_SECONDS <= evidence.observed_at <= now
                and now - evidence.observed_at <= 30)
            if not proven:
                return Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None,
                               reason='unlanded unlock not proven')
            reason = 'lab unlock tap did not land: slot still locked and gems unchanged'
            detail.update(reason=reason, reconciliation=asdict(evidence))
            conn.execute("UPDATE transactions SET stage = ?, outcome = ?, spent = 0, resolved_at = ?, "
                         "detail = ? WHERE key = ?",
                         (Stage.RESOLVED.value, Verdict.REFUTED.value, now, json.dumps(detail), key))
            conn.execute("DELETE FROM currency_commitments WHERE owner=? AND currency=?",
                         (f'purchase:{key}', txn.currency))
            conn.execute("DELETE FROM currency_observations WHERE currency=?", (txn.currency,))
            return Outcome(key=key, verdict=Verdict.REFUTED, spent=0, reason=reason)

    def open_transactions(self) -> tuple[Transaction, ...]:
        """Every attempt still waiting for an answer, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE stage IN (?, ?) ORDER BY ts, rowid",
                (Stage.INTENDED.value, Stage.ACTED.value),
            ).fetchall()
        finally:
            conn.close()
        return tuple(_transaction(row) for row in rows)

    def resolved_unproven_keys(self, currency: str, *, before: float) -> tuple[str, ...]:
        """Closed uncertain attempts older than a fresh wallet observation.

        Their purchase cannot be replayed. Once a newer wallet has been read,
        a reservation from the old attempt no longer protects any spend.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT key FROM transactions WHERE stage = ? AND outcome = ? "
                "AND currency = ? AND resolved_at < ?",
                (Stage.RESOLVED.value, Verdict.UNPROVEN.value, currency, before),
            ).fetchall()
        finally:
            conn.close()
        return tuple(row["key"] for row in rows)

    @staticmethod
    def recovery_event(txn: Transaction, outcome: Outcome) -> events.Event:
        if txn.operation == 'lab_start':
            return events.LabResearchStarted(
                concept_id=txn.before['research_id'], slot=txn.before['slot'],
                source_level=txn.before.get('source_level'), target_level=txn.before.get('target_level'),
                price=outcome.spent, coins_before=txn.wallet_before,
                coins_after=txn.reconciliation.get('wallet_after', txn.wallet_before - outcome.spent),
                completes_at=txn.reconciliation.get('completes_at'), transaction_key=txn.key)
        if txn.operation == 'lab_unlock':
            return events.LabSlotUnlocked(slot=txn.before['slot'], price=outcome.spent,
                gems_before=txn.wallet_before,
                gems_after=txn.reconciliation.get('wallet_after', txn.wallet_before-outcome.spent),
                transaction_key=txn.key)
        return events.Purchased(
            item=txn.item, category=txn.category, price=txn.price,
            coins_before=txn.wallet_before if txn.currency == "coins" else None,
            gems_before=txn.wallet_before if txn.currency == "gems" else None,
            dry_run=False, verdict=outcome.verdict.value, spent=outcome.spent,
            transaction_key=txn.key,
        )

    @_recovery_mutation
    def reconcile(self, key: str, evidence: RecoveryEvidence, *, now: float) -> Outcome:
        """Persist proof and its ledger debit together; uncertainty keeps the gate shut."""
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM transactions WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                detail = json.loads(row["detail"] or "{}")
                if row["stage"] == Stage.RESOLVED.value:
                    return Outcome(key=key, verdict=Verdict(row["outcome"]), spent=row["spent"],
                                   reason=detail.get("reason"))
                txn = _transaction(row)
                scope_matches = txn.scope is None and evidence.scope is None
                if txn.scope is not None:
                    scope_matches = evidence.scope is not None and self.currencies.scope_matches(
                        evidence.scope, txn.currency, conn) and (
                        evidence.scope == txn.scope or evidence.continuity is not None
                        and evidence.continuity.original == txn.scope
                        and evidence.continuity.current == evidence.scope
                        and evidence.continuity.valid(now=now))
                boundary = txn.acted_at if txn.stage == Stage.ACTED else txn.ts
                relevant = (
                    scope_matches and boundary is not None
                    and (txn.stage == Stage.ACTED or txn.scope is not None and txn.stage == Stage.INTENDED)
                    and boundary < evidence.observed_at <= now
                    and now - evidence.observed_at <= 30 and bool(evidence.frame_digest)
                    and evidence.category == txn.category and evidence.currency == txn.currency
                    and txn.currency in ("coins", "gems", "cash", "stones")
                    and txn.price is not None and txn.price >= 0
                    and txn.wallet_before is not None and txn.wallet_before >= 0
                    and evidence.wallet_after is not None and evidence.wallet_after >= 0
                )
                if txn.operation in ('lab_start', 'lab_unlock'):
                    relevant = relevant and (
                        evidence.operation == txn.operation and evidence.slot == txn.before.get('slot')
                        and (txn.operation == 'lab_unlock' or
                             evidence.research_id == txn.before.get('research_id')
                             and evidence.target_level == txn.before.get('target_level')))
                outcome = judge(key, price=txn.price, wallet_before=txn.wallet_before,
                                wallet_after=evidence.wallet_after,
                                effect_changed=evidence.effect_changed if relevant else None)
                undispatched = (relevant and txn.scope is not None and txn.stage == Stage.INTENDED
                                and evidence.effect_changed is False and evidence.wallet_after == txn.wallet_before)
                if undispatched:
                    outcome = Outcome(key=key, verdict=Verdict.REFUTED, spent=0,
                                      reason='dispatch was never claimed; unchanged semantic evidence')
                proven = (txn.stage == Stage.ACTED and outcome.verdict in (Verdict.BOUGHT, Verdict.FREE)
                          and outcome.spent is not None) or undispatched
                if not proven:
                    outcome = Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None,
                                      reason="restart evidence is insufficient; further actions blocked")
                saved_evidence = asdict(evidence)
                if evidence.continuity is not None:
                    saved_evidence['continuity']['root'] = str(evidence.continuity.root)
                detail.update(reason=outcome.reason, reconciliation=saved_evidence)
                if not proven and relevant and evidence.effect_changed is not None:
                    # A confirmed in-scope read of the item after the action;
                    # close_unproven may now settle it (see there).
                    detail.setdefault('inspected_at', evidence.observed_at)
                conn.execute(
                    "UPDATE transactions SET stage = ?, outcome = ?, spent = ?, resolved_at = ?, detail = ? "
                    "WHERE key = ?",
                    (Stage.RESOLVED.value if proven else txn.stage.value, outcome.verdict.value,
                     outcome.spent, now if proven else None, json.dumps(detail), key),
                )
                if proven:
                    conn.execute("DELETE FROM currency_commitments WHERE owner=? AND currency=?",
                                 (f'purchase:{key}', txn.currency))
                    # Pre-action wallet cannot authorize another purchase after any effect.
                    conn.execute("DELETE FROM currency_observations WHERE currency=?", (txn.currency,))
                    if not undispatched:
                        event = replace(self.recovery_event(replace(txn, reconciliation=saved_evidence), outcome), ts=now)
                        for line in ledger.LedgerWriter(conn).lines_for(event):
                            db.insert_ledger(conn, replace(line, seq=None).as_row(), commit=False)
                return outcome
        finally:
            conn.close()

    OPERATOR_VERDICTS = ('not_charged', 'unproven')
    OPERATOR_RECENT_SECONDS = 600.

    @_recovery_mutation
    def operator_reconcile(self, key: str, *, verdict: str, operator: str, evidence: str,
                           now: float, worker_stopped: bool = False) -> Outcome:
        """Auditable operator resolution of one open intent; never a DB hand edit.

        ``not_charged``: the operator verified the debit did not happen (spent 0).
        ``unproven``: the outcome stays unknown and uncredited (spent NULL).
        Either releases the reservation and discards pre-action wallet
        observations, so only a newer wallet read can authorize a spend. No
        ledger line is minted. A settled outcome is never rewritten, and a
        recent intent requires the operator's confirmation that its worker is
        stopped (it may still be reconciling in process).
        """
        if verdict not in self.OPERATOR_VERDICTS:
            raise ValueError(f'verdict must be one of {self.OPERATOR_VERDICTS}')
        if not operator.strip() or not evidence.strip():
            raise ValueError('operator and evidence are required')
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS transaction_reconciliations ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, verdict TEXT NOT NULL, "
                    "operator TEXT NOT NULL, evidence TEXT NOT NULL, reconciled_at REAL NOT NULL, "
                    "previous_stage TEXT NOT NULL, previous_outcome TEXT, scoped INTEGER NOT NULL)")
                row = conn.execute("SELECT * FROM transactions WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                if row["stage"] == Stage.RESOLVED.value:
                    raise ValueError('transaction already settled; settled outcomes are never rewritten')
                txn = _transaction(row)
                last = txn.acted_at if txn.acted_at is not None else txn.ts
                if not worker_stopped and now - last < self.OPERATOR_RECENT_SECONDS:
                    raise ValueError('intent is recent; confirm its worker is stopped (--worker-stopped)')
                detail = json.loads(row["detail"] or "{}")
                reason = f'operator {operator}: {verdict}'
                detail.update(reason=reason, operator_reconciliation={
                    'verdict': verdict, 'operator': operator, 'evidence': evidence, 'at': now})
                outcome = (Outcome(key=key, verdict=Verdict.REFUTED, spent=0, reason=reason)
                           if verdict == 'not_charged'
                           else Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None, reason=reason))
                conn.execute(
                    "INSERT INTO transaction_reconciliations (key, verdict, operator, evidence, "
                    "reconciled_at, previous_stage, previous_outcome, scoped) VALUES (?,?,?,?,?,?,?,?)",
                    (key, verdict, operator, evidence, now, row["stage"], row["outcome"],
                     int(txn.scope is not None)))
                conn.execute(
                    "UPDATE transactions SET stage = ?, outcome = ?, spent = ?, resolved_at = ?, "
                    "detail = ? WHERE key = ?",
                    (Stage.RESOLVED.value, outcome.verdict.value, outcome.spent, now,
                     json.dumps(detail), key))
                conn.execute("DELETE FROM currency_commitments WHERE owner=? AND currency=?",
                             (f'purchase:{key}', txn.currency))
                conn.execute("DELETE FROM currency_observations WHERE currency=?", (txn.currency,))
                return outcome
        finally:
            conn.close()

    def operator_reconciliations(self, key: str | None = None) -> tuple[tuple[Any, ...], ...]:
        conn = self._connect()
        try:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='transaction_reconciliations'"
                            ).fetchone() is None:
                return ()
            query = ("SELECT key, verdict, operator, evidence, reconciled_at, previous_stage, "
                     "previous_outcome, scoped FROM transaction_reconciliations")
            rows = (conn.execute(query + " WHERE key=? ORDER BY id", (key,)) if key is not None
                    else conn.execute(query + " ORDER BY id")).fetchall()
            return tuple(tuple(r) for r in rows)
        finally:
            conn.close()

    @_recovery_mutation
    def close_unproven(self, key: str, *, reason: str, now: float) -> Outcome:
        """Give up on proof: resolve UNPROVEN with the spend left unknown.

        Scoped attempts remain open and reserved until a read-only check has
        confirmed the item's row in scope after the action (``inspected_at``):
        the state is then observed, only the debit is unknowable (battle
        income moved the wallet), so waiting longer cannot prove more. Such
        a row settles like an operator ``unproven``: spend unknown and never
        credited, reservation released, pre-action wallet discarded. Legacy
        rows retain their historical resolved-unknown representation and
        release nothing here.
        """
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM transactions WHERE key = ?", (key,)).fetchone()
                if row is None:
                    raise KeyError(key)
                detail = json.loads(row["detail"] or "{}")
                if row["stage"] == Stage.RESOLVED.value:
                    return Outcome(key=key, verdict=Verdict(row["outcome"]), spent=row["spent"],
                                   reason=detail.get("reason"))
                detail["reason"] = reason
                txn = _transaction(row)
                if txn.scope is not None and detail.get('inspected_at') is None:
                    conn.execute("UPDATE transactions SET outcome=?, spent=NULL, detail=? WHERE key=?",
                                 (Verdict.UNPROVEN.value, json.dumps(detail), key))
                    return Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None, reason=reason)
                if txn.scope is not None:
                    conn.execute("DELETE FROM currency_commitments WHERE owner=? AND currency=?",
                                 (f'purchase:{key}', txn.currency))
                    conn.execute("DELETE FROM currency_observations WHERE currency=?", (txn.currency,))
                conn.execute(
                    "UPDATE transactions SET stage = ?, outcome = ?, spent = NULL, resolved_at = ?, "
                    "detail = ? WHERE key = ?",
                    (Stage.RESOLVED.value, Verdict.UNPROVEN.value, now, json.dumps(detail), key),
                )
                return Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None, reason=reason)
        finally:
            conn.close()

    def recovered_visit(self, *, operations: set[str] | None = None) -> tuple[tuple[Transaction, Outcome], ...]:
        """Receipts still owned by the interrupted visit, including after another crash."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM transactions WHERE stage = ? "
                "AND json_extract(detail, '$.reconciliation') IS NOT NULL "
                "AND COALESCE(json_extract(detail, '$.visit_closed'), 0) = 0 ORDER BY ts",
                (Stage.RESOLVED.value,),
            ).fetchall()
            return tuple((_transaction(row), Outcome(
                key=row["key"], verdict=Verdict(row["outcome"]), spent=row["spent"],
                reason=json.loads(row["detail"]).get("reason"),
            )) for row in rows if _transaction(row).operation in
                (operations if operations is not None else {'workshop_buy', 'card_buy'}))
        finally:
            conn.close()

    @_recovery_mutation
    def finish_recovered_visit(self, keys: set[str]) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.executemany(
                    "UPDATE transactions SET detail = json_set(detail, '$.visit_closed', 1) "
                    "WHERE key = ? AND stage = ?",
                    ((key, Stage.RESOLVED.value) for key in keys),
                )
        finally:
            conn.close()

    def _row(self, key: str) -> Any:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM transactions WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()

    def _require(self, key: str) -> Transaction:
        row = self._row(key)
        if row is None:
            raise KeyError(f"no transaction {key}")
        return _transaction(row)


def judge(
    key: str,
    *,
    price: int | None,
    wallet_before: int | None,
    wallet_after: int | None,
    effect_changed: bool | None,
    price_in_catalog: bool = False,
) -> Outcome:
    """The whole confirmation rule, kept pure so it needs no database.

    `price_in_catalog` says the read price is one the attributed catalog
    lists for this item - a second, independent source for the same number.

    Nothing here ever turns missing evidence into a number. `UNPROVEN` is
    the default because it is the only verdict that stays true when the
    evidence has run out.
    """
    drop = (
        None
        if wallet_before is None or wallet_after is None
        else wallet_before - wallet_after
    )

    if price == 0 and effect_changed and drop == 0:
        # A read 0 is itself only a reading. The unmoved wallet is what
        # proves nothing was paid; without it a misread price would certify
        # a paid upgrade as free.
        return Outcome(
            key=key,
            verdict=Verdict.FREE,
            spent=0,
            reason="the item changed and it cost nothing",
        )

    if effect_changed and drop is not None and drop == price:
        return Outcome(
            key=key,
            verdict=Verdict.BOUGHT,
            spent=price,
            reason="the item changed and the wallet fell by its price",
        )

    slack = (None if wallet_before is None or wallet_after is None
             else reading_tolerance(wallet_before, wallet_after))
    if (effect_changed and drop is not None and drop > 0 and price is not None
            and (price_in_catalog or slack < price and abs(drop - price) <= slack)):
        # One level was bought, and it costs one price. A drop that misses
        # it by no more than the header's abbreviation hides ("2.61K" is
        # any balance near 2610) is that price; a price misread by a digit
        # misses by far more. A price no bigger than the slack cannot be
        # told apart from rounding at all. The catalog vouching for the read price
        # settles it whatever the wallet did. Any residual - rounding, or
        # income mid-purchase - is priced by the next balance reading.
        return Outcome(
            key=key,
            verdict=Verdict.BOUGHT,
            spent=price,
            reason="the item changed and the wallet fell by about its price",
        )

    if effect_changed and drop is not None and drop > 0:
        # The item changed and currency left the wallet, but not by the
        # amount predicted - income landed in the same window, or the price
        # was misread. Which of those it was cannot be known from here, so
        # the amount stays unknown rather than being asserted as the price.
        return Outcome(
            key=key,
            verdict=Verdict.BOUGHT,
            spent=None,
            reason="the wallet fell, but not by the price that was read",
        )

    if effect_changed is False and drop == 0:
        # Both halves are needed. An unchanged item alone can be an OCR
        # miss; an unmoved wallet alone can be income cancelling a debit.
        # Together they say the tap did nothing, and only then is the item
        # safe to attempt again.
        return Outcome(
            key=key,
            verdict=Verdict.REFUTED,
            spent=0,
            reason="neither the item nor the wallet moved",
        )

    return Outcome(
        key=key,
        verdict=Verdict.UNPROVEN,
        spent=None,
        reason="the evidence after the action did not settle what happened",
    )


def _price_in_catalog(row: Any) -> bool:
    """Does the attributed catalog list this transaction's read price?"""
    if row["currency"] != "coins" or row["price"] is None:
        return False
    upgrade = upgrades.resolve(row["item"], row["category"])
    return upgrade is not None and workshop_prices.lists_price(upgrade.id, row["price"])


def _transaction(row: Any) -> Transaction:
    data = dict(row)
    raw = data.get("evidence")
    return Transaction(
        key=data["key"],
        stage=Stage(data["stage"]),
        item=data["item"],
        category=data["category"],
        currency=data["currency"],
        price=data["price"],
        wallet_before=data["wallet_before"],
        evidence=frozenset(json.loads(raw)) if raw else frozenset(),
        ts=data["ts"],
        acted_at=data["acted_at"],
        before=json.loads(data.get("detail") or "{}").get("before", {}),
        scope=(FactScope(**json.loads(data['detail'])['scope'])
               if json.loads(data.get('detail') or '{}').get('scope') else None),
        operation=json.loads(data.get('detail') or '{}').get('operation') or
                  ('card_buy' if data['category'] == 'CARDS' else 'workshop_buy'),
        reconciliation=json.loads(data.get('detail') or '{}').get('reconciliation', {}),
    )
