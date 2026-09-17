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
import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

import db
import events
import ledger


class Stage(str, Enum):
    """Where a transaction stands between intent and proof.

    `INTENDED` and `ACTED` are the two open stages, and the difference is
    the one a crash turns on: `INTENDED` means no device action was ever
    sent, so nothing was spent; `ACTED` means one was, and the outcome is
    unknown until the wallet is read again.
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


class TransactionJournal:
    """The durable half of every spend.

    Takes a path and opens a short connection per operation, the same way
    AccountRepository does and for the same reason: the session that calls
    this is built on one thread and runs on the scan loop's, and a sqlite
    connection belongs to the thread that created it.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # Creates the file and the schema when this is the first use.
        db.connect(self.path).close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=1.)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        return conn

    def open(self, intent: Intent) -> Transaction:
        """Record the intent. Must be called before the device action.

        Refuses while any earlier attempt is still open, which is what
        stops a crash between tap and confirmation from being paid for
        twice: the row a dead process left behind is still unanswered, so
        the next attempt never gets as far as tapping.
        """
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
                        "detail": json.dumps({"before": intent.before}),
                    },
                )
        finally:
            conn.close()
        return self._require(intent.key)

    def record_action(self, key: str, *, at: float | None = None) -> Transaction:
        """Mark that the one device action this transaction owns was sent.

        Called immediately after the tap returns, so the window in which a
        crash can lose the fact of the tap is one statement wide.
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
                conn.execute(
                    "UPDATE transactions SET stage = ?, acted_at = ? WHERE key = ?",
                    (Stage.ACTED.value, at, key),
                )
        finally:
            conn.close()
        return self._require(key)

    def resolve(
        self,
        key: str,
        *,
        wallet_after: int | None,
        effect_changed: bool | None,
        ts: float | None = None,
    ) -> Outcome:
        """Close a transaction against the evidence that followed it.

        `effect_changed` is three-valued like everything else here: True
        means the item provably changed, False means it provably did not,
        and None means nothing readable said either way.
        """
        row = self._row(key)
        if row is None:
            raise KeyError(f"no transaction {key}")

        outcome = judge(
            key,
            price=row["price"],
            wallet_before=row["wallet_before"],
            wallet_after=wallet_after,
            effect_changed=effect_changed,
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
                        json.dumps({"reason": outcome.reason}),
                        key,
                    ),
                )
        finally:
            conn.close()
        return outcome

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
    def recovery_event(txn: Transaction, outcome: Outcome) -> events.Purchased:
        return events.Purchased(
            item=txn.item, category=txn.category, price=txn.price,
            coins_before=txn.wallet_before if txn.currency == "coins" else None,
            gems_before=txn.wallet_before if txn.currency == "gems" else None,
            dry_run=False, verdict=outcome.verdict.value, spent=outcome.spent,
            transaction_key=txn.key,
        )

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
                relevant = (
                    txn.stage == Stage.ACTED and txn.acted_at is not None
                    and txn.acted_at < evidence.observed_at <= now
                    and now - evidence.observed_at <= 30 and bool(evidence.frame_digest)
                    and evidence.category == txn.category and evidence.currency == txn.currency
                    and txn.currency in ("coins", "gems")
                    and txn.price is not None and txn.price >= 0
                    and txn.wallet_before is not None and txn.wallet_before >= 0
                    and evidence.wallet_after is not None and evidence.wallet_after >= 0
                )
                outcome = judge(key, price=txn.price, wallet_before=txn.wallet_before,
                                wallet_after=evidence.wallet_after,
                                effect_changed=evidence.effect_changed if relevant else None)
                proven = outcome.verdict in (Verdict.BOUGHT, Verdict.FREE) and outcome.spent is not None
                if not proven:
                    outcome = Outcome(key=key, verdict=Verdict.UNPROVEN, spent=None,
                                      reason="restart evidence is insufficient; further actions blocked")
                detail.update(reason=outcome.reason, reconciliation=asdict(evidence))
                conn.execute(
                    "UPDATE transactions SET stage = ?, outcome = ?, spent = ?, resolved_at = ?, detail = ? "
                    "WHERE key = ?",
                    (Stage.RESOLVED.value if proven else txn.stage.value, outcome.verdict.value,
                     outcome.spent, now if proven else None, json.dumps(detail), key),
                )
                if proven:
                    event = replace(self.recovery_event(txn, outcome), ts=now)
                    for line in ledger.LedgerWriter(conn).lines_for(event):
                        db.insert_ledger(conn, replace(line, seq=None).as_row(), commit=False)
                return outcome
        finally:
            conn.close()

    def recovered_visit(self) -> tuple[tuple[Transaction, Outcome], ...]:
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
            )) for row in rows)
        finally:
            conn.close()

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
) -> Outcome:
    """The whole confirmation rule, kept pure so it needs no database.

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
    )
