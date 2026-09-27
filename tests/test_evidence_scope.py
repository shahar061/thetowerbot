from dataclasses import replace
import json

import pytest


def test_intervals_preserve_unknown_and_validate_bounds():
    from evidence_scope import BalanceInterval, FactScope
    scope = FactScope('acct', 'lease', 'g', 0)
    assert BalanceInterval('coins', None, None, scope, 10., 'frame').lower is None
    for lower, upper in ((-1, 2), (3, 2), (True, 2), (1, float('inf'))):
        with pytest.raises(ValueError):
            BalanceInterval('coins', lower, upper, scope, 10., 'frame')
    with pytest.raises(ValueError):
        BalanceInterval('cash', 1, 2, scope, 10., 'frame')
    assert BalanceInterval.from_reading('coins', 2610, scope, 10., 'frame').lower == 2600


def test_continuity_requires_complete_chain_current_lease_and_fresh_identity(tmp_path):
    from evidence_scope import FactScope, verified_continuity
    from fleet.identity import IdentityEvidence
    from fleet.input_lease import InputLease
    original = FactScope('acct', 'lease', 'a' * 32, 4)
    current = replace(original, generation='b' * 32)
    root = tmp_path
    (root / 'checkpoints').mkdir()
    binding = dict(account_id='acct', lease_id='lease', worker_id=root.name, endpoint='endpoint',
                   attempt_id='attempt', created_at=1., observed_at=2., evidence_ref='identity')
    for scope, predecessor in ((original, None), (current, original.generation)):
        (root / 'checkpoints' / f'{scope.generation}.json').write_text(json.dumps({
            **binding, 'generation': scope.generation, 'predecessor_generation': predecessor}))
    registration = dict(state='registered',instance=root.name,endpoint='endpoint',lease_id='lease',
        account_id='acct',job_id='attempt',binding=str(root / 'checkpoints' / f'{current.generation}.json'))
    (root / 'fleet-registration.json').write_text(json.dumps(registration))
    InputLease(root / 'input-lease.json').grant(current.generation)
    identity = IdentityEvidence('acct', 10., 'new-account-frame')
    proof = verified_continuity(root, original, current, identity=identity, now=11.)
    assert proof is not None and proof.original == original
    for field in ('worker_id', 'endpoint', 'attempt_id'):
        for scope in (original, current):
            path = root / 'checkpoints' / f'{scope.generation}.json'
            row = json.loads(path.read_text())
            row[field] = 'wrong'
            path.write_text(json.dumps(row))
        assert verified_continuity(root, original, current, identity=identity, now=11.) is None
        for scope in (original, current):
            path = root / 'checkpoints' / f'{scope.generation}.json'
            row = json.loads(path.read_text())
            row[field] = binding[field]
            path.write_text(json.dumps(row))
    assert verified_continuity(root, original, replace(current, epoch=5), identity=identity, now=11.) is None
    assert verified_continuity(root, original, current, identity=identity, now=100.) is None
    InputLease(root / 'input-lease.json').revoke(current.generation, 'test')
    assert verified_continuity(root, original, current, identity=identity, now=11.) is None
