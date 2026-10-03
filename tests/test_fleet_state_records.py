"""Read-only worker DB reads for the Fleet State page."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

import db
from fleet.state_records import RECENT_LIMIT, ForeignDatabase, read_records

BOUGHT = json.dumps({"verdict": "bought"})


def _database(tmp_path: Path, account: str = "account-a") -> Path:
    path = tmp_path / "tower_bot.db"
    db.bind_account(path, account)
    return path


def _ledger(conn: sqlite3.Connection, ts: float, kind: str, item: str, category: str,
            delta: int | None, price: int | None, detail: str = BOUGHT, dry_run: int = 0) -> None:
    conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (ts, kind, item, category, "gems" if kind == "CARD_BUY" else "coins",
                  delta, price, dry_run, detail))


def _run(conn: sqlite3.Connection, run_id: int, tier: int, wave: int, abandoned: int = 0,
         ended: bool = True) -> None:
    conn.execute("INSERT INTO runs(id, started_at, ended_at, wave, coins, tier, abandoned) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (run_id, 100. * run_id, 100. * run_id + 50 if ended else None, wave, 1000,
                  tier, abandoned))


def test_a_missing_or_foreign_database_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_records(tmp_path / "absent.db", "account-a", None)
    path = _database(tmp_path, "account-b")
    with pytest.raises(ForeignDatabase):
        read_records(path, "account-a", None)


def test_best_wave_is_per_tier_over_finished_runs_that_were_not_abandoned(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 11, 5020)
        _run(conn, 2, 11, 9999, abandoned=1)
        _run(conn, 3, 7, 1420)
        _run(conn, 4, 11, 4000)
        _run(conn, 5, 11, 8000, ended=False)
    records = read_records(path, "account-a", None)
    assert records.best_waves == {11: 5020, 7: 1420}


def test_the_last_five_finished_runs_are_newest_first(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        for run_id in range(1, 8):
            _run(conn, run_id, 1, 10 * run_id)
        _run(conn, 8, 1, 999, ended=False)
    records = read_records(path, "account-a", None)
    assert [run["id"] for run in records.runs] == [7, 6, 5, 4, 3]


def test_current_run_upgrades_come_from_its_purchase_events(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        _run(conn, 1, 1, 10)
        conn.execute("INSERT INTO run_upgrades VALUES (1, 'health', 3, 90, 0)")
        _run(conn, 2, 1, 0, ended=False)
        for seq, upgrade_id in enumerate(["damage", "damage", "health", None], start=1):
            conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                         (seq, 2, 200. + seq, "BattlePurchased",
                          json.dumps({"item": "x", "upgrade_id": upgrade_id, "value": None})))
    current = read_records(path, "account-a", 2)
    assert current.run_upgrades_scope == "current"
    assert sorted((row["upgrade_id"], row["levels"]) for row in current.run_upgrades) == [
        ("damage", 2), ("health", 1)]
    last = read_records(path, "account-a", None)
    assert last.run_upgrades_scope == "last"
    assert [(row["upgrade_id"], row["levels"]) for row in last.run_upgrades] == [("health", 3)]


def test_a_new_account_has_no_runs_no_revision_and_zero_gems_spent(tmp_path: Path) -> None:
    records = read_records(_database(tmp_path), "account-a", None)
    assert (records.revision, records.runs, records.best_waves) == (None, [], {})
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], None)
    assert (records.card_gems, records.workshop_recent, records.last_seen) == (0, [], None)
    assert records.balances == {"coins": None, "gems": None}


def test_a_revision_stamped_with_another_account_is_ignored(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)",
                     (json.dumps({"account_id": "account-old", "workshop_stats": []}),))
    assert read_records(path, "account-a", None).revision is None


def test_foreign_facts_inside_an_account_revision_do_not_supply_workshop_evidence(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES (?)", (json.dumps({
            "account_id": "account-a", "workshop_stats": [{
                "concept_id": "stats.thorns", "value": 6, "status": "verified",
                "scope": {"account_id": "account-old"}, "evidence": {"observed_at": 5., "raw_value": "6%"}}],
            "unlocks": [{"concept_id": "unlocks.thorns", "value": True, "status": "verified",
                         "scope": {"account_id": "account-old"}}]}),))
    records = read_records(path, "account-a", None)
    assert records.revision["workshop_stats"] == records.revision["unlocks"] == []
    assert records.workshop_evidence.actions.level_purchases == {}


def test_a_corrupt_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: a bad JSON blob in account_revisions must not blank the account."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('{bad')")
    assert read_records(path, "account-a", None).revision is None


def test_a_non_dict_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: valid JSON that is not an object (e.g. null) must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('null')")
    assert read_records(path, "account-a", None).revision is None


def test_a_list_revision_detail_is_ignored_not_raised(tmp_path: Path) -> None:
    """Review Focus: a non-null non-dict JSON value (a list) must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO account_revisions(detail) VALUES ('[1, 2]')")
    assert read_records(path, "account-a", None).revision is None


