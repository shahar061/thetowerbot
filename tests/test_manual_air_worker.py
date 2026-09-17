"""A worker accepts only its live, exact manually selected BlueStacks instance."""

from pathlib import Path

import pytest

from bluestacks import HostCapabilityError, HostInstance
from fleet.manual_air_worker import ManualAirWorker


class Inventory:
    def __init__(self) -> None:
        self.rows = [HostInstance("Tiramisu64_20", "127.0.0.1:5755", "lease-20", "running"),
                     HostInstance("Tiramisu64_21", "127.0.0.1:5765", "lease-21", "running")]

    def instances(self) -> list[HostInstance]:
        return list(self.rows)


def test_manual_worker_exposes_only_exact_live_member(tmp_path: Path) -> None:
    inventory = Inventory()
    worker = ManualAirWorker("Tiramisu64_20", "127.0.0.1:5755", "lease-20",
                             inventory=inventory)

    assert [row.name for row in worker.inventory()] == ["Tiramisu64_20"]
    with pytest.raises(HostCapabilityError, match="manual pool"):
        worker.stage_clone("Tiramisu64_22", "Tiramisu64_20")


def test_manual_worker_refuses_stale_endpoint_or_lease(tmp_path: Path) -> None:
    inventory = Inventory()
    worker = ManualAirWorker("Tiramisu64_20", "127.0.0.1:5755", "lease-20",
                             inventory=inventory)

    inventory.rows[0] = HostInstance("Tiramisu64_20", "127.0.0.1:5755", "new-lease", "running")
    with pytest.raises(HostCapabilityError, match="identity changed"):
        worker.inventory()
    inventory.rows[0] = HostInstance("Tiramisu64_20", "127.0.0.1:5775", "lease-20", "running")
    with pytest.raises(HostCapabilityError, match="identity changed"):
        worker.inventory()
