"""Durable card observations, immutable commands and journal-derived budgets."""
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from card_store import CardStore
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import db
from card_models import CardCommand, CardItem, CardSnapshot
from currencies import CurrencyRepository
from evidence_scope import FactScope

SCOPE = FactScope('a', 'lease', 'generation', 1)


def setup_store(tmp_path: Path) -> "CardStore":
    from card_store import CardStore
    path = tmp_path / 'account.db'
    db.bind_account(path, 'a')
    CurrencyRepository(path).bind_scope(SCOPE)
    return CardStore(path)


def snapshot(**changes: object) -> CardSnapshot:
    data = dict(scope=SCOPE, revision=1, observed_at=10., visit_id='visit', items=(),
                capacity=2, equipped=('cards.damage',), collection_complete=False,
                equipment_complete=True)
    data.update(changes)
    return CardSnapshot(**data)


def command(**changes: object) -> CardCommand:
    data = dict(idempotency_key='key', scope=SCOPE, program_revision='p1', kind='refresh',
                quantity=1, source='manual')
    data.update(changes)
    return CardCommand(**data)


def test_read_only_old_database_does_not_initialize(tmp_path: Path) -> None:
    from card_store import CardStore
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE old_marker (value TEXT)')
    store = CardStore(path)
    assert store.snapshot() is None
    assert store.operation('missing') is None
    assert store.pending(SCOPE) == ()
    with pytest.raises(KeyError):
        store.budget('missing')
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute('SELECT name FROM sqlite_master')] == ['old_marker']
    missing = tmp_path / 'missing.db'
    assert CardStore(missing).snapshot() is None
    assert not missing.exists()


def test_partial_scan_preserves_history_and_reopen(tmp_path: Path) -> None:
    from card_store import CardStore
    store = setup_store(tmp_path)
    assert store.observe(snapshot())
    assert not store.observe(snapshot())
    assert not store.observe(snapshot(observed_at=9., visit_id='earlier'))
    assert store.observe(snapshot(revision=2, observed_at=20., equipped=None, equipment_complete=False))
    result = CardStore(store.path).snapshot()
    assert result.equipped == ('cards.damage',)
    assert not result.equipment_complete
    assert result.equipment_evidence.observed_at == 10.


def test_scope_changes_keep_history_without_new_authority(tmp_path: Path) -> None:
    from card_screen import field_is_current
    store = setup_store(tmp_path)
    item = CardItem(card_id='cards.damage', ownership='owned', level=3,
                    observed_at=10., evidence_ref='frame')
    store.observe(snapshot(items=(item,)))
    newer = replace(SCOPE, generation='generation2', epoch=2)
    CurrencyRepository(store.path).bind_scope(newer)
    assert not store.observe(snapshot(observed_at=30.))
    assert store.observe(snapshot(scope=newer, observed_at=20., items=(), equipped=None,
                                  equipment_complete=False))
    result = store.snapshot()
    assert result.items[0].level == 3
    assert not field_is_current(result.items[0], 'level', scope=newer, visit_id='visit')
    with pytest.raises(ValueError):
        store.submit(command(idempotency_key='stale'), now=20.)
    with pytest.raises(ValueError):
        store.observe(snapshot(scope=replace(newer, account_id='other')))


def test_unbound_database_cannot_accept_card_writes(tmp_path: Path) -> None:
    from card_store import CardStore
    store = CardStore(tmp_path / 'unbound.db')
    with pytest.raises(ValueError):
        store.observe(snapshot())
    with pytest.raises(ValueError):
        store.submit(command(), now=10.)


def test_idempotency_pins_payload_across_reopen_and_generation(tmp_path: Path) -> None:
    from card_store import CardStore
    store = setup_store(tmp_path)
    original = store.submit(command(), now=10.)
    assert CardStore(store.path).submit(command(), now=20.) == original
    with pytest.raises(ValueError, match='idempotency'):
        store.submit(command(program_revision='p2'), now=20.)
    newer = replace(SCOPE, generation='generation2', epoch=2)
    CurrencyRepository(store.path).bind_scope(newer)
    assert store.submit(command(), now=30.) == original
    with pytest.raises(ValueError, match='idempotency'):
        store.submit(command(scope=newer), now=30.)
    assert store.pending(newer) == ()
    assert store.operation(original.operation_id).command.scope == SCOPE


