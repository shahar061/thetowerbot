"""Cards settlement uses scoped semantic proof and one durable debit identity."""
from dataclasses import asdict, replace
from pathlib import Path
import json

import pytest

import db
from card_models import CardCommand, CardOperation, CardSnapshot, RewardItem
from card_store import CardStore
from currencies import CurrencyRepository
from evidence_scope import BalanceInterval, FactScope
from transactions import Intent, TransactionJournal

SCOPE = FactScope('a', 'lease', 'generation', 1)


def setup(tmp_path: Path, *, kind: str = 'buy', quantity: int = 1, cap: int = 300) -> tuple:
    path = tmp_path / 'account.db'
    db.bind_account(path, 'a')
    CurrencyRepository(path).bind_scope(SCOPE)
    store = CardStore(path)
    store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=cap, now=1.)
    command = CardCommand(idempotency_key='key', scope=SCOPE, program_revision='p1',
                          kind=kind, quantity=quantity, source='manual', budget_cycle_id='cycle',
                          loadout_id='loadout' if kind == 'apply' else None)
    operation = store.submit(command, now=2.)
    store.transition(operation.operation_id, expected_status='queued', status='preflight', reason=None, now=3.)
    return store, TransactionJournal(path), operation.operation_id


def balance(value: int = 500, *, now: float = 10., scope: FactScope = SCOPE) -> BalanceInterval:
    return BalanceInterval('gems', value, value, scope, now, 'wallet')


def intent(operation_id: str, *, kind: str = 'buy', quantity: int = 1, price: int = 20) -> Intent:
    return Intent(item='Card', category='CARDS', currency='gems', price=price, wallet_before=500, ts=10.,
                  operation='card_slot_buy' if kind == 'slot' else 'card_buy',
                  before={'card_operation_id': operation_id, 'action_sequence': 1,
                          'scope': asdict(SCOPE), 'visit_id': 'visit', 'quantity': quantity,
                          'source_capacity': 2, 'evidence_digest': 'before'})


def prepare(store: CardStore, journal: TransactionJournal, operation_id: str, **changes: object) -> object:
    from card_transactions import CardBudgetPrecondition
    txn = journal.prepare(intent(operation_id, **changes), scope=SCOPE, balance=balance(),
                          cards=CardBudgetPrecondition(store, operation_id, program_cap=300))
    assert txn is not None
    journal.record_action(txn.key, at=11.)
    store.transition(operation_id, expected_status='preflight', status='dispatched', reason=None, now=11.)
    return txn


def result(op_id: str, **changes: object) -> object:
    from card_transactions import CardResultEvidence
    values = dict(operation_id=op_id, action_sequence=1, scope=SCOPE, visit_id='visit',
                  observed_at=12., evidence_ref='result', frame_digest='result-frame', layout_id='calibrated',
                  acquisition_observed=True, reward_flow_complete=True,
                  rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),))
    values.update(changes)
    return CardResultEvidence(**values)


def rows(store: CardStore) -> list:
    with db.reader(store.path) as conn:
        return conn.execute("SELECT * FROM ledger WHERE kind IN ('CARD_BUY','CARD_SLOT_BUY','CARD_ASSIGN')").fetchall()


def test_two_copies_and_replayed_frames_keep_positions() -> None:
    from card_transactions import merge_rewards
    rewards = tuple(RewardItem(position=p, card_id='cards.damage', quantity=1) for p in (0, 1))
    assert merge_rewards(merge_rewards((), rewards), rewards) == rewards
    with pytest.raises(ValueError):
        merge_rewards(rewards, (RewardItem(position=0, card_id='cards.health', quantity=1),))


