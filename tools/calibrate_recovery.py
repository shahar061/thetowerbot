"""Explicit local registration/activation of operator-reviewed recovery evidence.

    register MANIFEST --replay REPLAY --recorded-canary CANARY
        Validate the manifest contract (schema/host-contract version, action,
        screen/target, worker/account/model scope, lifetime) and that both
        artifact files hash to the manifest's digests. Registration is inert.
    activate ID --operator NAME   Operator review; only then may assist use it.
    revoke ID --operator NAME     Withdraw it.
    list                          Active grants and the audit trail.

The store lives at FLEET_ROOT/recovery-capabilities.sqlite3 and is empty until an
operator runs these commands. Nothing here calls a provider or touches a device.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import time
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recovery_capabilities import CalibrationEvidence, CapabilityRegistry  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--fleet-root', type=Path, required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    register = sub.add_parser('register')
    register.add_argument('manifest', type=Path)
    register.add_argument('--replay', type=Path, required=True)
    register.add_argument('--recorded-canary', type=Path, required=True)
    for command in ('activate', 'revoke'):
        edit = sub.add_parser(command)
        edit.add_argument('identifier')
        edit.add_argument('--operator', required=True)
    sub.add_parser('list')
    args = parser.parse_args(argv)
    store = CapabilityRegistry(args.fleet_root)
    if args.command == 'register':
        item = CalibrationEvidence.model_validate_json(args.manifest.read_text())
        for path, expected in ((args.replay, item.replay_sha256),
                               (args.recorded_canary, item.canary_sha256)):
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                parser.error('evidence artifact digest mismatch')
        print(store.register(item, now=time.time()))
    elif args.command == 'list':
        for grant in store.active(now=time.time()):
            print('active', grant.worker, grant.account_id, grant.model, grant.action,
                  grant.target, grant.expires_at)
        for identifier, operation, operator, at in store.audit():
            print('audit', identifier, operation, operator, at)
    else:
        getattr(store, args.command)(args.identifier, operator=args.operator, now=time.time())
        print(f'{args.command}: {args.identifier}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
