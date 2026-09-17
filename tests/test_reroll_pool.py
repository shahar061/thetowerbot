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
