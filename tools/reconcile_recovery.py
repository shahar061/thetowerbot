"""Operator reconciliation of recovery actions whose post-input outcome is unknown.

    list
        Open incidents that are blocked by an unresolved action.
    reconcile --worker W --account A --incident I --action ACT \
              --outcome {confirmed,no_effect,invalidated,not_dispatched} \
              --operator NAME --evidence TEXT
        [--worker-stopped]  required for an unrecorded (NULL) action, which may
        be a live worker's in-flight reservation.
        Record the operator's reviewed outcome through RecoveryEpisodes with an
        append-only audit row. Known outcomes are never rewritten; the incident
        is closed with its cooldown by the worker on its next open.
    audit --incident I
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

from recovery_episodes import EpisodeNamespace, RecoveryEpisodes  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fleet-root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list')
    reconcile = sub.add_parser('reconcile')
    for name in ('worker', 'account', 'incident', 'action', 'operator', 'evidence'):
        reconcile.add_argument(f'--{name}', required=True)
    reconcile.add_argument('--worker-stopped', action='store_true',
                           help='operator confirms the owning worker is stopped; required '
                                'for an unrecorded (possibly in-flight) action')
    reconcile.add_argument('--outcome', required=True,
                           choices=('confirmed', 'no_effect', 'invalidated', 'not_dispatched'))
    audit = sub.add_parser('audit')
    audit.add_argument('--incident', required=True)
    args = parser.parse_args(argv)
    if not (args.fleet_root / 'recovery-episodes.sqlite3').exists():
        parser.error('no recovery episode store under this fleet root')
    episodes = RecoveryEpisodes(args.fleet_root)
    if args.command == 'list':
        for episode in episodes.unresolved():
            states = dict(episodes.action_states(incident_id=episode.incident_id))
            actions = ','.join(f"{action}:{states.get(action) or 'unrecorded'}"
                               for action in episode.unresolved_action_ids)
            print('unresolved', episode.namespace.worker, episode.namespace.account_id,
                  episode.incident_id, actions)
    elif args.command == 'audit':
        for row in episodes.reconciliations(incident_id=args.incident):
            print('reconciled', *row)
    else:
        try:
            episode = episodes.reconcile(
                namespace=EpisodeNamespace(args.worker, args.account),
                incident_id=args.incident, action_id=args.action, outcome=args.outcome,
                operator=args.operator, evidence=args.evidence, now=time.time(),
                allow_unrecorded=args.worker_stopped)
        except ValueError as exc:
            parser.error(str(exc))
        print('reconciled', episode.incident_id, args.action, args.outcome, episode.status)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
