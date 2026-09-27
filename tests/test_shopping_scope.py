from unittest.mock import Mock
import time

import pytest

from account_state import AccountRepository, AccountState
from currencies import CommitmentError
from evidence_scope import FactScope
from fleet.identity import IdentityEvidence
from shopping import ShoppingSession
from transactions import TransactionJournal


def session(tmp_path):
    journal = TransactionJournal(tmp_path / 'bot.db')
    sut = ShoppingSession(Mock(), Mock(), Mock(), journal=journal)
    sut.account_state = AccountState(AccountRepository(journal.path))
    return sut


def test_unbound_purchase_is_rejected_before_recording_or_tapping(tmp_path):
    sut = session(tmp_path)
    with pytest.raises(CommitmentError):
        sut._open_intent(item='Damage', category='ATTACK', currency='coins', price=10,
                         wallet_before=20, armed=True)
    assert sut.journal.open_transactions() == ()


def test_scoped_shopping_prepares_atomically_using_rounded_lower_bound(tmp_path, monkeypatch):
    sut = session(tmp_path)
    scope = FactScope('acct', 'lease', 'g', 0)
    sut.account_state.bind_scope(scope, identity=IdentityEvidence('acct', time.time(), 'identity'))
    monkeypatch.setattr(sut.currencies, 'reserve', lambda *a, **k: pytest.fail('split reservation'))
    monkeypatch.setattr(sut.journal, 'open', lambda *a, **k: pytest.fail('split intent'))
    kwargs = dict(item='Damage', category='ATTACK', currency='coins', price=999,
                  wallet_before=1000, armed=True, before={'observed_at':time.time(),'frame_digest':'frame'})
    with pytest.raises(CommitmentError):
        sut._open_intent(**kwargs)
    txn = sut._open_intent(**{**kwargs,'price':10})
    assert txn.scope == scope
    assert sut.currencies.committed('coins') == 10


def test_armed_no_journal_is_unavailable_but_rehearsal_remains_read_only():
    sut = ShoppingSession(Mock(), Mock(), Mock())
    kwargs = dict(item='Damage', category='ATTACK', currency='coins', price=10, wallet_before=20)
    with pytest.raises(CommitmentError, match='transaction journal unavailable'):
        sut._open_intent(**kwargs,armed=True)
    assert sut._open_intent(**kwargs,armed=False) is None


def test_optional_history_does_not_disable_durable_safety_storage(tmp_path):
    sut = session(tmp_path)
    sut.account_state = AccountState()  # --no-store: event/account history disabled.
    sut.account_state.attach_safety_storage(sut.journal.path)
    scope = FactScope('acct','lease','generation',0)
    sut.account_state.bind_scope(scope,identity=IdentityEvidence('acct',time.time(),'identity'))
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=10,wallet_before=20,
        armed=True,before={'observed_at':time.time(),'frame_digest':'frame'})
    assert sut.account_state.repository is None
    assert sut.currencies.committed('coins') == 10
    assert TransactionJournal(sut.journal.path).open_transactions()[0].key == txn.key


def test_live_settlement_is_not_restored_again_but_survives_restart(tmp_path):
    sut = session(tmp_path)
    scope = FactScope('acct','lease','generation',0)
    sut.account_state.bind_scope(scope,identity=IdentityEvidence('acct',time.time(),'identity'))
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=10,
        wallet_before=20,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    outcome = sut._close(txn.key,price=10,wallet_before=20,wallet_after=10,
        effect_changed=True,evidence_ref='after',observed_at=time.time())
    assert outcome.spent == 10
    sut._spent, sut._coin_spent, sut._bought = 10, 10, 1
    assert not sut._restore_recovered_visit()
    assert sut._bought == 1
    sut._bus.publish.assert_not_called()

    restarted = session(tmp_path)
    assert restarted._restore_recovered_visit()
    assert restarted._bought == 1
    assert restarted._spent == 10
    assert not restarted._restore_recovered_visit()
    assert restarted._bus.publish.call_count == 1


def test_reset_closes_handled_receipts_without_replaying_next_visit(tmp_path):
    sut = session(tmp_path)
    scope = FactScope('acct','lease','generation',0)
    sut.account_state.bind_scope(scope,identity=IdentityEvidence('acct',time.time(),'identity'))
    txn = sut._open_intent(item='Damage',category='ATTACK',currency='coins',price=10,
        wallet_before=20,armed=True,before={'observed_at':time.time(),'frame_digest':'before'})
    sut._mark_acted(txn)
    sut._close(txn.key,price=10,wallet_before=20,wallet_after=10,
        effect_changed=True,evidence_ref='after',observed_at=time.time())
    sut.reset()
    assert sut.journal.recovered_visit() == ()
