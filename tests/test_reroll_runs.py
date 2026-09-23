"""Numbered reroll runs group pool members; leaving a run only stops the bot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fleet.reroll_pool import RerollPoolError
from fleet.reroll_runs import RerollRuns, RerollRunsError, run_numbers_from


class FakePool:
    """Only the file-backed surface RerollRuns uses; no host inventory."""

    def __init__(self, names: list[str], *, fresh: set[str] | None = None) -> None:
        self.entries = [self._entry(name) for name in names]
        self.fresh = fresh if fresh is not None else set()
        self.opened: set[str] = set()   # fresh-looking but fails the boot probe

    @staticmethod
    def _entry(name: str) -> dict[str, str]:
        return {"name": name, "endpoint": f"127.0.0.1:{name[-2:]}", "lease_id": f"lease-{name}"}

    def members(self) -> list[dict[str, str]]:
        return [dict(item) for item in self.entries]

    def member(self, name: str):
        return next((dict(item) for item in self.entries if item["name"] == name), None)

    def validate_add(self, names: list[str]) -> None:
        if not names:
            raise RerollPoolError("invalid_pool_members")
        for name in names:
            if name in {item["name"] for item in self.entries}:
                raise RerollPoolError("instance_already_in_pool")
            if name not in self.fresh:
                raise RerollPoolError("tower_already_opened")

    def add(self, names: list[str]) -> None:
        self.validate_add(names)
        self.entries.extend(self._entry(name) for name in names)

    def prove(self, names: list[str]) -> set[str]:
        self.validate_add(names)
        if self.opened & set(names):
            raise RerollPoolError(f"tower_already_opened: {sorted(self.opened & set(names))[0]}")
        return set(names)

    def replace(self, keep: list[str], add: list[str], proven: set[str] | None = None) -> None:
        if add:
            self.validate_add(add)
        by_name = {item["name"]: item for item in self.entries}
        self.entries = [by_name[name] for name in keep] + [self._entry(name) for name in add]

    def remove(self, name: str) -> None:
        self.entries = [item for item in self.entries if item["name"] != name]


def _registration(root: Path, name: str, registered_at: float) -> None:
    worker = root / "workers" / name
    worker.mkdir(parents=True)
    (worker / "fleet-registration.json").write_text(json.dumps({"registered_at": registered_at}))


def make(root: Path, pool: FakePool, *, release=None, remove=None) -> RerollRuns:
    return RerollRuns(root, pool=pool,
                      release=release or (lambda member: {"name": member["name"],
                                                          "worker": "stopped"}),
                      remove=remove or (lambda member: {"name": member["name"],
                                                        "worker": "stopped", "instance": "stopped"}),
                      clock=lambda: "2026-09-23T09:00:00Z")


def test_migration_turns_existing_pool_into_active_reroll_one(tmp_path: Path) -> None:
    _registration(tmp_path, "Tiramisu64_20", 1_756_720_800.0)   # 2025-09-01T10:00:00Z
    runs = make(tmp_path, FakePool(["Tiramisu64_20", "Tiramisu64_21"]))

    active = runs.active()
    assert active is not None
    assert (active["number"], active["name"], active["status"]) == (1, "Reroll #1", "active")
    assert active["members"] == ["Tiramisu64_20", "Tiramisu64_21"]
    assert active["started_at"] == "2025-09-01T10:00:00Z"
    assert json.loads((tmp_path / "reroll-runs.json").read_text())["runs"][0]["number"] == 1


def test_migration_with_empty_pool_has_no_active_run(tmp_path: Path) -> None:
    runs = make(tmp_path, FakePool([]))
    assert runs.active() is None
    assert runs.summaries() == []


def test_migration_records_legacy_workers_without_blocking_them(tmp_path: Path) -> None:
    _registration(tmp_path, "Tiramisu64_18", 1.0)
    _registration(tmp_path, "Tiramisu64_20", 2.0)
    runs = make(tmp_path, FakePool(["Tiramisu64_20"], fresh={"Tiramisu64_18"}))
    runs.active()
    state = json.loads((tmp_path / "reroll-runs.json").read_text())
    assert state["retired_before_runs"] == ["Tiramisu64_18"]
    runs.add_members(["Tiramisu64_18"])
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_18"]


def test_repair_adds_pool_members_missing_from_active_run(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20"])
    runs = make(tmp_path, pool)
    runs.active()
    pool.entries.append(FakePool._entry("Tiramisu64_21"))
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_21"]


def test_repair_records_members_that_left_the_pool_as_removed(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    runs.active()
    pool.remove("Tiramisu64_21")
    active = runs.active()
    assert active["removed"] == ["Tiramisu64_21"] and active["retired"] == []


def test_repair_opens_a_run_for_a_pool_without_one(tmp_path: Path) -> None:
    pool = FakePool([])
    runs = make(tmp_path, pool)
    runs.active()
    pool.entries.append(FakePool._entry("Tiramisu64_22"))
    assert runs.active()["members"] == ["Tiramisu64_22"]


def test_repair_is_skipped_while_an_operation_is_in_progress(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    runs.active()
    runs.busy = True
    pool.remove("Tiramisu64_21")
    assert runs.active()["removed"] == []
    runs.busy = False
    assert runs.active()["removed"] == ["Tiramisu64_21"]


def test_unreadable_runs_file_is_an_explicit_error(tmp_path: Path) -> None:
    (tmp_path / "reroll-runs.json").write_text("{not json")
    with pytest.raises(RerollRunsError, match="runs_state_unreadable"):
        make(tmp_path, FakePool([])).active()


@pytest.mark.parametrize("invalid_data", [
    # Missing keys
    {"runs": [{"number": 1, "status": "active"}]},
    # members is a string instead of list
    {"runs": [{"number": 1, "name": "R1", "status": "active", "started_at": "2026-01-01T00:00:00Z",
              "members": "A_1", "retired": [], "retire_failed": {}}]},
    # Two runs with same number
    {"runs": [
        {"number": 1, "name": "R1", "status": "active", "started_at": "2026-01-01T00:00:00Z",
         "members": ["A_1"], "retired": [], "retire_failed": {}},
        {"number": 1, "name": "R2", "status": "closed", "started_at": "2026-01-02T00:00:00Z",
         "members": ["A_2"], "retired": [], "retire_failed": {}}
    ]},
    # retired_before_runs is not a list
    {"runs": [], "retired_before_runs": "x"},
])
def test_structurally_invalid_runs_file_raises_error(tmp_path: Path, invalid_data: dict) -> None:
    (tmp_path / "reroll-runs.json").write_text(json.dumps(invalid_data))
    with pytest.raises(RerollRunsError, match="runs_state_unreadable"):
        make(tmp_path, FakePool([])).active()


def test_run_numbers_from_maps_every_member_and_tolerates_a_missing_file(tmp_path: Path) -> None:
    assert run_numbers_from(tmp_path / "reroll-runs.json") == {}
    (tmp_path / "reroll-runs.json").write_text(json.dumps({"runs": [
        {"number": 1, "members": ["A_1", "A_2"]}, {"number": 2, "members": ["A_2"]}]}))
    assert run_numbers_from(tmp_path / "reroll-runs.json") == {"A_1": [1], "A_2": [1, 2]}
    (tmp_path / "reroll-runs.json").write_text("garbage")
    assert run_numbers_from(tmp_path / "reroll-runs.json") == {}


from fleet.reroll_retirement import RetireError


def _files(root: Path) -> tuple[bytes, ...]:
    return tuple((root / name).read_bytes() for name in ("reroll-runs.json",)
                 if (root / name).exists())


@pytest.mark.parametrize("keep, add, code", [
    (["Tiramisu64_99"], [], "keep_not_in_active_run"),
    ([], [], "run_would_be_empty"),
    ([], ["Tiramisu64_30"], "tower_already_opened"),
])
def test_validation_codes_change_nothing(tmp_path: Path, keep, add, code) -> None:
    _registration(tmp_path, "Tiramisu64_18", 1.0)
    pool = FakePool(["Tiramisu64_20"])
    runs = make(tmp_path, pool)
    runs.active()
    before, members = _files(tmp_path), pool.members()
    with pytest.raises(ValueError, match=code):
        runs.start_new(keep, add)
    assert _files(tmp_path) == before and pool.members() == members


def test_start_new_closes_old_run_stops_the_rest_and_opens_the_next(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_22"})
    released: list[str] = []

    def release(member):
        released.append(member["name"])
        return {"name": member["name"], "worker": "stopped"}

    runs = make(tmp_path, pool, release=release)
    runs.active()

    outcome = runs.start_new(["Tiramisu64_20"], ["Tiramisu64_22"])

    assert outcome["number"] == 2
    assert [item["name"] for item in pool.members()] == ["Tiramisu64_20", "Tiramisu64_22"]
    old, new = sorted(json.loads((tmp_path / "reroll-runs.json").read_text())["runs"],
                      key=lambda run: run["number"])
    assert old["status"] == "closed" and old["closed_at"] == "2026-09-23T09:00:00Z"
    assert released == ["Tiramisu64_21"]
    assert old["removed"] == ["Tiramisu64_21"] and old["retired"] == []
    assert outcome["results"] == [{"name": "Tiramisu64_21", "worker": "stopped"}]
    assert (new["name"], new["status"], new["members"]) == (
        "Reroll #2", "active", ["Tiramisu64_20", "Tiramisu64_22"])


def test_an_emulator_that_left_a_run_can_be_added_back(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_21", "Tiramisu64_22"})
    runs = make(tmp_path, pool)
    runs.start_new(["Tiramisu64_20"], ["Tiramisu64_22"])
    with pytest.raises(ValueError, match="keep_not_in_active_run"):
        runs.start_new(["Tiramisu64_21"], [])
    runs.add_members(["Tiramisu64_21"])
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_22", "Tiramisu64_21"]


def test_a_bot_that_would_not_stop_is_carried_over_and_flagged(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_22"})

    def release(member):
        raise RetireError("identity_changed")

    runs = make(tmp_path, pool, release=release)
    outcome = runs.start_new([], ["Tiramisu64_22"])
    active = runs.active()
    assert active["members"] == ["Tiramisu64_20", "Tiramisu64_21", "Tiramisu64_22"]
    assert runs.leave_failures() == {"Tiramisu64_20": "identity_changed",
                                      "Tiramisu64_21": "identity_changed"}
    assert {"name": "Tiramisu64_20", "error": "identity_changed"} in outcome["results"]


def test_start_new_without_active_run_needs_no_keep(tmp_path: Path) -> None:
    pool = FakePool([], fresh={"Tiramisu64_22"})
    runs = make(tmp_path, pool)
    assert runs.start_new([], ["Tiramisu64_22"])["number"] == 1
    assert runs.active()["members"] == ["Tiramisu64_22"]


def test_add_members_extends_active_run(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20"], fresh={"Tiramisu64_22"})
    runs = make(tmp_path, pool)
    runs.add_members(["Tiramisu64_22"])
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_22"]
    assert runs.active()["number"] == 1


def test_start_new_carries_unexpected_release_exceptions_as_failures(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_22"})

    def release(member):
        raise RuntimeError("boom")

    runs = make(tmp_path, pool, release=release)
    outcome = runs.start_new([], ["Tiramisu64_22"])
    active = runs.active()
    assert active["members"] == ["Tiramisu64_20", "Tiramisu64_21", "Tiramisu64_22"]
    assert runs.leave_failures() == {"Tiramisu64_20": "boom", "Tiramisu64_21": "boom"}
    assert {"name": "Tiramisu64_20", "error": "boom"} in outcome["results"]


def test_start_new_drops_a_member_that_left_the_pool_while_its_bot_stopped(
        tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_22"})

    def release(member):
        pool.remove(member["name"])
        raise RuntimeError("stopped but unconfirmed")

    runs = make(tmp_path, pool, release=release)
    runs.start_new(["Tiramisu64_20"], ["Tiramisu64_22"])
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_22"]
    assert runs.leave_failures() == {}


def test_busy_guard_blocks_overlapping_mutations(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    runs.active()
    before, members = _files(tmp_path), pool.members()

    runs.busy = True
    with pytest.raises(ValueError, match="reroll_start_in_progress"):
        runs.start_new(["Tiramisu64_20"], [])
    with pytest.raises(ValueError, match="reroll_start_in_progress"):
        runs.remove_member("Tiramisu64_20")
    with pytest.raises(ValueError, match="reroll_start_in_progress"):
        runs.add_members(["Tiramisu64_22"])
    runs.busy = False

    assert _files(tmp_path) == before and pool.members() == members


def test_start_new_drops_a_leaving_member_missing_from_the_pool_by_the_time_it_stops(
        tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_22"})
    runs = make(tmp_path, pool)
    runs.active()

    real_try_release = runs._try_release

    def fake_try_release(name: str):
        if name == "Tiramisu64_21":
            pool.remove("Tiramisu64_21")   # gone from the pool before we got to stop it
            return None, {"name": name, "error": "instance_not_in_pool"}
        return real_try_release(name)

    runs._try_release = fake_try_release
    outcome = runs.start_new(["Tiramisu64_20"], ["Tiramisu64_22"])

    assert outcome["number"] == 2
    assert [item["name"] for item in pool.members()] == ["Tiramisu64_20", "Tiramisu64_22"]


def test_start_new_keeps_leave_failure_flags_for_members_still_kept(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"],
                    fresh={"Tiramisu64_22", "Tiramisu64_23"})

    def release(member):
        if member["name"] == "Tiramisu64_20":
            raise RetireError("identity_changed")
        return {"name": member["name"], "worker": "stopped"}

    runs = make(tmp_path, pool, release=release)
    runs.start_new([], ["Tiramisu64_22"])
    assert runs.leave_failures() == {"Tiramisu64_20": "identity_changed"}

    runs.start_new(["Tiramisu64_20"], ["Tiramisu64_23"])
    assert runs.leave_failures() == {"Tiramisu64_20": "identity_changed"}
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_23"]


def test_removed_emulator_leaves_the_run_without_being_retired(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    runs.remove_member("Tiramisu64_21")
    assert pool.member("Tiramisu64_21") is None
    active = runs.active()   # re-loads, so repair must not record it as "recovered"
    assert active["retired"] == [] and active["removed"] == ["Tiramisu64_21"]
    with pytest.raises(ValueError, match="instance_not_in_active_run"):
        runs.remove_member("Tiramisu64_21")


def test_removed_emulator_can_be_added_back_to_the_same_run(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_21"})
    runs = make(tmp_path, pool)
    runs.remove_member("Tiramisu64_21")
    runs.add_members(["Tiramisu64_21"])
    active = runs.active()
    assert active["members"] == ["Tiramisu64_20", "Tiramisu64_21"]
    assert active["removed"] == []
    assert pool.member("Tiramisu64_21") is not None


def test_new_run_neither_stops_nor_keeps_a_removed_emulator(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    released: list[str] = []

    def release(member):
        released.append(member["name"])
        return {"name": member["name"], "worker": "stopped"}

    runs = make(tmp_path, pool, release=release)
    runs.remove_member("Tiramisu64_21")
    with pytest.raises(ValueError, match="keep_not_in_active_run"):
        runs.validate_new(["Tiramisu64_21"], [])
    runs.start_new(["Tiramisu64_20"], [])
    assert released == []
    assert runs.active()["members"] == ["Tiramisu64_20"]


def test_failed_removal_leaves_pool_and_run_untouched(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])

    def remove(member):
        raise RetireError("instance_stop_failed: window stuck")

    runs = make(tmp_path, pool, remove=remove)
    with pytest.raises(RetireError, match="instance_stop_failed"):
        runs.remove_member("Tiramisu64_21")
    assert pool.member("Tiramisu64_21") is not None
    assert runs.active()["removed"] == []
    assert not runs.busy


def test_repair_keeps_a_half_removed_emulator_in_play(tmp_path: Path) -> None:
    # Crash after the run recorded the removal but before the pool dropped it:
    # the pool is the truth, so the emulator stays in the run (paused).
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    state = runs._load()
    state["runs"][0]["removed"] = ["Tiramisu64_21"]
    runs._save(state)
    assert runs.active()["removed"] == []
    assert runs.active()["retired"] == []


def test_runs_file_without_removed_key_still_loads(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20"])
    runs = make(tmp_path, pool)
    state = runs._load()
    del state["runs"][0]["removed"]
    runs._save(state)
    assert runs.active()["members"] == ["Tiramisu64_20"]


def test_new_run_checks_additions_before_stopping_anyone(tmp_path: Path) -> None:
    # A rejected addition must leave the old run playing, with no bot stopped.
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_23"})
    pool.opened = {"Tiramisu64_23"}
    released: list[str] = []

    def release(member):
        released.append(member["name"])
        return {"name": member["name"], "worker": "stopped"}

    runs = make(tmp_path, pool, release=release)
    with pytest.raises(RerollPoolError, match="tower_already_opened: Tiramisu64_23"):
        runs.start_new(["Tiramisu64_20"], ["Tiramisu64_23"])
    assert released == []
    assert runs.active()["number"] == 1
    assert not runs.busy


def test_hidden_members_are_recorded_on_the_active_run_and_can_be_restored(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21", "Tiramisu64_22"])
    runs = make(tmp_path, pool)
    runs.set_hidden(["Tiramisu64_21", "Tiramisu64_22", "Tiramisu64_21"], True)
    assert runs.hidden_names() == {"Tiramisu64_21", "Tiramisu64_22"}
    assert runs.active()["hidden"] == ["Tiramisu64_21", "Tiramisu64_22"]
    runs.set_hidden(["Tiramisu64_22"], False)
    assert runs.hidden_names() == {"Tiramisu64_21"}
    # Hiding is only about the dashboard: the pool and the run keep the member.
    assert pool.member("Tiramisu64_21") is not None
    assert "Tiramisu64_21" in runs._live(runs.active())


def test_hiding_needs_a_live_member_of_the_active_run(tmp_path: Path) -> None:
    runs = make(tmp_path, FakePool([]))
    with pytest.raises(RerollRunsError, match="no_active_run"):
        runs.set_hidden(["Tiramisu64_20"], True)
    pool = FakePool(["Tiramisu64_20"])
    runs = make(tmp_path / "with-run", pool)
    with pytest.raises(RerollRunsError, match="instance_not_in_active_run"):
        runs.set_hidden(["Tiramisu64_99"], True)
    assert runs.hidden_names() == set()


def test_a_new_run_starts_with_nothing_hidden(tmp_path: Path) -> None:
    runs = make(tmp_path, FakePool(["Tiramisu64_20", "Tiramisu64_21"]))
    runs.set_hidden(["Tiramisu64_20"], True)
    runs.start_new(["Tiramisu64_20"], [])
    assert runs.hidden_names() == set()


def test_adding_a_removed_emulator_back_unhides_it(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"], fresh={"Tiramisu64_21"})
    runs = make(tmp_path, pool)
    runs.set_hidden(["Tiramisu64_21"], True)
    runs.remove_member("Tiramisu64_21")
    runs.add_members(["Tiramisu64_21"])
    assert runs.hidden_names() == set()


def test_runs_file_without_hidden_key_still_loads(tmp_path: Path) -> None:
    runs = make(tmp_path, FakePool(["Tiramisu64_20"]))
    state = runs._load()
    state["runs"][0].pop("hidden", None)
    runs._save(state)
    assert runs.hidden_names() == set()
