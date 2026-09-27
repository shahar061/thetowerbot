"""Redacted recovery status document shared by the coordinator and fleet overview.

The coordinator writes one small JSON document per worker. Readers accept it
only when its worker/account/lease/attempt/generation scope matches the live,
already-verified process and its original observation time is fresh. Unknown
values stay None; nothing here carries keys, provider text or image data.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

STATUS_FILE = 'recovery-status.json'
SCHEMA_VERSION = 1
MAX_AGE_SECONDS = 120.

_TEXT = ('mode', 'model', 'phase', 'incident_id', 'last_outcome', 'blocker')
_INT = ('calls_remaining', 'cost_used_microusd', 'cost_reserved_microusd',
        'policy_revision', 'settings_revision')
_TIME = ('calls_observed_at', 'budget_observed_at')
_PROPOSAL = ('action_id', 'candidate_id', 'expected_postcondition')


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 160 else None


def _int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _time(value: object) -> float | None:
    return (float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0
            else None)


def redact(document: Mapping[str, Any]) -> dict[str, Any]:
    """Allowlist projection. Unknown or malformed values become None."""
    proposal = document.get('last_proposal')
    return {
        **{key: _text(document.get(key)) for key in _TEXT},
        **{key: _int(document.get(key)) for key in _INT},
        **{key: _time(document.get(key)) for key in _TIME},
        'configured': document.get('configured') is True,
        'policy_conflict': (document['policy_conflict']
                            if type(document.get('policy_conflict')) is bool else None),
        'last_proposal': ({key: _text(proposal.get(key)) for key in _PROPOSAL}
                          if isinstance(proposal, Mapping) else None),
        # No authoritative fleet-wide open-incident producer exists yet.
        'open_incidents': None,
        'observed_at': _time(document.get('observed_at')),
    }


def recovery_overview(worker_root: Path, *, account_id: str, lease_id: str, attempt_id: str,
                      generation: str, now: float,
                      max_age: float = MAX_AGE_SECONDS) -> dict[str, Any] | None:
    """Scoped, fresh recovery summary for U1's overview, else None (unknown)."""
    try:
        row = json.loads((Path(worker_root) / STATUS_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(row, dict) or row.get('schema_version') != SCHEMA_VERSION:
        return None
    scope = row.get('scope')
    expected = {'worker': Path(worker_root).name, 'account_id': account_id,
                'lease_id': lease_id, 'attempt_id': attempt_id,
                'attempt_generation': generation}
    if not isinstance(scope, dict) or any(scope.get(k) != v for k, v in expected.items()):
        return None
    observed = _time(row.get('observed_at'))
    if observed is None or not 0 <= now - observed <= max_age:
        return None
    return redact(row)


# -- configuration epoch -----------------------------------------------------
# Every writer of recovery mode/model/policy/capability authority replaces this
# token *after* its SQLite commit. The storage thread reads it *before* reading
# settings, so an unchanged token at the input boundary proves no such commit
# finished since that snapshot (except the microseconds between a writer's
# commit and its token replace). Reading it is one small file read, never SQLite.
CONFIG_EPOCH_FILE = 'recovery-config.epoch'


def bump_config_epoch(root: Path) -> None:
    """Best effort after a commit: a failed write must not report the committed
    change as failed. Safety then rests on the snapshot age bound."""
    import logging
    import os
    import tempfile
    from uuid import uuid4
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.recovery-epoch-', dir=root)
        try:
            with os.fdopen(fd, 'w') as stream:
                stream.write(uuid4().hex)
            os.replace(name, root / CONFIG_EPOCH_FILE)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    except OSError:
        logging.getLogger(__name__).warning(
            'recovery config epoch not replaced; input boundary falls back to the age bound')


def read_config_epoch(root: Path) -> str | None:
    """None when no writer has run yet; an unreadable file is a distinct value."""
    try:
        return (Path(root) / CONFIG_EPOCH_FILE).read_text()[:64]
    except FileNotFoundError:
        return None
    except OSError:
        return 'unreadable'
