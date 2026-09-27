"""Atomic, durable currency commitments shared by independent plans."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import re
import json
from dataclasses import asdict
from evidence_scope import BalanceInterval, FactScope
from pathlib import Path

import db


CURRENCIES = frozenset({
    "cash", "coins", "gems", "stones", "medals", "cells", "keys",
    "shards", "tickets", "bits",
})
_VERSIONED_RESOURCE = re.compile(r"resource:[a-z][a-z0-9_]*@[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class CommitmentError(ValueError):
    """A caller supplied an invalid currency, owner, or amount."""


def _amount(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _currency(value: object) -> bool:
    return isinstance(value, str) and (value in CURRENCIES or
                                       _VERSIONED_RESOURCE.fullmatch(value) is not None)


class CurrencyRepository:
    """One database for commitments made by shopping, labs, and UW plans.

    Commitments and observations persist together. A stored wallet is usable
    only under its exact active scope and original observation age; changing
    scope discards observations while preserving unresolved commitments.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        with closing(db.connect(self.path)) as conn, conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS currency_commitments (
                owner TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount >= 0),
                PRIMARY KEY (owner, currency)
            )""")

            conn.execute("CREATE TABLE IF NOT EXISTS fact_scope (id INTEGER PRIMARY KEY CHECK(id=1), detail TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS fact_run (id INTEGER PRIMARY KEY CHECK(id=1), run_id INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS currency_observations (currency TEXT PRIMARY KEY, detail TEXT NOT NULL)")

    def bind_scope(self, scope: FactScope) -> None:
        """Called by verified identity owner; observations never survive scope changes."""
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            namespace = db.connection_account(conn)
            if namespace is not None and namespace != scope.account_id:
                raise CommitmentError('database belongs to another account')
            prior = self.current_scope(conn)
            if prior != scope:
                conn.execute("DELETE FROM currency_observations")
            conn.execute("INSERT INTO fact_scope VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET detail=excluded.detail",
                         (json.dumps(asdict(scope)),))

    def current_scope(self, conn: sqlite3.Connection | None = None) -> FactScope | None:
        if conn is None:
            with closing(self._connect()) as connection, connection:
                return self.current_scope(connection)
        row = conn.execute("SELECT detail FROM fact_scope WHERE id=1").fetchone()
        return FactScope(**json.loads(row[0])) if row else None

    def set_run(self, run_id: int | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM currency_observations WHERE currency='cash'")
            conn.execute("INSERT INTO fact_run VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET run_id=excluded.run_id", (run_id,))

    def scope_matches(self, scope: FactScope, currency: str, conn: sqlite3.Connection) -> bool:
        from dataclasses import replace
        current = self.current_scope(conn)
        if current is not None and currency == 'cash':
            row = conn.execute("SELECT run_id FROM fact_run WHERE id=1").fetchone()
            current = replace(current, run_id=row[0]) if row and row[0] is not None else None
        return current == scope

    def observe(self, balance: BalanceInterval) -> bool:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self.scope_matches(balance.scope, balance.currency, conn):
                return False
            if conn.execute('SELECT 1 FROM transactions WHERE currency=? AND acted_at>=? LIMIT 1',
                            (balance.currency, balance.observed_at)).fetchone():
                return False
            row = conn.execute("SELECT detail FROM currency_observations WHERE currency=?", (balance.currency,)).fetchone()
            if row and json.loads(row[0])['observed_at'] > balance.observed_at:
                return False
            conn.execute("INSERT INTO currency_observations VALUES (?, ?) ON CONFLICT(currency) DO UPDATE SET detail=excluded.detail",
                         (balance.currency, json.dumps(asdict(balance))))
            return True

    def balance(self, currency: str, *, scope: FactScope, now: float,
                max_age: float = 30.) -> BalanceInterval | None:
        with closing(self._connect()) as conn, conn:
            if not self.scope_matches(scope, currency, conn):
                return None
            row = conn.execute("SELECT detail FROM currency_observations WHERE currency=?", (currency,)).fetchone()
        if not row:
            return None
        raw = json.loads(row[0])
        raw['scope'] = FactScope(**raw['scope'])
        balance = BalanceInterval(**raw)
        return balance if balance.scope == scope and 0 <= now - balance.observed_at <= max_age else None

    def available(self, balance: BalanceInterval) -> int | None:
        with closing(self._connect()) as conn, conn:
            if not self.scope_matches(balance.scope, balance.currency, conn):
                return None
        return None if balance.lower is None else max(0, balance.lower - self.committed(balance.currency))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _validate(owner: str, currency: str, amount: int) -> None:
        if not isinstance(owner, str) or not owner.strip():
            raise CommitmentError("owner must be a nonempty string")
        if not _currency(currency):
            raise CommitmentError(f"unknown currency {currency!r}")
        if not _amount(amount):
            raise CommitmentError("amount must be a non-negative integer")

    def committed(self, currency: str) -> int:
        if not _currency(currency):
            raise CommitmentError(f"unknown currency {currency!r}")
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM currency_commitments WHERE currency = ?",
                (currency,),
            ).fetchone()
        return int(row[0])

    def can_spend(self, currency: str, amount: int, *, wallet: int | None) -> bool:
        self._validate("purchase", currency, amount)
        return _amount(wallet) and wallet >= amount + self.committed(currency)

    def reserve(self, owner: str, currency: str, amount: int,
                *, wallet: int | None) -> bool:
        """Atomically reserve funds; identical retries leave the row unchanged."""
        self._validate(owner, currency, amount)
        if not _amount(wallet):
            return False
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT amount FROM currency_commitments WHERE owner = ? AND currency = ?",
                (owner, currency),
            ).fetchone()
            committed = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM currency_commitments WHERE currency = ?",
                (currency,),
            ).fetchone()[0]
            available = wallet - committed + (existing[0] if existing else 0)
            if amount > available:
                conn.rollback()
                return False
            conn.execute(
                "INSERT INTO currency_commitments(owner, currency, amount) VALUES (?, ?, ?) "
                "ON CONFLICT(owner, currency) DO UPDATE SET amount = excluded.amount",
                (owner, currency, amount),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def release(self, owner: str, currency: str) -> None:
        self._validate(owner, currency, 0)
        with closing(self._connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if owner.startswith('purchase:'):
                pending = conn.execute("SELECT 1 FROM transactions WHERE key=? AND stage != 'resolved' "
                                       "AND json_extract(detail,'$.scope') IS NOT NULL",
                                       (owner.removeprefix('purchase:'),)).fetchone()
                if pending:
                    raise CommitmentError('scoped reservation requires journal settlement')
            conn.execute(
                "DELETE FROM currency_commitments WHERE owner = ? AND currency = ?",
                (owner, currency),
            )


def currency_overview(path: Path, *, account_id: str, lease_id: str,
                      generation: str, now: float) -> dict[str, int | None]:
    """Read-only U1 adapter. Current scope and original observation age are required."""
    result = dict.fromkeys(('coins_lower', 'coins_upper', 'reserved', 'available_lower', 'gems'))
    try:
        with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=.1)) as conn:
            row = conn.execute('SELECT detail FROM fact_scope WHERE id=1').fetchone()
            if not row:
                return result
            scope = FactScope(**json.loads(row[0]))
            if (scope.account_id, scope.lease_id, scope.generation) != (account_id, lease_id, generation):
                return result
            for currency, raw in conn.execute('SELECT currency,detail FROM currency_observations'):
                data = json.loads(raw)
                data['scope'] = FactScope(**data['scope'])
                balance = BalanceInterval(**data)
                if balance.scope != scope or not 0 <= now - balance.observed_at <= 120:
                    continue
                if currency == 'coins':
                    reserved = conn.execute('SELECT COALESCE(SUM(amount),0) FROM currency_commitments WHERE currency=?',
                                            (currency,)).fetchone()[0]
                    result.update(coins_lower=balance.lower, coins_upper=balance.upper, reserved=reserved,
                                  available_lower=None if balance.lower is None else max(0, balance.lower-reserved))
                elif currency == 'gems' and balance.lower == balance.upper:
                    result['gems'] = balance.lower
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        return dict.fromkeys(result)
    return result
