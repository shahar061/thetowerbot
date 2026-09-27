"""The transaction journal - what the bot meant to do, and what it proved.

These tests exist because of one window in the buy path: between the tap
that spends coins and the `Purchased` event that records it, nothing is
written to disk. A process that dies in that window has spent the money and
kept no record of it, so the next start is free to spend it again.

Every test here drives real sqlite on a real file, because the whole point
of the module is what survives the process that wrote it.
"""

from __future__ import annotations

import pytest

import transactions
import db
import dataclasses
import ledger
import events


def _recovery(**overrides: object) -> transactions.RecoveryEvidence:
    return transactions.RecoveryEvidence(**{
        "category": "ATTACK", "currency": "coins", "wallet_after": 3,
        "effect_changed": True, "observed_at": 3., "frame_digest": "fresh",
        **overrides,
    })


def test_restart_resolution_and_ledger_are_durable_and_idempotent(tmp_path) -> None:
    path = tmp_path / "bot.db"
    journal = transactions.TransactionJournal(path)
    txn = journal.open(_intent())
    journal.record_action(txn.key, at=2.)
    reopened = transactions.TransactionJournal(path)
    outcome = reopened.reconcile(txn.key, _recovery(), now=3.)
    assert (outcome.verdict, outcome.spent) == (transactions.Verdict.BOUGHT, 10)
    assert reopened.open_transactions() == ()
    assert transactions.TransactionJournal(path).reconcile(txn.key, _recovery(), now=4.) == outcome
    with db.reader(path) as conn:
        rows = conn.execute("SELECT delta, balance_after FROM ledger").fetchall()
    assert [tuple(row) for row in rows] == [(-10, 3)]


