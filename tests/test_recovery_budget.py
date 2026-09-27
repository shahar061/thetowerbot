"""Durable fleet recovery spending and call-count invariants."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from recovery_budget import BudgetOverchargeError, RecoveryBudget


def budget(root: Path, *, incident: int = 50_000, daily: int = 1_000_000,
           calls: int = 2) -> RecoveryBudget:
    return RecoveryBudget(root, incident_limit_microusd=incident,
                          daily_limit_microusd=daily, max_calls=calls)


def test_independent_connections_race_incident_cap(tmp_path: Path) -> None:
    first, second = budget(tmp_path), budget(tmp_path)
    gate = Barrier(2)

    def reserve(ledger: RecoveryBudget, request_id: str) -> bool:
        gate.wait()
        return ledger.reserve(request_id=request_id, incident_id="incident",
                              day="2026-09-27", maximum_microusd=30_000)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda pair: reserve(*pair),
                                   [(first, "r1"), (second, "r2")]))
    assert sorted(results) == [False, True]
    assert first.summary(day="2026-09-27").reserved_microusd == 30_000


def test_independent_connections_race_daily_cap(tmp_path: Path) -> None:
    first, second = budget(tmp_path, daily=50_000), budget(tmp_path, daily=50_000)
    gate = Barrier(2)

    def reserve(ledger: RecoveryBudget, request_id: str) -> bool:
        gate.wait()
        return ledger.reserve(request_id=request_id, incident_id=request_id,
                              day="2026-09-27", maximum_microusd=30_000)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda pair: reserve(*pair),
                                   [(first, "r1"), (second, "r2")]))
    assert sorted(results) == [False, True]
    assert first.summary(day="2026-09-27").reserved_microusd == 30_000


def test_duplicate_request_never_grants_second_send_and_survives_restart(tmp_path: Path) -> None:
    first = budget(tmp_path)
    assert first.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                         maximum_microusd=15_000)
    second = budget(tmp_path)
    assert not second.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                              maximum_microusd=15_000)
    assert not second.reserve(request_id="r1", incident_id="other", day="2026-09-28",
                              maximum_microusd=1)
    assert second.summary(day="2026-09-27").reserved_microusd == 15_000


def test_unknown_charge_retained_until_later_reconciliation(tmp_path: Path) -> None:
    ledger = budget(tmp_path, incident=30_000)
    assert ledger.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=25_000)
    ledger.mark_sent("r1")
    ledger.settle("r1", actual_microusd=None)
    assert not budget(tmp_path, incident=30_000).reserve(
        request_id="r2", incident_id="i1", day="2026-09-27",
        maximum_microusd=10_000)
    ledger.settle("r1", actual_microusd=8_000)
    ledger.settle("r1", actual_microusd=8_000)
    summary = budget(tmp_path, incident=30_000).summary(day="2026-09-27")
    assert summary.reserved_microusd == 0
    assert summary.settled_microusd == 8_000
    assert ledger.reserve(request_id="r2", incident_id="i1", day="2026-09-27",
                          maximum_microusd=10_000)
    assert not ledger.reserve(request_id="r3", incident_id="i1", day="2026-09-27",
                              maximum_microusd=1)
    with pytest.raises(ValueError):
        ledger.settle("r1", actual_microusd=7_000)
    assert ledger.summary(day="2026-09-27").settled_microusd == 8_000


def test_released_unsent_request_refunds_but_sent_zero_cost_keeps_call_slot(tmp_path: Path) -> None:
    ledger = budget(tmp_path)
    assert ledger.reserve(request_id="unsent", incident_id="i1", day="2026-09-27",
                          maximum_microusd=50_000)
    ledger.release_not_sent("unsent")
    assert not ledger.reserve(request_id="unsent", incident_id="i1", day="2026-09-27",
                              maximum_microusd=50_000)
    assert ledger.reserve(request_id="sent1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=10_000)
    ledger.mark_sent("sent1")
    with pytest.raises(ValueError):
        ledger.release_not_sent("sent1")
    ledger.settle("sent1", actual_microusd=0)
    assert ledger.reserve(request_id="sent2", incident_id="i1", day="2026-09-27",
                          maximum_microusd=10_000)
    assert not ledger.reserve(request_id="sent3", incident_id="i1", day="2026-09-27",
                              maximum_microusd=1)


def test_rollover_charges_original_day_but_incident_limit_still_applies(tmp_path: Path) -> None:
    ledger = budget(tmp_path, incident=50_000, daily=50_000)
    assert ledger.reserve(request_id="yesterday", incident_id="i1", day="2026-09-27",
                          maximum_microusd=30_000)
    ledger.mark_sent("yesterday")
    assert ledger.reserve(request_id="today", incident_id="i2", day="2026-09-28",
                          maximum_microusd=30_000)
    assert not ledger.reserve(request_id="same-incident", incident_id="i1",
                              day="2026-09-28", maximum_microusd=30_000)
    assert ledger.summary(day="2026-09-28").reserved_microusd == 30_000
    assert ledger.summary(day="2026-09-27").reserved_microusd == 30_000


def test_overcharge_persists_budget_incident_and_disables_further_calls(tmp_path: Path) -> None:
    ledger = budget(tmp_path)
    assert ledger.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=20_000)
    ledger.mark_sent("r1")
    with pytest.raises(BudgetOverchargeError):
        ledger.settle("r1", actual_microusd=25_000)
    reopened = budget(tmp_path)
    summary = reopened.summary(day="2026-09-27")
    assert summary.settled_microusd == 25_000
    assert summary.disabled
    assert summary.budget_incident_request_id == "r1"
    assert not reopened.reserve(request_id="r2", incident_id="i2", day="2026-09-28",
                                maximum_microusd=1)
    with pytest.raises(BudgetOverchargeError):
        reopened.settle("r1", actual_microusd=25_000)


def test_large_reported_overcharge_still_leaves_summary_readable(tmp_path: Path) -> None:
    ledger = budget(tmp_path)
    assert ledger.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=20_000)
    assert ledger.reserve(request_id="r2", incident_id="i2", day="2026-09-27",
                          maximum_microusd=20_000)
    with pytest.raises(BudgetOverchargeError):
        ledger.settle("r1", actual_microusd=2**63 - 1)
    summary = ledger.summary(day="2026-09-27")
    assert summary.total_microusd == 2**63 - 1 + 20_000
    assert summary.disabled


def test_two_settlements_above_sqlite_sum_range_survive_restart(
        tmp_path: Path) -> None:
    ledger = budget(tmp_path)
    assert ledger.reserve(request_id="first", incident_id="i1", day="2026-09-27",
                          maximum_microusd=20_000)
    assert ledger.reserve(request_id="second", incident_id="i2", day="2026-09-27",
                          maximum_microusd=20_000)
    with pytest.raises(BudgetOverchargeError):
        ledger.settle("first", actual_microusd=2**63 - 1)
    ledger.settle("second", actual_microusd=1)
    summary = budget(tmp_path).summary(day="2026-09-27")
    assert summary.reserved_microusd == 0
    assert summary.settled_microusd == 2**63
    assert summary.total_microusd == 2**63
    assert summary.remaining_microusd == 0
    assert summary.budget_incident_request_id == "first"


def test_validates_immutable_hard_caps_and_amounts(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        budget(tmp_path, incident=50_001)
    with pytest.raises(ValueError):
        budget(tmp_path, daily=1_000_001)
    ledger = budget(tmp_path)
    for day, amount in [("2026-9-27", 1), ("2026-09-27", -1),
                        ("2026-09-27", True), ("2026-09-27", 2**63)]:
        with pytest.raises(ValueError):
            ledger.reserve(request_id="bad", incident_id="i1", day=day,
                           maximum_microusd=amount)
    assert ledger.summary(day="2026-09-27").total_microusd == 0


def test_different_settings_expose_conflict_without_replacing_active_policy(tmp_path: Path) -> None:
    ledger = budget(tmp_path, daily=50_000)
    assert ledger.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=30_000)
    reopened = budget(tmp_path, daily=1_000_000)
    assert reopened.policy().requested_policy_conflict
    assert reopened.policy().active.daily_limit_microusd == 50_000
    assert budget(tmp_path, daily=50_000).summary(
        day="2026-09-27").reserved_microusd == 30_000


def test_sqlite_records_are_separate_from_currency_ledgers(tmp_path: Path) -> None:
    ledger = budget(tmp_path)
    assert ledger.path == tmp_path / "recovery-budget.sqlite3"
    assert ledger.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                          maximum_microusd=10_000)
    with sqlite3.connect(ledger.path) as connection:
        rows = connection.execute("SELECT request_id, maximum_microusd, actual_microusd "
                                  "FROM recovery_reservations").fetchall()
    assert rows == [("r1", 10_000, None)]


def test_policy_update_preserves_commitments_and_old_workers_use_new_caps(tmp_path: Path) -> None:
    old = budget(tmp_path)
    assert old.reserve(request_id="r1", incident_id="i1", day="2026-09-27",
                       maximum_microusd=30_000)
    updated = old.update_policy(expected_revision=1, incident_limit_microusd=20_000,
                                daily_limit_microusd=20_000, max_calls=1)
    assert updated.revision == 2
    assert old.policy().requested_policy_conflict
    assert old.summary(day="2026-09-27").reserved_microusd == 30_000
    assert not old.reserve(request_id="r2", incident_id="i2", day="2026-09-27",
                           maximum_microusd=1)
    assert not old.reserve(request_id="r3", incident_id="i1", day="2026-09-28",
                           maximum_microusd=0)
    restarted = budget(tmp_path)
    assert restarted.policy().active == updated
    from recovery_budget import BudgetPolicyConflictError
    with pytest.raises(BudgetPolicyConflictError) as conflict:
        restarted.update_policy(expected_revision=1, incident_limit_microusd=50_000,
                                daily_limit_microusd=1_000_000, max_calls=2)
    assert conflict.value.active == updated


def test_policy_cas_race_only_one_writer_wins(tmp_path: Path) -> None:
    from recovery_budget import BudgetPolicyConflictError
    first, second = budget(tmp_path), budget(tmp_path)
    gate = Barrier(2)

    def update(ledger: RecoveryBudget, cap: int) -> bool:
        gate.wait()
        try:
            ledger.update_policy(expected_revision=1, incident_limit_microusd=20_000,
                                 daily_limit_microusd=cap, max_calls=1)
            return True
        except BudgetPolicyConflictError:
            return False

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda pair: update(*pair),
                                   [(first, 30_000), (second, 40_000)]))
    assert sorted(results) == [False, True]
    assert first.policy().active.revision == 2


def test_cap_update_and_reserve_are_serialized(tmp_path: Path) -> None:
    first, second = budget(tmp_path), budget(tmp_path)
    gate = Barrier(2)

    def update() -> None:
        gate.wait()
        first.update_policy(expected_revision=1, incident_limit_microusd=0,
                            daily_limit_microusd=0, max_calls=0)

    def reserve() -> bool:
        gate.wait()
        return second.reserve(request_id="race", incident_id="incident",
                              day="2026-09-27", maximum_microusd=1)

    with ThreadPoolExecutor(max_workers=2) as workers:
        update_future, reserve_future = workers.submit(update), workers.submit(reserve)
        update_future.result()
        granted = reserve_future.result()
    assert first.summary(day="2026-09-27").reserved_microusd == int(granted)
    assert not second.reserve(request_id="later", incident_id="other",
                               day="2026-09-27", maximum_microusd=0)


def test_legacy_migration_and_policy_change_preserve_disabled_and_settlements(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "recovery-budget.sqlite3") as db:
        db.execute("CREATE TABLE recovery_policy (singleton INTEGER PRIMARY KEY, "
                   "incident_limit_microusd INTEGER, daily_limit_microusd INTEGER, "
                   "max_calls INTEGER, disabled_request_id TEXT)")
        db.execute("INSERT INTO recovery_policy VALUES (1, 50000, 1000000, 2, 'old')")
        db.execute("CREATE TABLE recovery_reservations (request_id TEXT PRIMARY KEY, "
                   "incident_id TEXT, day TEXT, maximum_microusd INTEGER, "
                   "actual_microusd INTEGER, state TEXT)")
        db.execute("INSERT INTO recovery_reservations VALUES "
                   "('old', 'i', '2026-09-27', 1, 2, 'settled')")
    ledger = budget(tmp_path, daily=500)
    assert ledger.policy().requested_policy_conflict
    assert ledger.policy().active.revision == 1
    ledger.update_policy(expected_revision=1, incident_limit_microusd=500,
                         daily_limit_microusd=500, max_calls=1)
    summary = budget(tmp_path).summary(day="2026-09-27")
    assert summary.disabled and summary.budget_incident_request_id == "old"
    assert summary.settled_microusd == 2
    assert not ledger.reserve(request_id="new", incident_id="other", day="2026-09-28",
                              maximum_microusd=1)


def test_day_clock_reversal_cannot_reopen_unused_past_budget(tmp_path: Path) -> None:
    ledger = budget(tmp_path, daily=10)
    assert ledger.reserve(request_id="today", incident_id="i", day="2026-09-27",
                          maximum_microusd=10)
    assert not budget(tmp_path, daily=10).reserve(
        request_id="past", incident_id="other", day="2026-09-26", maximum_microusd=1)
