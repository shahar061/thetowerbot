"""Operator resolution of an unproven mission reward intent.

    show --state PATH
        The unresolved mission intent (if any) and retained intents.
    resolve --state PATH --intent ID --operator NAME --evidence TEXT --worker-stopped
        Retain the intent uncredited with an audit record and stop it blocking
        mission claims. No receipt is written and no balance is minted.

PATH is the worker's mission-notification-state.json. Run with the worker
stopped: the worker rewrites this file while it runs. Nothing here touches a
device or provider.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from notification_state import NotificationState  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    show = sub.add_parser('show')
    show.add_argument('--state', type=Path, required=True)
    resolve = sub.add_parser('resolve')
    resolve.add_argument('--state', type=Path, required=True)
    for name in ('intent', 'operator', 'evidence'):
        resolve.add_argument(f'--{name}', required=True)
    resolve.add_argument('--worker-stopped', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        saved = json.loads(args.state.read_text(encoding='utf-8'))
        scope = saved['scope']
    except (OSError, ValueError, KeyError, TypeError):
        parser.error('mission notification state unreadable')
    state = NotificationState(args.state, scope=scope)
    row = state.snapshot()
    if args.command == 'show':
        claim = row.get('pending_claim')
        if claim:
            print('unresolved', claim['intent_id'], claim.get('mission'),
                  'verification_attempts', claim.get('verification_attempts'))
        for item in row.get('retained_intents', ()):
            print('retained', item['intent_id'], item.get('mission'), item['retained_reason'],
                  item.get('operator') or '-')
        return 0
    try:
        state.operator_resolve_claim(args.intent, operator=args.operator,
                                     evidence=args.evidence, now=time.time())
    except ValueError as exc:
        parser.error(str(exc))
    print('retained', args.intent, 'uncredited')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