def test_concurrent_claim_has_one_winner_and_terminal_is_final(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    operation = store.submit(command(), now=10.)
    def claim(_: int) -> str:
        try:
            return store.transition(operation.operation_id, expected_status='queued',
                                    status='preflight', reason=None, now=11.).status
        except ValueError:
            return 'lost'
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(claim, range(2))) == ['lost', 'preflight']
    store.transition(operation.operation_id, expected_status='preflight', status='confirmed', reason=None, now=12.)
    with pytest.raises(ValueError):
        store.transition(operation.operation_id, expected_status='confirmed', status='queued', reason=None, now=13.)


def test_old_scope_cannot_be_claimed(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    operation = store.submit(command(), now=10.)
    CurrencyRepository(store.path).bind_scope(replace(SCOPE, epoch=2))
    with pytest.raises(ValueError, match='scope'):
        store.transition(operation.operation_id, expected_status='queued', status='preflight', reason=None, now=11.)


def test_cycle_is_immutable_and_unresolved_paid_work_blocks_new_cycle(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    assert store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=100, now=10.).cap == 100
    with pytest.raises(ValueError):
        store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=200, now=11.)
    operation = store.submit(command(kind='buy', budget_cycle_id='cycle'), now=11.)
    with pytest.raises(ValueError, match='unresolved'):
        store.start_budget_cycle(scope=SCOPE, cycle_id='next', cap=100, now=12.)
    store.transition(operation.operation_id, expected_status='queued', status='canceled', reason='cancel', now=13.)
    newer = replace(SCOPE, generation='generation2', epoch=2)
    CurrencyRepository(store.path).bind_scope(newer)
    assert store.start_budget_cycle(scope=newer, cycle_id='cycle', cap=100, now=14.).cap == 100
    assert store.budget('cycle').spent == 0
    assert store.start_budget_cycle(scope=newer, cycle_id='next', cap=40, now=15.).cap == 40
    with pytest.raises(ValueError):
        store.submit(command(scope=newer, kind='buy', budget_cycle_id='cycle', idempotency_key='old'), now=16.)


def test_budget_links_journal_and_derives_spend_without_duplicate_total(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=100, now=10.)
    operation = store.submit(command(kind='buy', budget_cycle_id='cycle'), now=11.)
    store.transition(operation.operation_id, expected_status='queued', status='preflight', reason=None, now=12.)
    with db.connect(store.path) as conn:
        conn.execute("INSERT INTO transactions(key,ts,stage,item,category,currency,price,detail) VALUES (?,?,?,?,?,?,?,?)",
                     ('txn', 12., 'intended', 'card', 'CARDS', 'gems', 20, json.dumps({'scope': command().model_dump(mode='json')['scope']})))
        conn.execute("INSERT INTO currency_commitments VALUES ('purchase:txn','gems',20)")
        store.link_transaction(operation.operation_id, transaction_key='txn', action_sequence=1, now=12., conn=conn)
    assert store.budget('cycle').pending == 20
    with db.connect(store.path) as conn:
        conn.execute("UPDATE transactions SET stage='resolved', outcome='bought', spent=20 WHERE key='txn'")
        conn.execute("DELETE FROM currency_commitments")
    assert store.operation(operation.operation_id).spent_gems == 20
    assert store.budget('cycle').spent == 20
    assert store.budget('cycle').pending == 0
    with pytest.raises(ValueError, match='unresolved'):
        store.start_budget_cycle(scope=SCOPE, cycle_id='next', cap=100, now=20.)
    assert store.budget('cycle').cap == 100


def test_additive_schema_preserves_existing_history(tmp_path: Path) -> None:
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE account_identity (id INTEGER PRIMARY KEY, account_id TEXT NOT NULL)')
        conn.execute("INSERT INTO account_identity VALUES (1,'a')")
    with db.connect(path) as conn:
        assert conn.execute('SELECT account_id FROM account_identity').fetchone()[0] == 'a'
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'card_observations','card_operations','card_budget_cycles'} <= tables


def test_unknown_journal_spend_holds_quote_after_commitment_loss(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=100, now=10.)
    op = store.submit(command(kind='buy', budget_cycle_id='cycle'), now=11.)
    store.transition(op.operation_id, expected_status='queued', status='preflight', reason=None, now=12.)
    with db.connect(store.path) as conn:
        conn.execute('INSERT INTO transactions(key,ts,stage,item,category,currency,price,outcome,detail) VALUES (?,?,?,?,?,?,?,?,?)',
                     ('unknown', 12., 'resolved', 'card', 'CARDS', 'gems', 20, 'unproven',
                      json.dumps({'scope': command().model_dump(mode='json')['scope']})))
        store.link_transaction(op.operation_id, transaction_key='unknown', action_sequence=1, now=12., conn=conn)
    assert store.operation(op.operation_id).spent_gems is None
    assert store.budget('cycle').pending == 20
    with pytest.raises(ValueError, match='unresolved'):
        store.transition(op.operation_id, expected_status='preflight', status='canceled', reason='cannot know', now=13.)


