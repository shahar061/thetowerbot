"""Fleet recovery configuration is revision fenced and preserves accounting."""

from __future__ import annotations

from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread, current_thread, main_thread

import pytest

from recovery_budget import BudgetOverchargeError, RecoveryBudget
from recovery_policy import RecoverySettings


def test_defaults_off_and_active_policy_visible(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    state = RecoverySettingsStore(tmp_path).read()
    assert state.settings.mode == "off"
    assert state.settings_revision == 1
    assert state.policy.active.revision == 1
    assert state.policy.requested_policy_conflict is False
    assert state.assist_allowed_actions == ()


def test_explicit_policy_cas_preserves_existing_reservations(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    budget = RecoveryBudget(tmp_path)
    assert budget.reserve(request_id="call-a", incident_id="incident-a", day="2026-09-27",
                          maximum_microusd=40_000)
    store = RecoverySettingsStore(tmp_path)
    original = store.read()
    saved = store.save(RecoverySettings(mode="shadow", daily_limit_microusd=40_000), shadow_worker="worker-a",
                       expected_settings_revision=1, expected_policy_revision=1)
    assert saved.settings.mode == "shadow"
    assert saved.policy.active.revision == 2
    assert saved.policy.active.daily_limit_microusd == 40_000
    assert budget.summary(day="2026-09-27").reserved_microusd == 40_000
    with pytest.raises(Exception) as conflict:
        store.save(RecoverySettings(mode="shadow", daily_limit_microusd=60_000), shadow_worker="worker-a",
                   expected_settings_revision=1, expected_policy_revision=1)
    assert getattr(conflict.value, "state").settings_revision == 2
    assert store.read().policy.active.daily_limit_microusd == 40_000


def test_settings_update_retains_overcharge_disabled_state(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    budget = RecoveryBudget(tmp_path)
    assert budget.reserve(request_id="charged", incident_id="incident", day="2026-09-27",
                          maximum_microusd=1_000)
    budget.mark_sent("charged")
    with pytest.raises(BudgetOverchargeError):
        budget.settle("charged", actual_microusd=2_000)
    store = RecoverySettingsStore(tmp_path)
    before = store.read()
    saved = store.save(RecoverySettings(mode="shadow"), shadow_worker="worker-a", expected_settings_revision=1,
                       expected_policy_revision=before.policy.active.revision)
    assert saved.policy.active.disabled_request_id == "charged"


def test_stale_policy_revision_and_external_policy_conflict_remain_visible(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    store = RecoverySettingsStore(tmp_path)
    RecoveryBudget(tmp_path).update_policy(expected_revision=1,
                                           incident_limit_microusd=25_000,
                                           daily_limit_microusd=500_000,
                                           max_calls=1)
    state = store.read()
    assert state.policy.requested_policy_conflict is True
    assert state.policy.active.max_calls == 1
    with pytest.raises(Exception):
        store.save(RecoverySettings(mode="shadow"), shadow_worker="worker-a", expected_settings_revision=1,
                   expected_policy_revision=1)
    assert store.read().settings.mode == "off"


def test_assist_requires_recorded_canary_capability(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    store = RecoverySettingsStore(tmp_path)
    with pytest.raises(ValueError, match="assist_uncalibrated"):
        store.save(RecoverySettings(mode="assist"), expected_settings_revision=1,
                   expected_policy_revision=1)
    assert store.read().settings.mode == "off"


def test_settings_write_failure_rolls_back_budget_policy(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    store = RecoverySettingsStore(tmp_path)
    store.read()
    with sqlite3.connect(tmp_path / "recovery-budget.sqlite3") as db:
        db.execute("""CREATE TRIGGER fail_settings BEFORE UPDATE ON recovery_settings
                   BEGIN SELECT RAISE(ABORT, 'injected'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        store.save(RecoverySettings(mode="shadow", daily_limit_microusd=500_000), shadow_worker="worker-a",
                   expected_settings_revision=1, expected_policy_revision=1)
    state = store.read()
    assert state.settings_revision == 1 and state.settings.mode == "off"
    assert state.policy.active.revision == 1
    assert state.policy.active.daily_limit_microusd == 1_000_000


def test_competing_settings_edits_only_one_wins(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    store = RecoverySettingsStore(tmp_path)
    store.read()

    def save(limit: int) -> bool:
        try:
            store.save(RecoverySettings(mode="shadow", daily_limit_microusd=limit), shadow_worker="worker-a",
                       expected_settings_revision=1, expected_policy_revision=1)
            return True
        except Exception as exc:
            assert hasattr(exc, "state")
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (500_000, 600_000)))
    assert sorted(results) == [False, True]
    state = store.read()
    assert state.settings_revision == 2 and state.policy.active.revision == 2
    assert state.policy.active.daily_limit_microusd == state.settings.daily_limit_microusd


def test_settings_snapshot_holds_one_read_transaction_across_interleaved_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import recovery_budget

    reader = RecoveryBudget(tmp_path)
    writer = RecoveryBudget(tmp_path)
    original_policy = recovery_budget._policy
    started, committed = Event(), Event()
    writers: list[Thread] = []
    transaction_active: list[bool] = []

    def commit_new_settings() -> None:
        started.set()
        writer.update_settings(expected_settings_revision=1, expected_policy_revision=1,
                               settings_json='{"settings":{"mode":"shadow"},"shadow_worker":"worker-a"}',
                               incident_limit_microusd=50_000,
                               daily_limit_microusd=500_000, max_calls=2)
        committed.set()

    def interleave(db: sqlite3.Connection) -> recovery_budget.BudgetPolicy:
        if current_thread() is not main_thread():
            return original_policy(db)
        transaction_active.append(db.in_transaction)
        thread = Thread(target=commit_new_settings)
        writers.append(thread)
        thread.start()
        assert started.wait(1)
        # The other connection attempts its commit before this policy read.
        # An open read snapshot keeps both rows at the old committed version.
        committed.wait(.05)
        return original_policy(db)

    monkeypatch.setattr(recovery_budget, "_policy", interleave)
    revision, raw, policy = reader.settings_snapshot()
    for thread in writers:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert committed.is_set()
    assert transaction_active == [True]
    assert (revision, raw, policy.revision, policy.daily_limit_microusd) == (1, None, 1, 1_000_000)
    monkeypatch.setattr(recovery_budget, "_policy", original_policy)
    assert reader.settings_snapshot()[0] == 2


def test_shadow_is_limited_to_one_stable_worker(tmp_path: Path) -> None:
    from recovery_settings import RecoverySettingsStore

    store = RecoverySettingsStore(tmp_path)
    with pytest.raises(ValueError, match="shadow_worker_required"):
        store.save(RecoverySettings(mode="shadow"), expected_settings_revision=1,
                   expected_policy_revision=1)
    saved = store.save(RecoverySettings(mode="shadow"), shadow_worker="worker-a",
                       expected_settings_revision=1, expected_policy_revision=1)
    assert saved.shadow_worker == "worker-a"
    assert store.effective_settings("worker-a").mode == "shadow"
    assert store.effective_settings("worker-b").mode == "off"