@pytest.mark.parametrize("overrides", [
    {"wallet_after": None}, {"wallet_after": 4}, {"wallet_after": 13},
    {"effect_changed": None}, {"effect_changed": False},
    {"category": "DEFENSE"}, {"currency": "gems"},
    {"observed_at": 1.}, {"observed_at": 4.}, {"observed_at": -40.},
    {"frame_digest": ""},
])
def test_restart_ambiguity_stays_blocked_on_disk(tmp_path, overrides: dict) -> None:
    path = tmp_path / "bot.db"
    journal = transactions.TransactionJournal(path)
    txn = journal.open(_intent())
    journal.record_action(txn.key, at=2.)
    outcome = journal.reconcile(txn.key, _recovery(**overrides), now=3.)
    assert outcome.verdict == transactions.Verdict.UNPROVEN
    assert outcome.spent is None
    reopened = transactions.TransactionJournal(path)
    assert reopened.open_transactions()[0].stage == transactions.Stage.ACTED
    with pytest.raises(transactions.TransactionInFlight):
        reopened.open(_intent(ts=4.))
    with db.reader(path) as conn:
        assert conn.execute("SELECT outcome, spent FROM transactions").fetchone()[:] == ("unproven", None)
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_restart_ledger_failure_rolls_back_resolution(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bot.db"
    journal = transactions.TransactionJournal(path)
    txn = journal.open(_intent())
    journal.record_action(txn.key, at=2.)
    insert = db.insert_ledger

    def fail_after_insert(*args: object, **kwargs: object) -> None:
        insert(*args, **kwargs)
        raise RuntimeError("crash while reconciling")

    monkeypatch.setattr(db, "insert_ledger", fail_after_insert)
    with pytest.raises(RuntimeError, match="crash while reconciling"):
        journal.reconcile(txn.key, _recovery(), now=3.)
    assert transactions.TransactionJournal(path).open_transactions()[0].stage == transactions.Stage.ACTED
    with db.reader(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone()[0] == 0


def test_existing_ledger_writer_refreshes_even_if_recovery_notification_is_lost(tmp_path) -> None:
    path = tmp_path / "bot.db"
    journal = transactions.TransactionJournal(path)
    conn = db.connect(path)
    writer = ledger.LedgerWriter(conn)
    for line in writer.lines_for(events.PurchaseSkipped(item="Damage", reason="test", coins_before=13)):
        db.insert_ledger(conn, dataclasses.replace(line, seq=None).as_row())
    txn = journal.open(_intent())
    journal.record_action(txn.key, at=2.)
    outcome = journal.reconcile(txn.key, _recovery(), now=3.)
    # No notification was delivered; a later event must use the durable balance.
    lines = writer.lines_for(events.PurchaseSkipped(item="Damage", reason="test", coins_before=3))
    assert [line.kind for line in lines] == ["BUY_SKIPPED"]
    assert lines[0].balance_after == 3
    assert writer.lines_for(journal.recovery_event(txn, outcome)) == []
    conn.close()


def _intent(**overrides) -> transactions.Intent:
    """A readable, affordable workshop row - the uninteresting case."""
    fields = {
        "item": "Damage",
        "category": "ATTACK",
        "currency": "coins",
        "price": 10,
        "wallet_before": 13,
        "evidence": frozenset({"damage", "attack_speed"}),
        "ts": 1.0,
    }
    fields.update(overrides)
    return transactions.Intent(**fields)


def test_an_intent_survives_the_process_that_recorded_it(tmp_path) -> None:
    """The crash case. A tap was sent and the process died before anything
    else. The record of that tap has to outlive it."""
    path = tmp_path / "bot.db"
    journal = transactions.TransactionJournal(path)

    txn = journal.open(_intent())
    journal.record_action(txn.key)
    del journal  # nothing orderly happens on the way out of a crash

    reopened = transactions.TransactionJournal(path)
    still_open = reopened.open_transactions()

    assert [t.item for t in still_open] == ["Damage"]
    assert still_open[0].stage == transactions.Stage.ACTED


def test_a_tap_that_crashed_before_confirmation_is_not_tapped_again(tmp_path) -> None:
    """The acceptance gate. The money may already be gone; a second tap
    would spend it twice. The next scan is refused the attempt outright."""
    path = tmp_path / "bot.db"
    first = transactions.TransactionJournal(path)
    txn = first.open(_intent())
    first.record_action(txn.key, at=1.0)

    restarted = transactions.TransactionJournal(path)

    with pytest.raises(transactions.TransactionInFlight):
        restarted.open(_intent(ts=2.0, wallet_before=3))


def test_a_transaction_owns_exactly_one_device_action(tmp_path) -> None:
    """One step, one tap. A retried tap inside one transaction would spend
    twice against a single record and reconcile to the wrong number."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent())
    journal.record_action(txn.key, at=1.0)

    with pytest.raises(transactions.ActionAlreadyTaken):
        journal.record_action(txn.key, at=1.5)


def test_a_free_upgrade_confirms_the_effect_but_never_a_spend(tmp_path) -> None:
    """The acceptance gate. The upgrade really was applied - the row
    changed - but nothing left the wallet, and the history must not claim
    it did."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(price=0, wallet_before=13))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(
        txn.key, wallet_after=13, effect_changed=True, ts=2.0
    )

    assert outcome.verdict == transactions.Verdict.FREE
    assert outcome.spent == 0


def test_a_purchase_is_bought_when_the_wallet_fell_by_what_it_cost(tmp_path) -> None:
    """The ordinary case, and the contrast that gives the free case its
    meaning: a debit of exactly the price, alongside a changed row."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(price=10, wallet_before=13))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(
        txn.key, wallet_after=3, effect_changed=True, ts=2.0
    )

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent == 10


def test_income_arriving_mid_purchase_leaves_the_amount_unknown(tmp_path) -> None:
    """Coins keep arriving while a purchase is in flight. The row changed,
    so something was bought - but the wallet no longer proves how much, and
    reporting the predicted price here would invent a number."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(price=10, wallet_before=13))
    journal.record_action(txn.key, at=1.0)

    # 10 spent and 5 earned in between: a drop of 5, not of 10.
    outcome = journal.resolve(
        txn.key, wallet_after=8, effect_changed=True, ts=2.0
    )

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent is None


def test_a_tap_that_changed_nothing_is_refuted_rather_than_left_open(tmp_path) -> None:
    """A swallowed tap. The row did not change and the wallet did not move,
    which together prove the money is still there - the one case where
    trying again is safe."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(price=10, wallet_before=13))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(
        txn.key, wallet_after=13, effect_changed=False, ts=2.0
    )

    assert outcome.verdict == transactions.Verdict.REFUTED
    assert outcome.spent == 0


def test_a_zero_price_read_is_not_free_when_the_wallet_fell(tmp_path) -> None:
    """0 is a claim that nothing was spent, and a falling wallet refutes it.
    A misread price must not certify a paid upgrade as free."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(price=0, wallet_before=13))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(
        txn.key, wallet_after=3, effect_changed=True, ts=2.0
    )

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent is None