def test_reward_evidence_is_durable_idempotent_and_has_no_spending_setter(tmp_path: Path) -> None:
    from card_models import RewardItem
    from card_store import CardStore
    store = setup_store(tmp_path)
    op = store.submit(command(), now=10.)
    rewards = (RewardItem(position=0, card_id='cards.damage', quantity=1),
               RewardItem(position=1, card_id='cards.damage', quantity=1))
    store.record_evidence(op.operation_id, expected_status='queued', now=11., rewards=rewards,
                          snapshot_before=snapshot())
    store.record_evidence(op.operation_id, expected_status='queued', now=12., rewards=rewards)
    result = CardStore(store.path).operation(op.operation_id)
    assert result.rewards == rewards
    assert result.snapshot_before == snapshot()
    assert result.spent_gems is None
    with pytest.raises(ValueError, match='conflicting'):
        store.record_evidence(op.operation_id, expected_status='queued', now=13.,
                              rewards=(RewardItem(position=0, card_id='cards.health', quantity=1),))


def test_link_rolls_back_journal_and_commitment_when_cycle_cap_exceeded(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=10, now=10.)
    op = store.submit(command(kind='buy', budget_cycle_id='cycle'), now=11.)
    store.transition(op.operation_id, expected_status='queued', status='preflight', reason=None, now=12.)
    with pytest.raises(ValueError):
        with db.connect(store.path) as conn:
            conn.execute('INSERT INTO transactions(key,ts,stage,item,category,currency,price,detail) VALUES (?,?,?,?,?,?,?,?)',
                         ('expensive', 12., 'intended', 'card', 'CARDS', 'gems', 20,
                          json.dumps({'scope': command().model_dump(mode='json')['scope']})))
            conn.execute("INSERT INTO currency_commitments VALUES ('purchase:expensive','gems',20)")
            store.link_transaction(op.operation_id, transaction_key='expensive', action_sequence=1, now=12., conn=conn)
    assert store.operation(op.operation_id).transaction_key is None
    with db.reader(store.path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM currency_commitments').fetchone()[0] == 0


def test_paid_dispatch_cannot_skip_journal_and_budget(tmp_path: Path) -> None:
    store = setup_store(tmp_path)
    store.start_budget_cycle(scope=SCOPE, cycle_id='cycle', cap=20, now=10.)
    op = store.submit(command(kind='buy', budget_cycle_id='cycle'), now=11.)
    store.transition(op.operation_id, expected_status='queued', status='preflight', reason=None, now=12.)
    with pytest.raises(ValueError, match='linked'):
        store.transition(op.operation_id, expected_status='preflight', status='dispatched', reason=None, now=13.)
    with pytest.raises(ValueError, match='proof'):
        store.transition(op.operation_id, expected_status='preflight', status='confirmed', reason=None, now=13.)


def test_account_state_card_hook_requires_verified_capture_and_refreshes_revision(tmp_path: Path) -> None:
    from account_state import AccountRepository, AccountState
    from fleet.identity import IdentityEvidence
    store = setup_store(tmp_path)
    state = AccountState(AccountRepository(store.path))
    assert not state.observe_cards(snapshot())
    state.bind_scope(SCOPE, identity=IdentityEvidence('a', 5., 'identity'))
    assert not state.observe_cards(snapshot(observed_at=4.))
    assert state.observe_cards(snapshot())
    assert state.snapshot()['revision']['cards_equipped'] == ['cards.damage']
    state.invalidate_scope('manual')
    assert not state.observe_cards(snapshot(observed_at=20.))


def test_cancel_submission_persists_target_intent_atomically_before_runtime_handles_it(tmp_path: Path) -> None:
    from card_store import CardStore
    store = setup_store(tmp_path)
    target = store.submit(command(), now=1.)
    cancel = command(kind='cancel', idempotency_key='cancel', target_operation_id=target.operation_id)
    accepted = store.submit(cancel, now=2.)
    reopened = CardStore(store.path)
    assert reopened.operation(accepted.operation_id).status == 'queued'
    assert reopened.operation(target.operation_id).cancel_requested
    assert reopened.operation(target.operation_id).command == target.command
    assert reopened.submit(cancel, now=3.).operation_id == accepted.operation_id
    assert reopened.operation(target.operation_id).updated_at == 2.
    with pytest.raises(KeyError):
        store.submit(command(kind='cancel', idempotency_key='invalid', target_operation_id='absent'), now=4.)
    assert len(store.recent_operations()) == 2
