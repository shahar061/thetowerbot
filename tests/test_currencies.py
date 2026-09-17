"""Durable commitments shared by competing currency plans."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from currencies import CommitmentError, CurrencyRepository


def test_competing_plans_cannot_reserve_the_same_gems(tmp_path) -> None:
    path = tmp_path / "bot.db"
    first = CurrencyRepository(path)
    second = CurrencyRepository(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda request: request[0].reserve(
            request[1], "gems", 80, wallet=100),
            ((first, "lab-slot"), (second, "cards"))))
    assert sorted(results) == [False, True]
    assert CurrencyRepository(path).committed("gems") == 80


def test_commitments_survive_restart_and_protect_lower_priority_spending(tmp_path) -> None:
    path = tmp_path / "bot.db"
    repository = CurrencyRepository(path)
    assert repository.reserve("lab-slot", "gems", 700, wallet=1000)
    assert repository.reserve("uw-sync", "stones", 180, wallet=200)
    restarted = CurrencyRepository(path)
    assert not restarted.can_spend("gems", 301, wallet=1000)
    assert restarted.can_spend("gems", 300, wallet=1000)
    assert not restarted.can_spend("stones", 21, wallet=200)
    assert restarted.can_spend("coins", 100, wallet=100)


def test_retry_is_idempotent_and_release_is_explicit(tmp_path) -> None:
    repository = CurrencyRepository(tmp_path / "bot.db")
    assert repository.reserve("lab-slot", "gems", 700, wallet=1000)
    assert repository.reserve("lab-slot", "gems", 700, wallet=1000)
    assert repository.committed("gems") == 700
    assert not repository.reserve("lab-slot", "gems", 1100, wallet=1000)
    repository.release("lab-slot", "gems")
    assert repository.committed("gems") == 0


@pytest.mark.parametrize("wallet", [None, -1, True])
def test_unavailable_or_ambiguous_wallet_refuses_reservation_and_spend(tmp_path, wallet) -> None:
    repository = CurrencyRepository(tmp_path / "bot.db")
    assert not repository.reserve("lab-slot", "gems", 10, wallet=wallet)
    assert not repository.can_spend("gems", 10, wallet=wallet)
    assert repository.committed("gems") == 0


def test_currency_identity_and_amounts_are_validated(tmp_path) -> None:
    repository = CurrencyRepository(tmp_path / "bot.db")
    with pytest.raises(CommitmentError):
        repository.reserve("plan", "unknown", 5, wallet=100)
    with pytest.raises(CommitmentError):
        repository.reserve("plan", "cash", -1, wallet=100)
    assert repository.reserve("run", "cash", 5, wallet=100)
    assert repository.committed("coins") == 0
    assert repository.reserve("event-plan", "resource:event_token@2026.09", 4, wallet=10)
    assert repository.committed("resource:event_token@2026.09") == 4
    assert repository.committed("resource:event_token@2026.10") == 0


def test_shopping_intent_respects_a_saved_plan_before_any_action(tmp_path) -> None:
    from shopping import ShoppingSession
    from transactions import TransactionJournal

    path = tmp_path / "bot.db"
    plans = CurrencyRepository(path)
    assert plans.reserve("lab-slot", "gems", 90, wallet=100)
    session = ShoppingSession(None, None, None, journal=TransactionJournal(path))
    with pytest.raises(CommitmentError):
        session._open_intent(item="Card", category="CARDS", currency="gems",
                             price=11, wallet_before=100, armed=True)
    assert session.journal.open_transactions() == ()


def test_shopping_temporary_commitment_releases_when_tap_never_sent(tmp_path) -> None:
    from shopping import ShoppingSession
    from transactions import TransactionJournal

    path = tmp_path / "bot.db"
    session = ShoppingSession(None, None, None, journal=TransactionJournal(path))
    intent = session._open_intent(item="Card", category="CARDS", currency="gems",
                                  price=10, wallet_before=100, armed=True)
    assert intent is not None
    assert CurrencyRepository(path).committed("gems") == 10
    session._abandon_intent(intent, "no tap")
    assert CurrencyRepository(path).committed("gems") == 0


def test_shopping_commitment_survives_restart_until_purchase_is_proven(tmp_path) -> None:
    from shopping import ShoppingSession
    from transactions import TransactionJournal, Verdict

    path = tmp_path / "bot.db"
    session = ShoppingSession(None, None, None, journal=TransactionJournal(path))
    intent = session._open_intent(item="Card", category="CARDS", currency="gems",
                                  price=10, wallet_before=100, armed=True)
    assert intent is not None
    session._mark_acted(intent)
    assert CurrencyRepository(path).committed("gems") == 10
    restarted = ShoppingSession(None, None, None, journal=TransactionJournal(path))
    outcome = restarted._close(intent.key, price=10, wallet_before=100,
                               wallet_after=90, effect_changed=True)
    assert outcome.verdict == Verdict.BOUGHT
    assert CurrencyRepository(path).committed("gems") == 0


def test_fresh_wallet_releases_resolved_uncertain_shopping_commitments(tmp_path) -> None:
    from shopping import ShoppingSession
    from transactions import Intent, TransactionJournal, Verdict

    path = tmp_path / "bot.db"
    journal = TransactionJournal(path)
    currencies = CurrencyRepository(path)
    for item, price in (("Unlock Cash Bonuses", 40), ("Unlock Coin Bonuses", 100)):
        old = journal.open(Intent(item=item, category="UTILITY", currency="coins",
                                  price=price, wallet_before=205, ts=1.0))
        assert currencies.reserve(f"purchase:{old.key}", "coins", price, wallet=205)
        journal.record_action(old.key, at=2.0)
        assert journal.resolve(old.key, wallet_after=None, effect_changed=None,
                               ts=3.0).verdict is Verdict.UNPROVEN
    assert currencies.committed("coins") == 140

    restarted = ShoppingSession(None, None, None, journal=TransactionJournal(path))
    intent = restarted._open_intent(item="Unlock Defense Upgrades", category="DEFENSE",
                                    currency="coins", price=75, wallet_before=192,
                                    armed=True)

    assert intent is not None
    assert currencies.committed("coins") == 75
    assert [t.key for t in journal.open_transactions()] == [intent.key]


def test_old_wallet_evidence_keeps_uncertain_commitment(tmp_path) -> None:
    from shopping import ShoppingSession
    from transactions import Intent, TransactionJournal

    path = tmp_path / "bot.db"
    journal = TransactionJournal(path)
    currencies = CurrencyRepository(path)
    old = journal.open(Intent(item="Unlock Cash Bonuses", category="UTILITY",
                              currency="coins", price=40, wallet_before=100, ts=1.0))
    assert currencies.reserve(f"purchase:{old.key}", "coins", 40, wallet=100)
    journal.record_action(old.key, at=2.0)
    journal.resolve(old.key, wallet_after=None, effect_changed=None, ts=3.0)

    session = ShoppingSession(None, None, None, journal=journal)
    with pytest.raises(CommitmentError):
        session._open_intent(item="Defense Absolute", category="DEFENSE",
                             currency="coins", price=75, wallet_before=100,
                             armed=True, before={"observed_at": 2.5})

    assert currencies.committed("coins") == 40