def test_a_zero_price_read_is_not_free_without_a_wallet_reading() -> None:
    """Nothing read after the action, so nothing proves the read 0."""
    outcome = transactions.judge(
        "k", price=0, wallet_before=13, wallet_after=None, effect_changed=True,
    )

    assert outcome.verdict == transactions.Verdict.UNPROVEN
    assert outcome.spent is None


def test_a_changed_item_with_an_unmoved_wallet_is_not_a_debit_of_its_price() -> None:
    """The row moved on but the coins did not fall: a price was read, and
    nothing proves it was paid. Unknown, not the read price."""
    outcome = transactions.judge(
        "k", price=10, wallet_before=13, wallet_after=13, effect_changed=True,
    )

    assert outcome.verdict == transactions.Verdict.UNPROVEN
    assert outcome.spent is None


@pytest.mark.parametrize(("before", "price", "after"), [
    (2610, 2470, 141),  # "2.61K" was a coin over 2610
    (3080, 2230, 849),  # "3.08K" was a coin under 3080
])
def test_a_drop_within_the_header_abbreviation_proves_the_price(
    before: int, price: int, after: int,
) -> None:
    """Over 1000 the header shows "2.61K", which parses to exactly 2610 but
    hides the last digit. A drop that misses the price by no more than that
    hidden digit is the price; a price misread by a digit misses by far more."""
    outcome = transactions.judge(
        "k", price=price, wallet_before=before, wallet_after=after, effect_changed=True,
    )

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent == price


def test_a_drop_far_from_the_price_still_leaves_the_amount_unknown() -> None:
    outcome = transactions.judge(
        "k", price=1000, wallet_before=2000, wallet_after=1100, effect_changed=True,
    )

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent is None


def test_a_price_the_catalog_lists_for_the_item_proves_the_price(tmp_path) -> None:
    """Thorns costs 961 at one level in the attributed catalog. The row
    changed and coins left, so that level was bought - however much income
    the run paid into the wallet while the purchase was in flight."""
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(item="Thorns", category="DEFENSE", price=961,
                               wallet_before=1080))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(txn.key, wallet_after=500, effect_changed=True, ts=2.0)

    assert outcome.verdict == transactions.Verdict.BOUGHT
    assert outcome.spent == 961


def test_a_price_the_catalog_does_not_list_is_not_proven_by_it(tmp_path) -> None:
    journal = transactions.TransactionJournal(tmp_path / "bot.db")
    txn = journal.open(_intent(item="Thorns", category="DEFENSE", price=960,
                               wallet_before=1080))
    journal.record_action(txn.key, at=1.0)

    outcome = journal.resolve(txn.key, wallet_after=500, effect_changed=True, ts=2.0)

    assert outcome.spent is None


def test_exact_readings_leave_no_room_for_a_near_miss() -> None:
    """Both readings under 1000 are shown in full, so a drop a few coins off
    the price is not rounding - the amount stays unknown."""
    outcome = transactions.judge(
        "k", price=700, wallet_before=900, wallet_after=205, effect_changed=True,
    )

    assert outcome.spent is None


def test_reading_tolerance_adds_each_readings_abbreviation_and_each_rounded_amount() -> None:
    assert transactions.reading_tolerance(450, 451) == 0
    assert transactions.reading_tolerance(2610, 2614) == 20
    assert transactions.reading_tolerance(450, 451, rounded_amounts=2) == 2
    assert transactions.reading_tolerance(12300, rounded_amounts=1) == 101


def _scoped(journal, *, wallet=13, lower=None):
    from evidence_scope import FactScope, BalanceInterval
    from currencies import CurrencyRepository
    scope = FactScope('acct', 'lease', 'generation', 0)
    CurrencyRepository(journal.path).bind_scope(scope)
    balance = BalanceInterval('coins', wallet if lower is None else lower, wallet,
                              scope, 1., 'frame')
    return scope, balance