def test_a_corrupt_live_run_purchase_detail_leaves_run_upgrades_empty(tmp_path: Path) -> None:
    """Review Focus: a bad BattlePurchased detail must empty run_upgrades, not raise."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                     (1, 5, 200., "BattlePurchased", "{bad"))
    records = read_records(path, "account-a", 5)
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], "current")


def test_a_null_live_run_purchase_detail_leaves_run_upgrades_empty(tmp_path: Path) -> None:
    """Review Focus: a null (not object) BattlePurchased detail must not crash .get()."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("INSERT INTO events(seq, run_id, ts, type, detail) VALUES (?,?,?,?,?)",
                     (1, 5, 200., "BattlePurchased", "null"))
    records = read_records(path, "account-a", 5)
    assert (records.run_upgrades, records.run_upgrades_scope) == ([], "current")


def test_a_huge_ledger_is_summed_and_bounded_by_sql(tmp_path: Path) -> None:
    """Review Focus: years of ledger rows must not be loaded into Python."""
    path = _database(tmp_path)
    from fleet.workshop_prices import WorkshopPrices
    memory = WorkshopPrices(tmp_path, "account-a")
    memory.observe("damage", 30, 20_000, now=20_000.)
    memory.save()
    with db.connect(path) as conn:
        conn.executemany(
            "INSERT INTO ledger(ts,kind,item,category,currency,delta,price,dry_run,detail) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(float(i), "WORKSHOP_BUY", "Damage", "ATTACK", "coins", -10, 10, 0, BOUGHT)
             for i in range(50_000)])
        _ledger(conn, 60_000., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99, dry_run=1)
        _ledger(conn, 60_001., "WORKSHOP_BUY", "Damage", "ATTACK", -99, 99,
                detail=json.dumps({"verdict": "unconfirmed"}))
        _ledger(conn, 60_002., "CARD_BUY", "Card", "CARDS", -20, 20)
        _ledger(conn, 60_003., "CARD_BUY", "Card", "CARDS", None, 20)
    started = time.monotonic()
    records = read_records(path, "account-a", None)
    assert time.monotonic() - started < 2
    assert records.workshop_spent == [("Damage", "ATTACK", 500_000)]
    assert records.workshop_evidence.actions.price_purchases == {"damage": 29_999}
    assert records.workshop_evidence.actions.invalidated == {"damage": 60_001.}
    assert "damage" not in records.workshop_evidence.quotes
    assert len(records.workshop_recent) == RECENT_LIMIT
    assert records.workshop_recent[0] == {"ts": 49_999., "item": "Damage", "category": "ATTACK",
                                          "price": 10}
    assert records.card_gems == 20
    assert [row["price"] for row in records.card_recent] == [20, 20]


def test_a_database_locked_mid_write_fails_fast(tmp_path: Path) -> None:
    """Review Focus: a worker holding a write lock costs this read 0.1 s, not a hang."""
    path = _database(tmp_path)
    with db.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")  # WAL would let the reader through
    locker = sqlite3.connect(path)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            read_records(path, "account-a", None)
        assert time.monotonic() - started < 1
    finally:
        locker.rollback()
        locker.close()


