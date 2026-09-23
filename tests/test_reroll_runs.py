"""Numbered reroll runs group pool members and never readmit a retired emulator."""

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

    def replace(self, keep: list[str], add: list[str]) -> None:
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


def make(root: Path, pool: FakePool, *, retire=None, stop_instance=None) -> RerollRuns:
    def default_retire(member):
        pool.remove(member["name"])
        return {"name": member["name"], "worker": "stopped", "instance": "stopped"}
    return RerollRuns(root, pool=pool, retire=retire or default_retire,
                      stop_instance=stop_instance or (lambda *args: None),
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


def test_migration_blocks_legacy_registered_workers(tmp_path: Path) -> None:
    _registration(tmp_path, "Tiramisu64_18", 1.0)
    _registration(tmp_path, "Tiramisu64_20", 2.0)
    runs = make(tmp_path, FakePool(["Tiramisu64_20"]))
    assert runs.retired_names() == {"Tiramisu64_18"}


def test_repair_adds_pool_members_missing_from_active_run(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20"])
    runs = make(tmp_path, pool)
    runs.active()
    pool.entries.append(FakePool._entry("Tiramisu64_21"))
    assert runs.active()["members"] == ["Tiramisu64_20", "Tiramisu64_21"]


def test_repair_records_members_that_left_the_pool_as_recovered(tmp_path: Path) -> None:
    pool = FakePool(["Tiramisu64_20", "Tiramisu64_21"])
    runs = make(tmp_path, pool)
    runs.active()
    pool.remove("Tiramisu64_21")
    active = runs.active()
    assert [(item["name"], item["reason"], item["instance"]) for item in active["retired"]] == [
        ("Tiramisu64_21", "recovered", "unknown")]
    assert "Tiramisu64_21" in runs.retired_names()


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
    assert runs.active()["retired"] == []
    runs.busy = False
    assert runs.active()["retired"][0]["name"] == "Tiramisu64_21"


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