def test_wallet_drop_and_raw_rewards_cannot_prove_acquisition(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    actual = reconcile_card_operation(store, journal, op, snapshot=None,
                                      rewards=(RewardItem(position=0, card_id='cards.damage', quantity=1),),
                                      wallet=balance(480, now=12.), now=12.)
    assert actual.status == 'reconciliation_required'
    assert actual.spent_gems is None
    assert actual.rewards == ()
    assert not rows(store)


def test_spend_survives_unreadable_rewards_and_enriches_same_ledger_row(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    pending = reconcile_card_operation(store, journal, op, result=result(op, rewards=(), reward_flow_complete=False),
                                       wallet=balance(480, now=12.), now=12.)
    assert pending.spent_gems == 20
    assert pending.status == 'reconciliation_required'
    first = rows(store)
    assert len(first) == 1 and first[0]['delta'] == -20
    for _ in range(2):
        final = reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=13.)
    assert final.status == 'confirmed'
    final_rows = rows(store)
    assert len(final_rows) == 1 and final_rows[0]['id'] == first[0]['id']
    assert len(json.loads(final_rows[0]['detail'])['rewards']) == 1
    assert store.budget('cycle').spent == 20


@pytest.mark.parametrize('change', [dict(scope=replace(SCOPE, generation='new')), dict(operation_id='foreign'),
                                    dict(action_sequence=2), dict(visit_id='other'), dict(acquisition_observed=False),
                                    dict(rewards=(RewardItem(position=1, card_id='cards.damage', quantity=1),))])
def test_mismatched_evidence_cannot_settle(tmp_path: Path, change: dict) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    with pytest.raises(ValueError):
        reconcile_card_operation(store, journal, op, result=result(op, **change), wallet=balance(480, now=12.), now=12.)
    assert store.operation(op).spent_gems is None
    assert not rows(store)


def test_interval_lower_is_not_an_exact_wallet(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    wallet = replace(balance(480, now=12.), upper=490)
    actual = reconcile_card_operation(store, journal, op, result=result(op), wallet=wallet, now=12.)
    assert actual.spent_gems is None
    assert actual.status == 'reconciliation_required'


def test_slot_requires_observed_capacity_growth(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path, kind='slot')
    prepare(store, journal, op, kind='slot', price=50)
    snap = CardSnapshot(scope=SCOPE, revision=1, observed_at=12., visit_id='visit', capacity=2,
                        collection_complete=False, equipment_complete=False, frame_digest='slot', layout_id='cards')
    actual = reconcile_card_operation(store, journal, op, snapshot=snap, result=result(op, rewards=()),
                                      wallet=balance(450, now=12.), now=12.)
    assert actual.spent_gems is None
    actual = reconcile_card_operation(store, journal, op, snapshot=snap.model_copy(update={'capacity': 3}),
                                      result=result(op, rewards=()), wallet=balance(450, now=12.), now=13.)
    assert actual.status == 'confirmed' and actual.spent_gems == 50
    assert rows(store)[0]['kind'] == 'CARD_SLOT_BUY'


@pytest.mark.parametrize('program_cap,route_remaining,reserve', [(19, None, 0), (300, 19, 0), (300, None, 481)])
def test_atomic_preparation_rejects_edited_cap_route_or_shared_reserve(tmp_path: Path, program_cap: int,
                                                                    route_remaining: int | None, reserve: int) -> None:
    from card_transactions import CardBudgetPrecondition
    store, journal, op = setup(tmp_path)
    actual = journal.prepare(intent(op), scope=SCOPE, balance=balance(), reserve=reserve,
                             cards=CardBudgetPrecondition(store, op, program_cap=program_cap,
                                                          route_remaining=route_remaining))
    assert actual is None
    assert store.operation(op).transaction_key is None
    assert not journal.open_transactions()
    assert store.budget('cycle').pending == 0


def test_original_cap_and_link_failure_roll_back_everything(tmp_path: Path) -> None:
    from card_transactions import CardBudgetPrecondition
    store, journal, op = setup(tmp_path, cap=19)
    assert journal.prepare(intent(op), scope=SCOPE, balance=balance(),
                           cards=CardBudgetPrecondition(store, op, program_cap=300)) is None
    other = CardStore(tmp_path / 'other.db')
    with pytest.raises(ValueError):
        journal.prepare(intent(op), scope=SCOPE, balance=balance(), cards=CardBudgetPrecondition(other, op, program_cap=300))
    assert not journal.open_transactions()


def test_repair_after_resolution_and_pruned_events_uses_durable_identity(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation, repair_card_ledger
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=12.)
    with db.connect(store.path) as conn:
        conn.execute('DELETE FROM events')
        conn.execute('DELETE FROM ledger')
    assert repair_card_ledger(store, journal) == 1
    assert repair_card_ledger(store, journal) == 0
    line = rows(store)[0]
    assert line['delta'] == -20
    assert json.loads(line['detail'])['transaction_key'] == txn.key
    assert json.loads(line['detail'])['card_operation_id'] == op


def test_assignment_target_is_pinned_before_any_input(tmp_path: Path) -> None:
    from card_transactions import pin_card_assignment, assignment_target, reconcile_card_operation
    store, journal, op = setup(tmp_path, kind='apply')
    desired = ('cards.damage',)
    pin_card_assignment(store, op, desired=desired, visit_id='visit', now=10.)
    assert assignment_target(CardStore(store.path), op) == desired
    pin_card_assignment(store, op, desired=desired, visit_id='visit', now=10.)
    with pytest.raises(ValueError):
        pin_card_assignment(store, op, desired=(), visit_id='visit', now=10.)
    store.transition(op, expected_status='preflight', status='dispatched', reason=None, now=11.)
    from card_models import CardFieldEvidence
    evidence = CardFieldEvidence(scope=SCOPE, visit_id='visit', observed_at=12., evidence_ref='equipment',
                                 frame_digest='equipment', layout_id='cards')
    snap = CardSnapshot(scope=SCOPE, revision=2, observed_at=12., visit_id='visit', equipped=desired,
                        capacity=2, equipment_complete=True, collection_complete=False, equipment_evidence=evidence)
    actual = reconcile_card_operation(store, journal, op, snapshot=snap, now=12.)
    assert actual.status == 'confirmed'
    assert len(rows(store)) == 1 and rows(store)[0]['currency'] is None


def test_budget_link_failure_rolls_back_insert_and_commitment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from card_transactions import CardBudgetPrecondition
    store, journal, op = setup(tmp_path)
    original = store.link_transaction
    def fail_after_link(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise RuntimeError('crash before commit')
    monkeypatch.setattr(store, 'link_transaction', fail_after_link)
    with pytest.raises(RuntimeError):
        journal.prepare(intent(op), scope=SCOPE, balance=balance(), cards=CardBudgetPrecondition(store, op, program_cap=300))
    assert not journal.open_transactions()
    assert store.operation(op).transaction_key is None
    assert journal.currencies.committed('gems') == 0


def test_shared_commitments_and_concurrent_last_budget_are_enforced(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from card_transactions import CardBudgetPrecondition
    store, journal, op = setup(tmp_path, cap=20)
    other = store.submit(store.operation(op).command.model_copy(update={'idempotency_key': 'second'}), now=3.)
    store.transition(other.operation_id, expected_status='queued', status='preflight', reason=None, now=3.)
    with db.connect(store.path) as conn:
        conn.execute("INSERT INTO currency_commitments VALUES ('lab','gems',490)")
    assert journal.prepare(intent(op), scope=SCOPE, balance=balance(), cards=CardBudgetPrecondition(store, op, 20)) is None
    with db.connect(store.path) as conn:
        conn.execute("DELETE FROM currency_commitments WHERE owner='lab'")
    def claim(oid: str) -> bool:
        return journal.prepare(intent(oid), scope=SCOPE, balance=balance(), cards=CardBudgetPrecondition(store, oid, 20)) is not None
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, (op, other.operation_id))) == [False, True]
    assert store.budget('cycle').pending == 20


def test_legacy_card_key_stays_stable_and_linked_key_contains_operation_identity(tmp_path: Path) -> None:
    old = Intent(item='x1', category='CARDS', currency='gems', price=20, wallet_before=40, ts=1.)
    assert old.key == replace(old, before={'anything': 'legacy'}).key
    assert intent('one').key != intent('two').key
    store, journal, op = setup(tmp_path)
    assert journal.prepare(intent(op), scope=SCOPE, balance=balance()) is None
    from card_transactions import CardBudgetPrecondition
    malformed = replace(intent(op), before={**intent(op).before, 'quantity': 10})
    assert journal.prepare(malformed, scope=SCOPE, balance=balance(), cards=CardBudgetPrecondition(store, op, 300)) is None


def test_interrupted_finalization_has_durable_debit_and_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from card_transactions import reconcile_card_operation, repair_card_ledger
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    original = store.transition
    def crash(operation_id: str, **kwargs: object) -> CardOperation:
        raise RuntimeError('publication/finalization interrupted')
    monkeypatch.setattr(store, 'transition', crash)
    with pytest.raises(RuntimeError):
        reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=12.)
    assert store.operation(op).spent_gems == 20
    assert len(rows(store)) == 1
    monkeypatch.setattr(store, 'transition', original)
    with db.connect(store.path) as conn:
        conn.execute('DELETE FROM events')
    assert repair_card_ledger(store, journal) == 0
    final = reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=13.)
    assert final.status == 'confirmed'
    assert len(rows(store)) == 1


