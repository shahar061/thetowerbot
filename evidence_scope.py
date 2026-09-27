"""Identity and provenance for actionable observations; history is never authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from fleet.identity import IdentityEvidence
from fleet.input_lease import InputLease, InputLeaseExpired


@dataclass(frozen=True)
class FactScope:
    account_id: str
    lease_id: str
    generation: str
    epoch: int
    run_id: int | None = None

    def __post_init__(self) -> None:
        if (not all(isinstance(v, str) and v.strip() for v in
                    (self.account_id, self.lease_id, self.generation))
                or type(self.epoch) is not int or self.epoch < 0
                or self.run_id is not None and (type(self.run_id) is not int or self.run_id <= 0)):
            raise ValueError('invalid fact scope')

    @property
    def bound(self) -> bool:
        return True


@dataclass(frozen=True)
class BalanceInterval:
    currency: str
    lower: int | None
    upper: int | None
    scope: FactScope
    observed_at: float
    evidence_ref: str
    source: str = 'observed'
    catalog_revision: str | None = None
    modifier_revision: str | None = None

    def __post_init__(self) -> None:
        from currencies import _currency
        if (not _currency(self.currency) or not isinstance(self.scope, FactScope)
                or any(v is not None and (type(v) is not int or v < 0) for v in (self.lower, self.upper))
                or self.lower is not None and self.upper is not None and self.lower > self.upper
                or type(self.observed_at) not in (float, int) or not math.isfinite(self.observed_at)
                or self.observed_at < 0 or not isinstance(self.evidence_ref, str) or not self.evidence_ref
                or self.source not in ('observed', 'derived', 'estimated')
                or self.currency == 'cash' and self.scope.run_id is None):
            raise ValueError('invalid balance interval or provenance')

    @classmethod
    def from_reading(cls, currency: str, value: int | None, scope: FactScope,
                     observed_at: float, evidence_ref: str) -> BalanceInterval:
        from transactions import abbreviation_slack
        slack = abbreviation_slack(value) if type(value) is int and value >= 0 else 0
        return cls(currency, None if value is None else max(0, value - slack),
                   None if value is None else value + slack, scope, observed_at, evidence_ref)


@dataclass(frozen=True)
class ScopeContinuity:
    """Reconciliation authority only. No old balance or intent becomes replayable."""
    original: FactScope
    current: FactScope
    identity: IdentityEvidence
    root: Path
    # Read-only reconciliation across a durable epoch bump (disconnect or
    # manual invalidation): the same account and lease, re-verified in
    # process, with a complete binding chain. Never replay or affordability.
    epoch_advance: bool = False

    def valid(self, *, now: float) -> bool:
        return verified_continuity(self.root, self.original, self.current,
                                   identity=self.identity, now=now,
                                   epoch_advance=self.epoch_advance) is not None


def verified_continuity(root: Path, original: FactScope, current: FactScope, *,
                        identity: IdentityEvidence, now: float,
                        epoch_advance: bool = False) -> ScopeContinuity | None:
    if epoch_advance:
        if (original.epoch >= current.epoch
                or replace(original, generation=current.generation, epoch=current.epoch) != current):
            return None
    elif (replace(original, generation=current.generation) != current
            or original.generation == current.generation):
        return None
    if identity.account_id != current.account_id or not 0 <= now - identity.observed_at <= 30:
        return None
    root = Path(root)
    try:
        if (root / 'generation-transition.json').exists():
            transition = json.loads((root / 'generation-transition.json').read_text())
            if transition.get('state') != 'complete':
                return None
        InputLease(root / 'input-lease.json').assert_current(current.generation)
        expected = _registered_binding(root, current)
        generation, seen = current.generation, set()
        while generation not in seen:
            if not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None:
                return None
            seen.add(generation)
            row = json.loads((root / 'checkpoints' / f'{generation}.json').read_text())
            if (row.get('generation') != generation or row.get('account_id') != current.account_id
                    or row.get('lease_id') != current.lease_id
                    or not all(isinstance(row.get(k), str) and row[k] for k in
                               ('worker_id', 'endpoint', 'attempt_id', 'evidence_ref'))
                    or type(row.get('observed_at')) not in (float, int)
                    or type(row.get('created_at')) not in (float, int)
                    or not 0 < row['created_at'] <= row['observed_at'] <= identity.observed_at):
                return None
            binding = tuple(row[k] for k in ('worker_id', 'endpoint', 'attempt_id', 'account_id', 'lease_id'))
            if binding != expected:
                return None
            if generation == original.generation:
                return ScopeContinuity(original, current, identity, root, epoch_advance)
            generation = row.get('predecessor_generation')
    except (OSError, ValueError, TypeError, KeyError, AttributeError, InputLeaseExpired):
        return None
    return None


def historical_scope_matches(root: Path, original: FactScope | Mapping[str, Any], current: FactScope,
                             *, destination_account: str | None, observed_at: float | None = None) -> bool:
    """Validate archived *confirmed* receipts without authorizing input or a wallet.

    Epoch and live lease freshness intentionally do not apply to historical ledger
    evidence. Registration and a complete immutable binding chain still must match.
    """
    source = asdict(original) if isinstance(original, FactScope) else original
    if (not isinstance(source, Mapping) or destination_account is None
            or source.get('account_id') != destination_account
            or current.account_id != destination_account or source.get('lease_id') != current.lease_id):
        return False
    try:
        expected = _registered_binding(root, current)
        if 'attempt_id' in source and source['attempt_id'] != expected[2]:
            return False
        generation, seen = current.generation, set()
        while generation not in seen:
            if not isinstance(generation, str) or re.fullmatch(r'[0-9a-f]{32}', generation) is None:
                return False
            seen.add(generation)
            row = json.loads((root / 'checkpoints' / f'{generation}.json').read_text())
            if (tuple(row.get(k) for k in ('worker_id','endpoint','attempt_id','account_id','lease_id')) != expected
                    or row.get('generation') != generation
                    or not isinstance(row.get('endpoint'), str) or not row['endpoint']
                    or not isinstance(row.get('evidence_ref'), str) or not row['evidence_ref']
                    or type(row.get('created_at')) not in (float,int)
                    or type(row.get('observed_at')) not in (float,int)
                    or not math.isfinite(row['observed_at'])
                    or not 0 < row['created_at'] <= row['observed_at']):
                return False
            if generation == source.get("generation"):
                return observed_at is None or row['observed_at'] <= observed_at
            generation = row.get('predecessor_generation')
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
    return False


def _registered_binding(root: Path, current: FactScope) -> tuple[str, str, str, str, str]:
    """Anchor checkpoint lineage to the trusted fleet registration, never itself."""
    registration = json.loads((root / 'fleet-registration.json').read_text())
    if (registration.get('state') != 'registered' or registration.get('instance') != root.name
            or registration.get('account_id') != current.account_id
            or registration.get('lease_id') != current.lease_id
            or not all(isinstance(registration.get(k), str) and registration[k]
                       for k in ('endpoint', 'job_id'))
            or Path(registration['binding']).resolve() !=
            (root / 'checkpoints' / f'{current.generation}.json').resolve()):
        raise ValueError('current registered binding mismatch')
    return (root.name, registration['endpoint'], registration['job_id'], current.account_id, current.lease_id)
