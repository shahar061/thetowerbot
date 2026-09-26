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