def test_repair_preserves_verified_wallet_and_legacy_event_cannot_double_debit(tmp_path: Path) -> None:
    import events
    import ledger
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=12.)
    assert rows(store)[0]['balance_after'] == 480
    with db.connect(store.path) as conn:
        assert ledger.LedgerWriter(conn).lines_for(events.Purchased(item='x1', category='CARDS', price=20,
            gems_before=500, dry_run=False, verdict='bought', spent=20, transaction_key=txn.key)) == []


def test_generic_recovery_cannot_bypass_scoped_result_envelope(tmp_path: Path) -> None:
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    outcome = journal.reconcile(txn.key, RecoveryEvidence(category='CARDS', currency='gems', wallet_after=480,
        effect_changed=True, observed_at=12., frame_digest='frame', scope=SCOPE,
        operation='card_buy', card_operation_id=op, card_result={}), now=12.)
    assert outcome.spent is None
    assert not rows(store)


def test_reward_enrichment_rejects_conflicting_wallet_and_foreign_snapshot(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    reconcile_card_operation(store, journal, op, result=result(op, rewards=(), reward_flow_complete=False),
                             wallet=balance(480, now=12.), now=12.)
    with pytest.raises(ValueError):
        reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(460, now=12.), now=13.)
    snap = CardSnapshot(scope=replace(SCOPE, generation='foreign'), visit_id='visit', revision=2, observed_at=12.,
                        collection_complete=False, equipment_complete=False)
    with pytest.raises(ValueError):
        reconcile_card_operation(store, journal, op, result=result(op), snapshot=snap, now=13.)
    assert store.operation(op).status == 'reconciliation_required'


