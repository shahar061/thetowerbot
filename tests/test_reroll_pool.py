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
    added = pool.add(["Tiramisu64_20"])
    assert added["members"][0]["endpoint"] == "127.0.0.1:5755"
    assert RerollPool(tmp_path, inventory=lambda: rows,
                      package_state=lambda endpoint: "installed_unopened",
                      protected_names=lambda: {"Tiramisu64_6"}).snapshot()["members"] == added["members"]


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
    assert pool.add(["Tiramisu64_20"])["members"][0]["state"] == "start_required"


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
