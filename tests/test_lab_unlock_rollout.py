"""The fleet's lab-slot unlock rollout: dry run, canary, fleet, halted."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest

from lab_unlock_rollout import (DryRun, LabUnlockRollout, RolloutError, SlotRollout,
                                canary_present, rollout_status)
from tests.test_labs_view import _registered


def canary(rollout: LabUnlockRollout, worker: str = "Air_1", account: str = "account-a") -> None:
    rollout.note_dry_run(2, worker, 100, 150, 1000., account_id=account)
    rollout.note_dry_run(2, worker, 100, 150, 1600., account_id=account)


def _pool(root: Path, *names: str) -> None:
    (root / "reroll-pool.json").write_text(json.dumps(
        [{"name": name, "endpoint": "127.0.0.1:5555", "lease_id": "lease"} for name in names]))


def test_a_missing_file_leaves_every_slot_at_dry_run(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.slots() == {slot: SlotRollout() for slot in (2, 3, 4, 5)}
    assert not rollout.may_tap(2, "Air_1")
    assert not (tmp_path / "lab-unlock-rollout.json").exists()


def test_two_rehearsals_ten_minutes_apart_promote_that_worker_to_canary(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.note_dry_run(2, "Air_1", 100, 150, 1000., account_id="account-a").promoted is None
    assert rollout.slot(2).rehearsals("Air_1") == 1
    assert rollout.note_dry_run(2, "Air_1", 100, 151, 1599., account_id="account-a").promoted is None
    assert rollout.note_dry_run(2, "Air_2", 100, 300, 1700., account_id="account-b").promoted is None
    change = rollout.note_dry_run(2, "Air_1", 100, 152, 1600., account_id="account-a")
    assert change.promoted == "canary"
    state = LabUnlockRollout(tmp_path).slot(2)
    assert (state.stage, state.canary_worker, state.canary_account) == ("canary", "Air_1", "account-a")
    assert rollout.may_tap(2, "Air_1") and not rollout.may_tap(2, "Air_2")
    assert not rollout.may_tap(3, "Air_1")


def test_more_than_twenty_interleaved_workers_still_let_one_promote(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    rollout.note_dry_run(2, "Air_1", 100, 150, 0., account_id="account-a")
    for index in range(25):
        rollout.note_dry_run(2, f"Noise_{index}", 100, 150, 10. + index, account_id=f"noise-{index}")
    change = rollout.note_dry_run(2, "Air_1", 100, 150, 600., account_id="account-a")
    assert change.promoted == "canary"


def test_a_worker_moved_to_another_account_starts_its_rehearsals_again(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    rollout.note_dry_run(2, "Air_1", 100, 150, 1000., account_id="account-a")
    assert rollout.note_dry_run(2, "Air_1", 100, 150, 2000., account_id="account-b").promoted is None


def test_a_rehearsal_price_that_disagrees_with_the_catalog_halts_the_slot(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    change = rollout.note_dry_run(3, "Air_1", 450, 500, 1000., account_id="account-a")
    assert change.halted
    assert change.after.halted_reason == "Rehearsal read 450 gems for slot 3; the catalog says 400"
    assert rollout.note_dry_run(3, "Air_1", 400, 500, 2000., account_id="account-a").after.stage == "halted"


def test_a_proven_canary_unlock_promotes_the_slot_to_fleet(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    assert rollout.note_unlock(2, "Air_2", "k0", "bought", at=1.).after.stage == "canary"
    change = rollout.note_unlock(2, "Air_1", "k1", "bought", at=2000.)
    assert change.promoted == "fleet"
    assert change.after.unlock == {"worker": "Air_1", "transaction_key": "k1",
                                   "outcome": "bought", "at": 2000.}
    assert rollout.may_tap(2, "Air_7")
    later = rollout.note_unlock(2, "Air_7", "k2", "bought", at=3000.)
    assert later.after.unlock["transaction_key"] == "k1"  # the fleet stage records nothing new


def test_a_second_missed_canary_tap_halts_the_slot(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    once = rollout.note_unlock(2, "Air_1", "k1", "not_charged", at=2000.)
    assert once.after.stage == "canary" and not once.halted
    twice = rollout.note_unlock(2, "Air_1", "k2", "not_charged", at=3000.)
    assert twice.halted and twice.after.halted_reason == "The canary's unlock tap did not land twice"
    with pytest.raises(ValueError):
        rollout.note_unlock(2, "Air_1", "k3", "maybe", at=1.)


def test_halt_keeps_the_first_reason_and_reset_returns_to_dry_run(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    with pytest.raises(RolloutError, match="slot_not_halted"):
        rollout.reset(2)
    assert rollout.slot(2).stage == "canary"
    first = rollout.halt(2, "Post-tap screen was not understood", ("a.png", "b.png"))
    assert first.halted and first.after.evidence == ("a.png", "b.png")
    again = rollout.halt(2, "another reason", ("c.png",))
    assert not again.halted
    assert again.after.halted_reason == "Post-tap screen was not understood"
    assert again.after.evidence == ("a.png", "b.png", "c.png")
    assert not rollout.may_tap(2, "Air_1")
    assert rollout.reset(2).after == SlotRollout()


def test_release_canary_only_releases_the_expected_worker(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    assert rollout.release_canary(2, expected_worker="Air_2").after.stage == "canary"
    assert rollout.release_canary(2, expected_worker="Air_1").after == SlotRollout()


def test_a_canary_that_left_the_pool_is_released(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    _registered(tmp_path, "Air_1", "account-a")
    _pool(tmp_path, "Air_1")
    assert rollout.release_absent_canary(2).after.stage == "canary"
    _pool(tmp_path)
    assert rollout.release_absent_canary(2).after.stage == "dry_run"


def test_a_canary_bound_to_another_account_is_released(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    _registered(tmp_path, "Air_1", "account-b")
    _pool(tmp_path, "Air_1")
    assert canary_present(tmp_path, "Air_1", "account-a") is False
    assert rollout.release_absent_canary(2).after.stage == "dry_run"


def test_an_unreadable_pool_never_releases_a_canary(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    (tmp_path / "reroll-pool.json").write_text("{bad")
    assert canary_present(tmp_path, "Air_1", "account-a") is None
    assert rollout.release_absent_canary(2).after.stage == "canary"


def test_a_corrupt_file_is_moved_aside_and_every_slot_is_dry_run(tmp_path: Path) -> None:
    path = tmp_path / "lab-unlock-rollout.json"
    path.write_text('{"schema_version": 1, "slots": {"2": {"stage": "bogus"}}}')
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.slots(quarantine=False)[2] == SlotRollout()
    assert path.exists()  # a dashboard read never moves it
    assert rollout.slot(2) == SlotRollout()
    assert not path.exists()
    (aside,) = tmp_path.glob("lab-unlock-rollout.json.corrupt-*")
    assert "bogus" in aside.read_text()
    rollout.note_dry_run(2, "Air_1", 100, 150, 1., account_id="a")
    assert json.loads(path.read_text())["slots"]["2"]["dry_runs"][0]["worker"] == "Air_1"


def test_a_file_that_is_not_valid_utf8_is_quarantined_too(tmp_path: Path) -> None:
    path = tmp_path / "lab-unlock-rollout.json"
    path.write_bytes(b'{"schema_version": 1, "slots": {"2": {\xff\xfe')
    rollout = LabUnlockRollout(tmp_path)
    assert rollout.slots(quarantine=False)[2] == SlotRollout()
    assert path.exists()  # a dashboard read never moves it
    assert rollout.slot(2) == SlotRollout()
    assert not path.exists()
    (aside,) = tmp_path.glob("lab-unlock-rollout.json.corrupt-*")
    assert aside.read_bytes().startswith(b'{"schema_version"')


def test_concurrent_writers_never_lose_a_rehearsal(tmp_path: Path) -> None:
    workers = [f"Air_{index}" for index in range(8)]

    def rehearse(worker: str) -> None:
        LabUnlockRollout(tmp_path).note_dry_run(2, worker, 100, 150, 1000., account_id=worker)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(rehearse, workers))
    assert sorted(run.worker for run in LabUnlockRollout(tmp_path).slot(2).dry_runs) == sorted(workers)


def test_a_lost_promotion_race_sees_the_winner(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    with ThreadPoolExecutor(max_workers=2) as pool:
        changes = list(pool.map(lambda key: LabUnlockRollout(tmp_path).note_unlock(
            2, "Air_1", key, "bought", at=2000.), ["k1", "k2"]))
    assert sorted(change.promoted is not None for change in changes) == [False, True]
    assert rollout.slot(2).stage == "fleet"


def test_status_lines_for_fleet_state() -> None:
    assert rollout_status(SlotRollout(), 2, "Air_1") == "Rehearsing slot 2 · 0/2 dry runs"
    one = SlotRollout(dry_runs=(DryRun("Air_1", 1., 100, 150, "a"),))
    assert rollout_status(one, 2, "Air_1") == "Rehearsing slot 2 · 1/2 dry runs"
    held = SlotRollout(stage="canary", canary_worker="Air_1")
    assert rollout_status(held, 2, "Air_1") == "Canary: Air_1 unlocks slot 2 next visit"
    assert rollout_status(held, 2, "Air_2") == "Waiting for canary"
    assert rollout_status(SlotRollout(stage="fleet"), 3, "Air_2") == "Unlocking slot 3"
    assert rollout_status(SlotRollout(stage="halted", halted_reason="x"), 2, None) == "Halted: x"


def test_snapshot_rows_for_the_dashboard(tmp_path: Path) -> None:
    rollout = LabUnlockRollout(tmp_path)
    canary(rollout)
    rows = rollout.snapshot()
    assert [row["slot"] for row in rows] == [2, 3, 4, 5]
    assert rows[0] == {"slot": 2, "stage": "canary", "canary_worker": "Air_1", "dry_runs": 2,
                       "price": 100, "halted_reason": None, "evidence": []}
    assert [row["price"] for row in rows] == [100, 400, 1400, 3000]