def test_ledger_failure_rolls_back_card_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError('ledger unavailable')
    monkeypatch.setattr(db, 'insert_ledger', fail)
    with pytest.raises(RuntimeError):
        reconcile_card_operation(store, journal, op, result=result(op), wallet=balance(480, now=12.), now=12.)
    assert store.operation(op).spent_gems is None
    assert journal.currencies.committed('gems') == 20
    assert len(journal.open_transactions()) == 1


def test_ten_card_result_requires_ten_positions_and_replays_two_identical_copies(tmp_path: Path) -> None:
    from card_transactions import reconcile_card_operation
    store, journal, op = setup(tmp_path, quantity=10)
    prepare(store, journal, op, quantity=10, price=200)
    rewards = tuple(RewardItem(position=i, card_id='cards.damage', quantity=1) for i in range(10))
    actual = reconcile_card_operation(store, journal, op, result=result(op, rewards=rewards[:2]),
                                      wallet=balance(300, now=12.), now=12.)
    assert actual.status == 'reconciliation_required' and actual.spent_gems == 200
    actual = reconcile_card_operation(store, journal, op, result=result(op, rewards=rewards), now=13.)
    assert actual.status == 'confirmed' and len(actual.rewards) == 10
    assert len(rows(store)) == 1


def test_verified_continuity_allows_result_recovery_without_retagging_intent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from card_transactions import reconcile_card_operation
    from evidence_scope import verified_continuity
    from fleet.identity import IdentityEvidence
    from fleet.input_lease import InputLease
    # Use real binding files, lease, and continuity validator, not an account-only shortcut.
    first = replace(SCOPE, generation='a' * 32)
    second = replace(first, generation='b' * 32)
    monkeypatch.setitem(globals(), 'SCOPE', first)
    store, journal, op = setup(tmp_path)
    # Defaults in helper functions are definition-time values.
    from card_transactions import CardBudgetPrecondition
    txn = journal.prepare(intent(op), scope=first, balance=balance(scope=first), cards=CardBudgetPrecondition(store, op, 300))
    journal.record_action(txn.key, at=11.)
    store.transition(op, expected_status='preflight', status='dispatched', reason=None, now=11.)
    root = tmp_path / 'worker'
    (root / 'checkpoints').mkdir(parents=True)
    binding = dict(worker_id='worker', account_id='a', lease_id='lease', attempt_id='attempt',
                   endpoint='endpoint', created_at=1., observed_at=2., evidence_ref='identity')
    for scope, predecessor in ((first, None), (second, first.generation)):
        (root / 'checkpoints' / f'{scope.generation}.json').write_text(json.dumps({**binding,
            'generation': scope.generation, 'predecessor_generation': predecessor}))
    (root / 'fleet-registration.json').write_text(json.dumps(dict(account_id='a', job_id='attempt',
        state='registered', instance='worker', endpoint='endpoint', lease_id='lease',
        binding=str(root / 'checkpoints' / f'{second.generation}.json'))))
    InputLease(root / 'input-lease.json').grant(second.generation)
    CurrencyRepository(store.path).bind_scope(second)
    proof = verified_continuity(root, first, second, identity=IdentityEvidence('a', 12., 'new-id'), now=13.)
    assert proof is not None
    with pytest.raises(ValueError):
        reconcile_card_operation(store, journal, op, result=result(op, scope=second), wallet=balance(480, scope=second, now=12.), now=13.)
    actual = reconcile_card_operation(store, journal, op, result=result(op, scope=second),
        wallet=balance(480, scope=second, now=12.), continuity=proof, now=13.)
    assert actual.status == 'confirmed' and actual.command.scope == first