def test_prepare_is_atomic_and_duplicate_safe(tmp_path):
    from currencies import CurrencyRepository
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope, balance = _scoped(journal)
    txn = journal.prepare(_intent(), scope=scope, balance=balance)
    assert txn.scope == scope
    assert journal.prepare(_intent(), scope=scope, balance=balance).key == txn.key
    assert CurrencyRepository(journal.path).committed('coins') == 10
    with pytest.raises(transactions.TransactionInFlight):
        transactions.TransactionJournal(journal.path).prepare(_intent(ts=2.), scope=scope, balance=balance)


def test_prepare_conservative_bound_shared_reserves_and_wrong_scope(tmp_path):
    from currencies import CurrencyRepository
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope, balance = _scoped(journal, wallet=1000, lower=990)
    assert journal.prepare(_intent(price=995), scope=scope, balance=balance) is None
    assert journal.prepare(_intent(), scope=dataclasses.replace(scope, epoch=1), balance=balance) is None
    repo = CurrencyRepository(journal.path)
    assert repo.reserve('lab:1', 'coins', 985, wallet=1000)
    assert journal.prepare(_intent(), scope=scope, balance=balance) is None
    assert not journal.open_transactions()


def test_failed_prepare_cannot_leave_reservation(tmp_path):
    from currencies import CurrencyRepository
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope, balance = _scoped(journal)
    with journal._connect() as conn:
        conn.execute("CREATE TRIGGER crash_intent BEFORE INSERT ON transactions BEGIN SELECT RAISE(ABORT, 'crash'); END")
    with pytest.raises(Exception, match='crash'):
        journal.prepare(_intent(), scope=scope, balance=balance)
    assert CurrencyRepository(journal.path).committed('coins') == 0
    assert not journal.open_transactions()


def test_scoped_uncertainty_retains_original_intent_and_reservation(tmp_path):
    from currencies import CurrencyRepository
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope, balance = _scoped(journal)
    txn = journal.prepare(_intent(), scope=scope, balance=balance)
    journal.record_action(txn.key, at=2.)
    outcome = journal.resolve(txn.key, wallet_after=None, effect_changed=None, ts=3., scope=scope)
    assert outcome.spent is None
    assert journal.open_transactions()[0].scope == scope
    journal.close_unproven(txn.key, reason='timeout', now=4.)
    assert journal.open_transactions()[0].stage == transactions.Stage.ACTED
    assert CurrencyRepository(journal.path).committed('coins') == 10
    assert journal.reconcile(txn.key, _recovery(scope=dataclasses.replace(scope, epoch=1)), now=3.).spent is None
    assert journal.reconcile(txn.key, _recovery(scope=scope), now=3.).spent == 10
    assert CurrencyRepository(journal.path).committed('coins') == 0


def test_prepare_rejects_stale_catalog_and_unknown_bound(tmp_path):
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope, balance = _scoped(journal)
    assert journal.prepare(_intent(), scope=scope, balance=dataclasses.replace(balance, lower=None)) is None
    intent = _intent(before={'catalog_revision': 'old', 'modifier_revision': 'm1'})
    assert journal.prepare(intent, scope=scope, balance=balance) is None