def test_cards_projection_preserves_partial_facts_and_equipment(tmp_path: Path) -> None:
    from account_state import AccountRepository
    from card_models import CardFieldEvidence, CardItem, CardSnapshot
    from card_store import CardStore
    from currencies import CurrencyRepository
    from evidence_scope import FactScope
    path = _database(tmp_path)
    scope = FactScope('account-a', 'lease', 'generation', 1)
    CurrencyRepository(path).bind_scope(scope)
    store = CardStore(path)
    evidence = CardFieldEvidence(scope=scope, visit_id='visit', observed_at=10., evidence_ref='frame', confidence=.99)
    item = CardItem(card_id='cards.damage', ownership='owned', level=3, observed_at=10., evidence_ref='frame',
                    field_evidence={'level': evidence, 'ownership': evidence})
    first = CardSnapshot(scope=scope, revision=1, observed_at=10., visit_id='visit', items=(item,), capacity=2,
                         equipped=('cards.damage',), equipment_evidence=evidence,
                         collection_complete=False, equipment_complete=True)
    store.observe(first)
    store.observe(first.model_copy(update={'revision': 2, 'observed_at': 20., 'items': (),
                                           'capacity': None, 'equipped': None, 'equipment_complete': False}))
    revision = AccountRepository(path).latest()
    facts = {fact.concept_id: fact for fact in revision.cards}
    assert facts['cards.damage.level'].value == 3
    assert facts['cards.damage.level'].evidence.observed_at == 10.
    assert facts['cards.slots.capacity'].value == 2
    assert facts['cards.slots.capacity'].evidence.observed_at == 10.
    assert revision.cards_equipped == ('cards.damage',)
    assert revision.cards_equipment_evidence.evidence.observed_at == 10.
    records = read_records(path, 'account-a', None)
    assert records.revision['cards_equipped'] == ['cards.damage']
    assert any(fact['concept_id'] == 'cards.damage.level' for fact in records.revision['cards'])


def test_cards_and_labs_revisions_coexist_through_account_state(tmp_path: Path) -> None:
    from account_state import AccountRepository, AccountState
    from card_models import CardSnapshot
    from evidence_scope import FactScope
    from fleet.identity import IdentityEvidence
    path = _database(tmp_path)
    repository = AccountRepository(path)
    state = AccountState(repository)
    scope = FactScope('account-a', 'lease', 'generation', 1)
    state.bind_scope(scope, identity=IdentityEvidence('account-a', 5., 'identity'))
    snapshot = CardSnapshot(scope=scope, revision=1, observed_at=10., visit_id='visit',
                            capacity=2, equipped=(), collection_complete=False, equipment_complete=True)
    assert state.observe_cards(snapshot)
    assert state.record_labs(slots_owned=3, observed_at=11.)
    assert repository.latest().cards_equipped == ()
    assert repository.latest().cards is not None
    assert state.observe_cards(snapshot.model_copy(update={'observed_at': 12., 'capacity': 3}))
    assert repository.latest().lab_slots_owned == 3


def test_labs_first_cards_projection_uses_bound_account_identity(tmp_path: Path) -> None:
    from account_state import AccountRepository, AccountState
    from card_models import CardSnapshot
    from evidence_scope import FactScope
    from fleet.identity import IdentityEvidence
    path = _database(tmp_path)
    repository = AccountRepository(path)
    state = AccountState(repository)
    scope = FactScope('account-a', 'lease', 'generation', 1)
    state.bind_scope(scope, identity=IdentityEvidence('account-a', 5., 'identity'))
    assert state.record_labs(slots_owned=3, observed_at=6.)
    assert repository.latest().account_id is None
    snapshot = CardSnapshot(scope=scope, revision=1, observed_at=10., visit_id='visit',
                            capacity=2, equipped=(), collection_complete=False, equipment_complete=True)
    assert state.observe_cards(snapshot)
    revision = repository.latest()
    assert revision.account_id == 'account-a'
    assert revision.lab_slots_owned == 3
    assert revision.cards_equipped == ()
    assert state.record_labs(slots_owned=4, observed_at=11.)
    revision = repository.latest()
    assert revision.account_id == 'account-a'
    assert revision.lab_slots_owned == 4
    assert revision.cards_equipped == ()
    assert next(f.value for f in revision.cards if f.concept_id == 'cards.slots.capacity') == 2


def test_cards_projection_rejects_explicitly_foreign_revision(tmp_path: Path) -> None:
    from account_state import AccountRepository, AccountRevision, AccountState
    from card_models import CardSnapshot
    from card_store import CardStore
    from evidence_scope import FactScope
    from fleet.identity import IdentityEvidence
    path = _database(tmp_path)
    repository = AccountRepository(path)
    repository.save_account(AccountRevision(account_id='account-other', lab_slots_owned=3), ())
    state = AccountState(repository)
    scope = FactScope('account-a', 'lease', 'generation', 1)
    state.bind_scope(scope, identity=IdentityEvidence('account-a', 5., 'identity'))
    snapshot = CardSnapshot(scope=scope, revision=1, observed_at=10., visit_id='visit',
                            capacity=2, equipped=(), collection_complete=False, equipment_complete=True)
    with pytest.raises(ValueError, match='foreign account revision'):
        state.observe_cards(snapshot)
    assert repository.latest().account_id == 'account-other'
    assert repository.latest().cards is None
    assert CardStore(path).snapshot() is None