def test_proven_spend_with_unreadable_rewards_still_fences_next_paid_operation(tmp_path: Path) -> None:
    from card_transactions import CardBudgetPrecondition, reconcile_card_operation
    store, journal, op = setup(tmp_path)
    prepare(store, journal, op)
    reconcile_card_operation(store, journal, op, result=result(op, rewards=(), reward_flow_complete=False),
                             wallet=balance(480, now=12.), now=12.)
    other = store.submit(store.operation(op).command.model_copy(update={'idempotency_key': 'next'}), now=13.)
    store.transition(other.operation_id, expected_status='queued', status='preflight', reason=None, now=13.)
    next_intent = replace(intent(other.operation_id), wallet_before=480, ts=14.)
    assert journal.prepare(next_intent, scope=SCOPE, balance=balance(480, now=14.),
                           cards=CardBudgetPrecondition(store, other.operation_id, 300)) is None
    assert store.budget('cycle').spent == 20
    assert store.budget('cycle').pending == 0


@pytest.mark.parametrize('changes', [dict(operation_id=' '), dict(action_sequence=True), dict(observed_at=float('nan')),
                                    dict(unexpected=True), dict(visit_id='')])
def test_result_envelope_rejects_invalid_authority_fields(changes: dict) -> None:
    with pytest.raises(ValueError):
        result('operation', **changes)