def test_full_generation_restart_reconciles_without_replay_and_ingests_receipt_once(tmp_path):
    import json
    from account_state import AccountState, AccountRepository
    from evidence_scope import FactScope, BalanceInterval
    from fleet.identity import IdentityEvidence
    from fleet.input_lease import InputLease
    from mission_receipts import ingest_receipts
    from notification_state import NotificationState
    root = tmp_path / 'worker'
    root.mkdir()
    path = root / 'bot.db'
    db.bind_account(path,'acct')
    first = FactScope('acct','lease','a' * 32,2)
    second = dataclasses.replace(first,generation='b' * 32)
    (root / 'checkpoints').mkdir()
    binding = dict(worker_id='worker',account_id='acct',lease_id='lease',attempt_id='attempt',
                   endpoint='endpoint',created_at=.1,observed_at=.5,evidence_ref='original-id')
    for scope, predecessor in ((first,None),(second,first.generation)):
        (root / 'checkpoints' / f'{scope.generation}.json').write_text(json.dumps({**binding,
            'generation':scope.generation,'predecessor_generation':predecessor}))
    (root / 'fleet-registration.json').write_text(json.dumps(dict(account_id='acct',job_id='attempt',
        state='registered',instance='worker',endpoint='endpoint',lease_id='lease',
        binding=str(root / 'checkpoints' / f'{second.generation}.json'))))
    old = AccountState(AccountRepository(path))
    old.bind_scope(first,identity=IdentityEvidence('acct',1.,'id-a'),runtime_root=root)
    journal = transactions.TransactionJournal(path)
    txn = journal.prepare(_intent(),scope=first,balance=BalanceInterval('coins',13,13,first,1.,'frame'))
    journal.record_action(txn.key,at=2.)
    notification = NotificationState(root / 'mission-notification-state-archive-a.json',scope={
        'account_id':'acct','lease_id':'lease','generation':first.generation,'attempt_id':'attempt','fact_epoch':2})
    notification.begin('missions',2.)
    notification.prepare_claim(mission='Damage',mission_id='d',coins=0,gems=2,
        completed_before=1,completed_target=5,visible_before=[['d','Damage']],now=2.)
    notification.record_receipt(events.MissionClaimed(mission='Damage',mission_id='d',coins=0,gems=2,
        completed_before=1,completed_after=2),3.)
    InputLease(root / 'input-lease.json').grant(second.generation)
    restarted = AccountState(AccountRepository(path))
    assert restarted.persisted_epoch == 2
    restarted.bind_scope(second,identity=IdentityEvidence('acct',4.,'id-b'),runtime_root=root)
    proof = restarted.continuity(first,now=5.)
    assert proof is not None
    new_balance = BalanceInterval('coins',3,3,second,5.,'frame-after')
    with pytest.raises(transactions.TransactionInFlight):
        journal.prepare(_intent(ts=5.,price=1,wallet_before=3),scope=second,balance=new_balance)
    evidence = _recovery(scope=second,continuity=proof,observed_at=5.)
    outcome = journal.reconcile(txn.key,evidence,now=5.)
    assert outcome.spent == 10
    assert journal._require(txn.key).scope == first
    assert journal.reconcile(txn.key,evidence,now=6.) == outcome
    assert journal.currencies.committed('coins') == 0
    # Old confirmed reward remains importable after explicit invalidation and hours of delay.
    restarted.invalidate_scope('manual_play')
    assert restarted.verified_scope is None
    assert restarted.continuity(first,now=1000.) is None
    keys = ingest_receipts(root,restarted,now=1000.)
    assert len(keys) == 1
    assert ingest_receipts(root,restarted,now=2000.) == keys
    with db.reader(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ledger WHERE kind='WORKSHOP_BUY'").fetchone()[0] == 1
        rows = conn.execute("SELECT currency,delta,balance_after FROM ledger WHERE kind='MISSION_CLAIM'").fetchall()
        assert [tuple(r) for r in rows] == [('coins',0,None),('gems',2,None)]


def test_multiple_connections_cannot_prepare_two_spends_or_release_unknown_reserve(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from currencies import CommitmentError
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope,balance = _scoped(journal,wallet=100)
    other = transactions.TransactionJournal(journal.path)
    def prepare(args):
        owner, stamp = args
        try:
            return owner.prepare(_intent(ts=stamp),scope=scope,balance=balance)
        except transactions.TransactionInFlight:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(prepare,((journal,1.),(other,2.))))
    assert sum(r is not None for r in rows) == 1
    txn = next(r for r in rows if r is not None)
    with pytest.raises(CommitmentError):
        journal.currencies.release(f'purchase:{txn.key}','coins')
    assert other.currencies.committed('coins') == 10


def test_scoped_prepared_but_undispatched_restart_can_refute_without_replay(tmp_path):
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope,balance = _scoped(journal)
    txn = journal.prepare(_intent(),scope=scope,balance=balance)
    evidence = _recovery(scope=scope,effect_changed=False,wallet_after=13)
    outcome = journal.reconcile(txn.key,evidence,now=3.)
    assert outcome.verdict == transactions.Verdict.REFUTED
    assert outcome.spent == 0
    assert journal.currencies.committed('coins') == 0
    assert not journal.open_transactions()


def test_original_wallet_cannot_be_reused_after_confirmed_spend(tmp_path):
    journal = transactions.TransactionJournal(tmp_path / 'bot.db')
    scope,balance = _scoped(journal)
    txn = journal.prepare(_intent(),scope=scope,balance=balance)
    journal.record_action(txn.key,at=2.)
    assert journal.reconcile(txn.key,_recovery(scope=scope),now=3.).spent == 10
    assert journal.prepare(_intent(ts=4.),scope=scope,balance=balance) is None
