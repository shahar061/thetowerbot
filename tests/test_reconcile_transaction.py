"""Operator transaction reconciliation: auditable, never rewrites a settled outcome."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import db
import transactions
from evidence_scope import BalanceInterval, FactScope
from tools import reconcile_transaction


SCOPE = FactScope('acct', 'lease', 'a'*32, 0)


def _scoped(journal: transactions.TransactionJournal, *, at: float) -> transactions.Transaction:
    journal.currencies.bind_scope(SCOPE)
    txn = journal.prepare(transactions.Intent(item='Damage', category='ATTACK', currency='coins',
        price=30, wallet_before=100, ts=at, before={'observed_at': at, 'frame_digest': 'd'}),
        scope=SCOPE, balance=BalanceInterval('coins', 100, 100, SCOPE, at, 'before'))
    journal.record_action(txn.key, at=at + 1)
    return txn


def _ledger_rows(path: Path) -> int:
    with db.reader(path) as conn:
        return conn.execute("SELECT count(*) FROM ledger").fetchone()[0]


def test_operator_resolves_epoch_stranded_scoped_intent_with_audit(tmp_path: Path) -> None:
    path = tmp_path / 'bot.db'
    journal = transactions.TransactionJournal(path)
    txn = _scoped(journal, at=100.)
    assert journal.currencies.committed('coins') == 30
    outcome = journal.operator_reconcile(txn.key, verdict='unproven', operator='shahar',
                                         evidence='wallet screenshot reviewed', now=10_000.)
    assert outcome.verdict is transactions.Verdict.UNPROVEN and outcome.spent is None
    assert journal.open_transactions() == ()
    assert journal.currencies.committed('coins') == 0
    assert _ledger_rows(path) == 0  # Never mints a balance or a debit.
    (row,) = journal.operator_reconciliations(txn.key)
    assert row[:3] == (txn.key, 'unproven', 'shahar') and row[5] == 'acted' and row[7] == 1
    with pytest.raises(ValueError, match='never rewritten'):
        journal.operator_reconcile(txn.key, verdict='not_charged', operator='x', evidence='y',
                                   now=10_001.)
    assert len(journal.operator_reconciliations()) == 1


def test_recent_intent_requires_confirmed_stopped_worker(tmp_path: Path) -> None:
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    txn = _scoped(journal, at=time.time())
    with pytest.raises(ValueError, match='worker is stopped'):
        journal.operator_reconcile(txn.key, verdict='not_charged', operator='o', evidence='e',
                                   now=time.time())
    outcome = journal.operator_reconcile(txn.key, verdict='not_charged', operator='o',
                                         evidence='e', now=time.time(), worker_stopped=True)
    assert outcome.verdict is transactions.Verdict.REFUTED and outcome.spent == 0


def test_cli_lists_and_reconciles_legacy_unscoped_row(tmp_path: Path, capsys) -> None:
    path = tmp_path / 'bot.db'
    journal = transactions.TransactionJournal(path)
    legacy = journal.open(transactions.Intent(item='Damage', category='ATTACK', currency='coins',
                                              price=10, wallet_before=13, ts=1.))
    journal.record_action(legacy.key, at=2.)
    assert reconcile_transaction.main(['--db', str(path), 'list']) == 0
    assert 'unscoped' in capsys.readouterr().out
    assert reconcile_transaction.main(['--db', str(path), 'reconcile', '--key', legacy.key,
        '--verdict', 'unproven', '--operator', 'op', '--evidence', 'legacy row']) == 0
    assert 'reconciled' in capsys.readouterr().out
    assert journal.open_transactions() == ()
    assert reconcile_transaction.main(['--db', str(path), 'audit']) == 0
    assert legacy.key in capsys.readouterr().out
    with pytest.raises(SystemExit):
        reconcile_transaction.main(['--db', str(path), 'reconcile', '--key', legacy.key,
            '--verdict', 'unproven', '--operator', 'op', '--evidence', 'again'])
