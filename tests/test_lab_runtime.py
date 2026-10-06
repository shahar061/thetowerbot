"""Recorded lab observations survive restarts without inventing completions."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_lab_screen import frame, recorded


def runtime(root: Path, account_id: str = "ACCOUNT-A", **scope: object) -> object:
    from lab_runtime import LabRuntime
    return LabRuntime(root, account_id, lease_id="lease-a", generation="worker-1", **scope)


def observation(now: float = 1000.) -> object:
    from lab_screen import read_slots
    return read_slots(frame("menu_labs_active"), recorded("menu_labs_active"), observed_at=now)


def confirmed(store: object) -> object:
    first = observation()
    store.observe(first)
    store.observe(replace(first, observed_at=1001.))
    return store.snapshot()


def test_confirmed_jobs_survive_restart_and_expiry_is_not_completion(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    store.observe(observation())
    assert all(j.state == "unknown" for j in store.snapshot().slots)
    snapshot = confirmed(store)
    assert snapshot.slots_owned == 5
    assert [j.state for j in snapshot.slots] == ["researching", "idle", "idle", "idle", "researching"]
    assert snapshot.slots[0].confirmed
    assert snapshot.slots[0].transaction_id is None
    assert snapshot.slots[0].catalog_revision
    assert snapshot.slots[0].generation
    resumed = runtime(tmp_path).snapshot()
    assert resumed == replace(snapshot, strip_complete=False, slots=tuple(replace(job, confirmed=False,
        evidence_status='historical') for job in snapshot.slots))
    assert resumed.slots[0].state == "researching"
    assert (resumed.slots[0].source_level, resumed.slots[0].target_level) == (86, 87)


def test_partial_or_unknown_readings_do_not_erase_an_unseen_job(tmp_path: Path) -> None:
    from lab_screen import read_slots
    store = runtime(tmp_path)
    before = confirmed(store)
    partial = read_slots(frame("menu_labs_active")[:1800], recorded("menu_labs_active"),
                         observed_at=1002.)
    store.observe(partial)
    store.observe(replace(partial, observed_at=1003.))
    after = store.snapshot()
    assert after.slots[4] == before.slots[4]
    assert after.slots_owned == 5


def test_scope_change_cannot_restore_other_accounts_or_worker_generations(tmp_path: Path) -> None:
    from lab_runtime import LabRuntime
    confirmed(runtime(tmp_path))
    for changed in (runtime(tmp_path, "ACCOUNT-B"),
                    LabRuntime(tmp_path, "ACCOUNT-A", lease_id="lease-a", generation="worker-2"),
                    runtime(tmp_path, epoch=1)):
        assert all(j.state == "unknown" for j in changed.snapshot().slots)
        assert changed.snapshot().slots_owned is None


def test_timer_correction_preserves_job_generation_and_observed_wall_time(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    before = confirmed(store).slots[0]
    reading = observation(1010.)
    boosted = replace(reading.jobs[0], remaining_s=120., completes_at=1130.,
                      speed_multiplier=2., acceleration="active", native_repeat="enabled")
    reading = replace(reading, jobs=(boosted, *reading.jobs[1:]))
    store.observe(reading)
    store.observe(replace(reading, observed_at=1011.))
    after = store.snapshot().slots[0]
    assert after.expected_finish == 1130.  # No multiplication by battle/lab speed.
    assert after.remaining_s == 120.
    assert after.generation == before.generation
    assert after.speed_multiplier == 2. and after.native_repeat == "enabled"


def test_new_job_gets_a_generation_only_after_fresh_confirmed_evidence(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    before = confirmed(store).slots[0]
    reading = observation(1010.)
    new_job = replace(reading.jobs[0], source_level=87, target_level=88)
    reading = replace(reading, jobs=(new_job, *reading.jobs[1:]))
    store.observe(reading)
    assert store.snapshot().slots[0] == before
    store.observe(replace(reading, observed_at=1011.))
    assert store.snapshot().slots[0].generation != before.generation
    store.observe(observation(900.))
    store.observe(observation(901.))
    assert store.snapshot().slots[0].target_level == 88


def test_confirmed_idle_clears_only_its_slot(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    before = confirmed(store)
    reading = observation(1010.)
    idle = replace(reading.jobs[0], status="idle", concept_id=None, raw_name="Lab Offline",
                   completes_at=None, remaining_s=None, source_level=None, target_level=None)
    reading = replace(reading, jobs=(idle, *reading.jobs[1:]))
    store.observe(reading)
    store.observe(replace(reading, observed_at=1011.))
    assert store.snapshot().slots[0].state == "idle"
    assert store.snapshot().slots[4].state == before.slots[4].state == "researching"
    assert store.snapshot().slots[4].generation == before.slots[4].generation


@pytest.mark.parametrize("already_running", [False, True])
def test_incomplete_apparent_idle_cannot_clear_or_confirm_a_slot(
    tmp_path: Path, already_running: bool,
) -> None:
    store = runtime(tmp_path)
    before = confirmed(store) if already_running else store.snapshot()
    reading = observation(1010.)
    idle = replace(reading.jobs[0], status="idle", concept_id=None, raw_name="Lab Offline",
                   completes_at=None, remaining_s=None, source_level=None, target_level=None)
    reading = replace(reading, slots_owned=None, slots_status="unreadable", jobs=(idle,))
    store.observe(reading)
    store.observe(replace(reading, observed_at=1011.))
    assert store.snapshot() == before


def test_observing_another_account_preserves_the_first_accounts_snapshot(tmp_path: Path) -> None:
    first = runtime(tmp_path, "ACCOUNT-A")
    first_snapshot = confirmed(first)
    second = runtime(tmp_path, "ACCOUNT-B")
    second_snapshot = confirmed(second)

    for account, snapshot in (('ACCOUNT-A', first_snapshot), ('ACCOUNT-B', second_snapshot)):
        assert runtime(tmp_path, account).snapshot() == replace(snapshot, strip_complete=False, slots=tuple(
            replace(job, confirmed=False, evidence_status='historical') for job in snapshot.slots))
    assert first.path != second.path


def test_unknown_target_never_becomes_an_actionable_identified_job(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    reading = observation()
    unknown_target = replace(reading.jobs[0], source_level=None, target_level=None)
    reading = replace(reading, jobs=(unknown_target, *reading.jobs[1:]))
    store.observe(reading)
    store.observe(replace(reading, observed_at=1001.))
    first = store.snapshot().slots[0]
    assert first.state == "researching" and first.target_level is None
    assert not first.confirmed
    assert first.generation is None
    later = replace(reading, observed_at=1100., jobs=(
        replace(unknown_target, completes_at=1200., remaining_s=100.), *reading.jobs[1:]))
    store.observe(later)
    store.observe(replace(later, observed_at=1101.))
    assert not store.snapshot().slots[0].confirmed
    assert store.snapshot().slots[0].generation is None


def test_legacy_cadence_is_historical_and_never_fakes_other_slots(tmp_path: Path) -> None:
    from lab_runtime import LabRuntime
    (tmp_path / "lab-slot1-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "kind": "wait_running", "game_speed_level": 1,
        "observed_at": 1000., "job_completes_at": 1200.}))
    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "owned", "observed_at": 1000.}))
    snapshot = LabRuntime(tmp_path, "ACCOUNT-A").snapshot()
    assert snapshot.slots[0].state == "researching"
    assert not snapshot.slots[0].confirmed
    assert snapshot.slots[0].evidence_status == "historical"
    assert snapshot.slots[1].state == "owned_unread"
    assert all(j.state == "unknown" and j.observed_at is None for j in snapshot.slots[2:])
    assert all(j.state == "unknown" for j in LabRuntime(tmp_path, "OTHER").snapshot().slots)


def test_observations_without_a_bound_lease_remain_nonactionable(tmp_path: Path) -> None:
    from lab_runtime import LabRuntime
    snapshot = confirmed(LabRuntime(tmp_path, "ACCOUNT-A"))
    assert not snapshot.slots[0].confirmed
    assert snapshot.slots[0].evidence_status == "historical"


@pytest.mark.parametrize("field,value", [
    ("target_level", "87"), ("confirmed", "yes"), ("speed_multiplier", float("nan")),
    ("native_repeat", "guess"), ("remaining_s", -1), ("expected_finish", None),
])
def test_corrupt_persisted_job_cannot_be_actionable(tmp_path: Path, field: str, value: object) -> None:
    store = runtime(tmp_path)
    confirmed(store)
    data = json.loads(store.path.read_text())
    data["slots"][0][field] = value
    store.path.write_text(json.dumps(data))
    assert all(j.state == "unknown" for j in runtime(tmp_path).snapshot().slots)


def test_failed_write_does_not_advance_in_memory_and_next_frame_retries(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    reading = observation()
    store.observe(reading)
    with patch("lab_runtime.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            store.observe(replace(reading, observed_at=1001.))
    assert store.snapshot().slots_owned is None
    store.observe(replace(reading, observed_at=1002.))
    assert runtime(tmp_path).snapshot().slots_owned == 5


def test_strip_completeness_is_current_pair_provenance_not_history(tmp_path: Path) -> None:
    store = runtime(tmp_path)
    reading = observation()
    reading = replace(reading, slots_owned=1, jobs=(reading.jobs[0],))
    store.observe(reading)
    store.observe(replace(reading, observed_at=1001.))
    assert store.snapshot().strip_complete and store.snapshot().slots_owned == 1
    partial = replace(reading, slots_owned=None, slots_status="unknown")
    store.observe(replace(partial, observed_at=1002.))
    store.observe(replace(partial, observed_at=1003.))
    snapshot = store.snapshot()
    assert snapshot.observed_at == 1003. and snapshot.slots_owned == 1
    assert not snapshot.strip_complete
    store.observe(replace(reading, observed_at=1004.))
    store.observe(replace(reading, observed_at=1005.))
    assert store.snapshot().strip_complete
    assert not runtime(tmp_path).snapshot().strip_complete  # Disk is never live proof.


def test_lab_slots_file_is_historical_for_every_locked_or_owned_slot(tmp_path: Path) -> None:
    from lab_plan import LabCadence
    from lab_runtime import LabRuntime

    LabCadence(tmp_path, "ACCOUNT-A").note_slots({2: "owned", 3: "locked"}, 50, 1000.)
    snapshot = LabRuntime(tmp_path, "ACCOUNT-A").snapshot()
    assert [slot.state for slot in snapshot.slots[1:3]] == ["owned_unread", "locked"]
    assert all(slot.evidence_status == "historical" and not slot.confirmed
               for slot in snapshot.slots[1:3])
    assert snapshot.slots_owned == 2


def test_legacy_slot_two_file_alone_sets_slots_owned(tmp_path: Path) -> None:
    """No lab-slots.json yet: the legacy slot-2 file still proves slot 1 owned."""
    from lab_runtime import LabRuntime

    (tmp_path / "lab-slot2-cadence.json").write_text(json.dumps({
        "account_id": "ACCOUNT-A", "status": "locked", "observed_at": 1000.}))
    snapshot = LabRuntime(tmp_path, "ACCOUNT-A").snapshot()
    assert snapshot.slots[1].state == "locked"
    assert snapshot.slots_owned == 1


def test_a_job_seen_researching_outlives_the_idle_strip_of_a_later_generation(tmp_path: Path) -> None:
    """A job that finished while the bot was down keeps its target level on record."""
    from lab_runtime import LabRuntime
    before = confirmed(runtime(tmp_path)).slots[0]
    assert before.state == "researching" and before.research_id
    later = LabRuntime(tmp_path, "ACCOUNT-A", lease_id="lease-a", generation="worker-2")
    reading = observation(2000.)
    idle = replace(reading.jobs[0], status="idle", concept_id=None, raw_name="Lab Offline",
                   completes_at=None, remaining_s=None, source_level=None, target_level=None)
    reading = replace(reading, jobs=(idle, *reading.jobs[1:]))
    later.observe(reading)
    later.observe(replace(reading, observed_at=2001.))
    assert later.snapshot().slots[0].state == "idle"
    again = LabRuntime(tmp_path, "ACCOUNT-A", lease_id="lease-a", generation="worker-3").snapshot()
    assert (before.research_id, before.target_level, before.expected_finish) in again.job_history
    assert all(row[0] != before.research_id
               for row in runtime(tmp_path, "ACCOUNT-B").snapshot().job_history)



@pytest.mark.parametrize("confirmed,status,kept", [
    (True, "verified", True), (False, "verified", False), (True, "historical", False), (None, None, False)])
def test_job_history_comes_only_from_confirmed_researching_records(
        tmp_path: Path, confirmed: bool | None, status: str | None, kept: bool) -> None:
    """A legacy or unconfirmed row never becomes a completed level."""
    import hashlib
    import json
    from lab_runtime import LabRuntime, read_job_history
    scope = {"account_id": "ACCOUNT-A", "lease_id": "l", "generation": "g", "epoch": 0}
    row = {"scope": scope, "slot": 1, "state": "researching", "research_id": "labs.game-speed",
           "target_level": 4, "expected_finish": 50.}
    if confirmed is not None:
        row.update(confirmed=confirmed, evidence_status=status)
    path = tmp_path / f"lab-runtime-{hashlib.sha256(b'ACCOUNT-A').hexdigest()}.json"
    path.write_text(json.dumps({"version": 1, "scope": scope, "slots": [row],
                                "slots_owned": 1, "observed_at": 1.}))
    expected = (("labs.game-speed", 4, 50.),) if kept else ()
    assert read_job_history(tmp_path, "ACCOUNT-A") == expected
    assert LabRuntime(tmp_path, "ACCOUNT-A", generation="g2").snapshot().job_history == expected