def test_direct_journal_slot_result_requires_capacity_growth_evidence(tmp_path: Path) -> None:
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path, kind='slot')
    txn = prepare(store, journal, op, kind='slot', price=50)
    outcome = journal.reconcile(txn.key, RecoveryEvidence(category='CARDS', currency='gems', wallet_after=450,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_slot_buy', card_operation_id=op, card_result=result(op, rewards=()).model_dump(mode='json')), now=12.)
    assert outcome.spent is None
    assert not rows(store)
    assert journal.currencies.committed('gems') == 50


@pytest.mark.parametrize('persisted', [False, True])
def test_direct_journal_rejects_conflicting_positional_rewards(tmp_path: Path, persisted: bool) -> None:
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    original = RewardItem(position=0, card_id='cards.damage', quantity=1)
    conflict = RewardItem(position=0, card_id='cards.health', quantity=1)
    if persisted:
        store.record_evidence(op, expected_status='dispatched', now=11., rewards=(original,))
    incoming = (conflict,) if persisted else (original, conflict)
    outcome = journal.reconcile(txn.key, RecoveryEvidence(category='CARDS', currency='gems', wallet_after=480,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_buy', card_operation_id=op, card_result=result(op, rewards=incoming).model_dump(mode='json')), now=12.)
    assert outcome.spent is None
    assert not rows(store)
    assert journal.currencies.committed('gems') == 20


def test_assignment_capture_before_dispatch_cannot_confirm_even_after_status_changes(tmp_path: Path) -> None:
    from card_models import CardFieldEvidence
    from card_transactions import pin_card_assignment, reconcile_card_operation
    store, journal, op = setup(tmp_path, kind='apply')
    pin_card_assignment(store, op, desired=('cards.damage',), visit_id='visit', now=10.)
    store.transition(op, expected_status='preflight', status='dispatched', reason=None, now=11.)
    evidence = CardFieldEvidence(scope=SCOPE, visit_id='visit', observed_at=10.5, evidence_ref='old',
                                 frame_digest='old', layout_id='cards')
    snapshot = CardSnapshot(scope=SCOPE, revision=2, observed_at=10.5, visit_id='visit', equipped=('cards.damage',),
        capacity=2, equipment_complete=True, collection_complete=False, equipment_evidence=evidence)
    actual = reconcile_card_operation(store, journal, op, snapshot=snapshot, now=12.)
    assert actual.status == 'reconciliation_required'
    assert not rows(store)
    store.transition(op, expected_status='reconciliation_required', status='verifying', reason=None, now=13.)
    actual = reconcile_card_operation(store, journal, op, snapshot=snapshot, now=14.)
    assert actual.status == 'reconciliation_required'
    assert not rows(store)
    # The dispatch boundary survives later transitions; a genuine post-input capture works.
    fresh = snapshot.model_copy(update={'observed_at': 11.5,
        'equipment_evidence': evidence.model_copy(update={'observed_at': 11.5})})
    actual = reconcile_card_operation(store, journal, op, snapshot=fresh, now=14.)
    assert actual.status == 'confirmed'
    assert len(rows(store)) == 1


def test_direct_journal_slot_requires_fresh_scoped_snapshot_then_accepts_growth(tmp_path: Path) -> None:
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path, kind='slot')
    txn = prepare(store, journal, op, kind='slot', price=50)
    snapshot = CardSnapshot(scope=SCOPE, revision=2, observed_at=12., visit_id='visit', capacity=3,
        collection_complete=False, equipment_complete=False, frame_digest='slots-after', layout_id='cards')
    evidence = RecoveryEvidence(category='CARDS', currency='gems', wallet_after=450,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_slot_buy', card_operation_id=op, card_result=result(op, rewards=()).model_dump(mode='json'))
    for changes in ({'capacity': 2}, {'observed_at': 10.5}, {'scope': replace(SCOPE, generation='foreign')},
                    {'visit_id': 'different'}, {'frame_digest': None}, {'layout_id': None}):
        invalid = snapshot.model_copy(update=changes)
        outcome = journal.reconcile(txn.key, replace(evidence, card_snapshot=invalid.model_dump(mode='json')), now=12.)
        assert outcome.spent is None
        assert not rows(store)
    outcome = journal.reconcile(txn.key, replace(evidence, card_snapshot=snapshot.model_dump(mode='json')), now=12.)
    assert outcome.spent == 50
    assert len(rows(store)) == 1 and rows(store)[0]['kind'] == 'CARD_SLOT_BUY'


def test_generic_reward_replay_and_pruned_repair_preserve_canonical_positions(tmp_path: Path) -> None:
    from card_transactions import repair_card_ledger
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    reward = RewardItem(position=0, card_id='cards.damage', quantity=1)
    evidence = RecoveryEvidence(category='CARDS', currency='gems', wallet_after=480,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_buy', card_operation_id=op,
        card_result=result(op, rewards=(reward, reward)).model_dump(mode='json'))
    assert journal.reconcile(txn.key, evidence, now=12.).spent == 20
    first = rows(store)[0]
    assert json.loads(first['detail'])['rewards'] == [reward.model_dump(mode='json')]
    assert store.operation(op).rewards == (reward,)
    assert journal._require(txn.key).reconciliation['card_result']['rewards'] == [reward.model_dump(mode='json')]
    assert journal.reconcile(txn.key, evidence, now=13.).spent == 20
    assert len(rows(store)) == 1 and rows(store)[0]['id'] == first['id']
    with db.connect(store.path) as conn:
        conn.execute('DELETE FROM events')
        conn.execute('DELETE FROM ledger')
    assert repair_card_ledger(CardStore(store.path), TransactionJournal(store.path)) == 1
    assert repair_card_ledger(store, journal) == 0
    repaired = rows(store)
    assert len(repaired) == 1 and repaired[0]['delta'] == -20
    assert json.loads(repaired[0]['detail'])['rewards'] == [reward.model_dump(mode='json')]


def test_generic_reward_persistence_rolls_back_with_failed_ledger_projection(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path)
    txn = prepare(store, journal, op)
    evidence = RecoveryEvidence(category='CARDS', currency='gems', wallet_after=480,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_buy', card_operation_id=op, card_result=result(op).model_dump(mode='json'))
    original = db.insert_ledger
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError('projection failed')
    monkeypatch.setattr(db, 'insert_ledger', fail)
    with pytest.raises(RuntimeError):
        journal.reconcile(txn.key, evidence, now=12.)
    assert store.operation(op).rewards == () and store.operation(op).spent_gems is None
    assert journal.currencies.committed('gems') == 20
    monkeypatch.setattr(db, 'insert_ledger', original)
    assert journal.reconcile(txn.key, evidence, now=12.).spent == 20
    assert store.operation(op).rewards == (RewardItem(position=0, card_id='cards.damage', quantity=1),)


@pytest.mark.parametrize('kind,quantity', [('buy', 1), ('buy', 10), ('slot', 1)])
@pytest.mark.parametrize('source', ['manual', 'automatic'])
def test_initial_and_pruned_repair_preserve_immutable_command_context(
        tmp_path: Path, kind: str, quantity: int, source: str) -> None:
    from card_transactions import repair_card_ledger
    from transactions import RecoveryEvidence
    store, journal, op = setup(tmp_path, kind=kind, quantity=quantity)
    # Construct the durable request with its original goal/source before dispatch.
    command = store.operation(op).command.model_copy(update={
        'idempotency_key': 'context', 'source': source, 'goal_id': 'goal'})
    store.transition(op, expected_status='preflight', status='canceled', reason='superseded', now=4.)
    op = store.submit(command, now=5.).operation_id
    store.transition(op, expected_status='queued', status='preflight', reason=None, now=6.)
    txn = prepare(store, journal, op, kind=kind, quantity=quantity, price=20*quantity)
    rewards = tuple(RewardItem(position=i, card_id='cards.damage', quantity=1)
                    for i in range(quantity)) if kind == 'buy' else ()
    snap = CardSnapshot(scope=SCOPE, visit_id='visit', observed_at=12., revision=0,
        capacity=3, frame_digest='result-frame', layout_id='calibrated',
        collection_complete=False, equipment_complete=False) if kind == 'slot' else None
    evidence = RecoveryEvidence(category='CARDS', currency='gems', wallet_after=500-20*quantity,
        effect_changed=True, observed_at=12., frame_digest='result-frame', scope=SCOPE,
        operation='card_slot_buy' if kind == 'slot' else 'card_buy', card_operation_id=op,
        card_result=result(op, rewards=rewards).model_dump(mode='json'),
        card_snapshot=snap.model_dump(mode='json') if snap else None)
    outcome = journal.reconcile(txn.key, evidence, now=12.)
    assert outcome.spent == 20*quantity
    initial = rows(store)[0]
    expected = dict(quantity=quantity, source=source, program_revision='p1', goal_id='goal', budget_cycle_id='cycle')
    detail = json.loads(initial['detail'])
    assert {key: detail.get(key) for key in expected} == expected
    event = journal.recovery_event(journal._require(txn.key), outcome)
    assert {key: getattr(event, key) for key in expected} == expected
    with db.connect(store.path) as conn:
        conn.execute('DELETE FROM events')
    assert repair_card_ledger(store, journal) == 0
    assert journal.reconcile(txn.key, evidence, now=13.).spent == 20*quantity
    repaired = rows(store)
    assert len(repaired) == 1
    assert {key: json.loads(repaired[0]['detail']).get(key) for key in expected} == expected
    for key in ('id', 'delta', 'balance_after', 'currency', 'ts'):
        assert repaired[0][key] == initial[key]
    assert repaired[0]['balance_after'] == 500-20*quantity


@pytest.mark.parametrize('kind', ['buy', 'slot'])
def test_canceled_operation_cannot_prepare_paid_intent(tmp_path: Path, kind: str) -> None:
    from card_transactions import CardBudgetPrecondition
    store, journal, op = setup(tmp_path, kind=kind)
    store.request_cancel(op, now=4.)
    assert journal.prepare(intent(op, kind=kind), scope=SCOPE, balance=balance(),
        cards=CardBudgetPrecondition(store, op, program_cap=300)) is None
    assert journal.open_transactions() == ()
    assert store.budget('cycle').pending == 0
