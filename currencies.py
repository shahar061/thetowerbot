"""Atomic, durable currency commitments shared by independent plans."""
from __future__ import annotations

import sqlite3
import re
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

    A wallet observation is supplied afresh by the caller. Commitments are
    durable; wallet readings are deliberately not, because a reading from a
    previous screen or process cannot authorize a new purchase.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        with db.connect(self.path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS currency_commitments (
                owner TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount >= 0),
                PRIMARY KEY (owner, currency)
            )""")

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
        with self._connect() as conn:
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
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM currency_commitments WHERE owner = ? AND currency = ?",
                (owner, currency),
            )
