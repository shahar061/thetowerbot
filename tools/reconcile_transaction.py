"""Operator reconciliation of open spending intents (Workshop, Cards, Labs).

    list
        Every open intent (scoped or legacy unscoped) that blocks spending.
    reconcile --key K --verdict {not_charged,unproven} --operator NAME --evidence TEXT
        [--worker-stopped]  required when the intent is younger than 10 minutes,
        because its worker may still be reconciling it in process.
        Resolve one open intent through TransactionJournal with an append-only
        audit row. ``not_charged`` records spent 0 after the operator verified
        no debit; ``unproven`` keeps the outcome unknown and uncredited. The
        reservation is released and pre-action wallet readings are discarded,
        so only a newer wallet read can authorize another spend. No ledger line
        is minted, and an already settled outcome is never rewritten.
    audit [--key K]
        Show the reconciliation audit trail.

The database is never edited by hand. Nothing here touches a device or provider.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transactions import TransactionJournal  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, required=True, help="the worker's account database")
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    reconcile = sub.add_parser('reconcile')
    for name in ('key', 'operator', 'evidence'):
        reconcile.add_argument(f'--{name}', required=True)
    reconcile.add_argument('--verdict', required=True, choices=TransactionJournal.OPERATOR_VERDICTS)
    reconcile.add_argument('--worker-stopped', action='store_true',
                           help='operator confirms the owning worker is stopped')
    audit = sub.add_parser('audit')
    audit.add_argument('--key')
    args = parser.parse_args(argv)
    if not args.db.exists():
        parser.error('no account database at this path')
    journal = TransactionJournal(args.db)
    if args.command == 'list':
        for txn in journal.open_transactions():
            scope = 'unscoped' if txn.scope is None else f'{txn.scope.account_id}@epoch{txn.scope.epoch}'
            print('open', txn.key, txn.operation, txn.item, txn.currency, txn.price,
                  txn.stage.value, scope)
    elif args.command == 'audit':
        for row in journal.operator_reconciliations(args.key):
            print('reconciled', *row)
    else:
        try:
            outcome = journal.operator_reconcile(
                args.key, verdict=args.verdict, operator=args.operator, evidence=args.evidence,
                now=time.time(), worker_stopped=args.worker_stopped)
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        print('reconciled', args.key, outcome.verdict.value,
              'spent', 'unknown' if outcome.spent is None else outcome.spent)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
