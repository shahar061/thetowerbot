"""A manually prepared emulator cannot enter Reroll without host identity checks."""

from pathlib import Path

import pytest

from bluestacks import HostInstance
from fleet.reroll_pool import RerollPool, RerollPoolError


def test_pool_persists_exact_installed_members_and_excludes_template(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_6", "127.0.0.1:5615", "source", "running"),
            HostInstance("Tiramisu64_20", "127.0.0.1:5755", "worker", "running")]
    pool = RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: {"Tiramisu64_6"})

    with pytest.raises(RerollPoolError, match="protected_template"):
        pool.add(["Tiramisu64_6"])
    pool.add(["Tiramisu64_20"])
    assert pool.members()[0]["endpoint"] == "127.0.0.1:5755"
    assert RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: {"Tiramisu64_6"}).snapshot()["members"] == \
        pool.snapshot()["members"]


def test_pool_rejects_opened_tower_and_changed_host_identity(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "first", "running")]
    package = {"state": "opened"}
    pool = RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: package["state"],
                      protected_names=lambda: set())

    with pytest.raises(RerollPoolError, match="tower_already_opened"):
        pool.add(["Tiramisu64_20"])
    package["state"] = "installed_unopened"
    pool.add(["Tiramisu64_20"])
    rows[0] = HostInstance("Tiramisu64_20", "127.0.0.1:5755", "second", "running")
    assert pool.snapshot()["members"][0]["state"] == "identity_changed"


def test_pool_reports_stopped_instance_as_needing_start(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "lease", "stopped")]
    pool = RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: set())

    assert pool.snapshot()["candidates"][0]["state"] == "start_required"
    pool.add(["Tiramisu64_20"])
    assert pool.snapshot()["members"][0]["state"] == "start_required"


def test_registered_running_member_skips_repeated_package_probe(tmp_path: Path) -> None:
    row = HostInstance("Tiramisu64_20", "127.0.0.1:5755", "lease", "running")
    calls: list[str] = []
    pool = RerollPool(tmp_path, inventory=lambda: [row],
        package_state=lambda endpoint: calls.append(endpoint) or "installed_unopened",
        protected_names=lambda: set(), registered=lambda observed: observed == row)
    pool.add([row.name])
    calls.clear()
    assert pool.snapshot()["members"][0]["state"] == "tower_already_opened"
    assert calls == []


def _pool(tmp_path: Path, rows: list[HostInstance]) -> RerollPool:
    return RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: set())


def test_replace_keeps_exact_entries_and_appends_validated_new_members(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "a", "running"),
            HostInstance("Tiramisu64_21", "127.0.0.1:5765", "b", "running"),
            HostInstance("Tiramisu64_22", "127.0.0.1:5775", "c", "stopped")]
    pool = _pool(tmp_path, rows)
    pool.add(["Tiramisu64_20", "Tiramisu64_21"])
    kept_before = pool.member("Tiramisu64_20")

    pool.replace(["Tiramisu64_20"], ["Tiramisu64_22"])

    assert [item["name"] for item in pool.members()] == ["Tiramisu64_20", "Tiramisu64_22"]
    assert pool.member("Tiramisu64_20") == kept_before
    assert pool.member("Tiramisu64_21") is None


def test_replace_rejects_unknown_keep_empty_result_and_leaves_file_unchanged(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "a", "running")]
    pool = _pool(tmp_path, rows)
    pool.add(["Tiramisu64_20"])
    before = (tmp_path / "reroll-pool.json").read_bytes()

    with pytest.raises(RerollPoolError, match="instance_not_in_pool"):
        pool.replace(["Tiramisu64_99"], [])
    with pytest.raises(RerollPoolError, match="run_would_be_empty"):
        pool.replace([], [])
    with pytest.raises(RerollPoolError, match="instance_already_in_pool"):
        pool.replace([], ["Tiramisu64_20"])
    assert (tmp_path / "reroll-pool.json").read_bytes() == before


def test_remove_and_replace_succeed_even_when_the_next_inventory_read_fails(tmp_path: Path) -> None:
    """remove/replace must not lose their already-saved write to a later host
    read failing. Before the fix both ended with `return self.snapshot()`,
    which re-reads the host inventory *after* the file was written; a failure
    there raised RerollPoolError even though membership had already changed
    on disk."""
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "a", "running"),
            HostInstance("Tiramisu64_21", "127.0.0.1:5765", "b", "running"),
            HostInstance("Tiramisu64_22", "127.0.0.1:5775", "c", "running"),
            HostInstance("Tiramisu64_23", "127.0.0.1:5785", "d", "running")]
    pool = RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: set())
    pool.add(["Tiramisu64_20", "Tiramisu64_21", "Tiramisu64_22"])

    # remove() touches no host inventory at all, so it must succeed outright
    # even once every further inventory read raises.
    pool.inventory = _raise_after_first_call(rows)
    pool.remove("Tiramisu64_22")
    assert [item["name"] for item in pool.members()] == ["Tiramisu64_20", "Tiramisu64_21"]
    assert RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: set()).members() == pool.members()

    # replace() with a nonempty `add` needs exactly one inventory read to
    # validate the new member; the old trailing snapshot() call would have
    # been a second, failing read after the save already landed.
    pool.inventory = _raise_after_first_call(rows)
    pool.replace(["Tiramisu64_20"], ["Tiramisu64_23"])
    assert [item["name"] for item in pool.members()] == ["Tiramisu64_20", "Tiramisu64_23"]
    assert RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: set()).members() == pool.members()


def _raise_after_first_call(rows: list[HostInstance]):
    calls = {"count": 0}

    def inventory() -> list[HostInstance]:
        calls["count"] += 1
        if calls["count"] > 1:
            raise RuntimeError("host busy")
        return rows
    return inventory


def test_validate_add_matches_add_without_writing(tmp_path: Path) -> None:
    rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "a", "running")]
    pool = RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "opened",
                      protected_names=lambda: set())
    with pytest.raises(RerollPoolError, match="tower_already_opened"):
        pool.validate_add(["Tiramisu64_20"])
    with pytest.raises(RerollPoolError, match="invalid_pool_members"):
        pool.validate_add([])
    assert not (tmp_path / "reroll-pool.json").exists()
