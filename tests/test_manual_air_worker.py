"""A worker accepts only its live, exact manually selected BlueStacks instance."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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


class LifecycleWorker(ManualAirWorker):
    """Scripted Manager: ``controls`` yields each row observation or the error it raises."""

    def __init__(self, inventory: Inventory, controls: list[object]) -> None:
        self.pressed: list[tuple[object, ...]] = []

        def press(*args: object) -> None:
            self.pressed.append(args)
            inventory.rows[0] = replace(inventory.rows[0], state="running")

        super().__init__("Tiramisu64_20", "127.0.0.1:5755", "lease-20", inventory=inventory,
                         manager=SimpleNamespace(activate=lambda: None, press=press),
                         endpoint_present=lambda _endpoint: inventory.rows[0].state == "running")
        self.controls = controls

    def _control(self, action: str) -> object:
        step = self.controls.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def stopped() -> Inventory:
    inventory = Inventory()
    inventory.rows[0] = HostInstance("Tiramisu64_20", "127.0.0.1:5755", "lease-20", "stopped")
    return inventory


def test_manual_start_reobserves_when_the_manager_moves_before_the_press(
        stopped: Inventory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    control = type("Control", (), {"window_id": 41, "point": (840, 310)})()
    worker = LifecycleWorker(stopped, [
        HostCapabilityError("manager window changed: id,x,y,w,h 41,0,0,9,9 -> 41,0,0,9,10"),
        control, control, "stop-visible",
    ])

    worker.start("Tiramisu64_20")

    assert worker.pressed == [(41, (840, 310), "Start")]


def test_manual_start_tolerates_a_moving_manager_while_confirming(
        stopped: Inventory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    control = type("Control", (), {"window_id": 41, "point": (840, 310)})()
    worker = LifecycleWorker(stopped, [
        control, control, HostCapabilityError("manager window changed"), "stop-visible",
    ])

    worker.start("Tiramisu64_20")

    assert worker.pressed == [(41, (840, 310), "Start")]
    assert worker.controls == []


def test_manual_start_does_not_retry_an_unrelated_refusal(
        stopped: Inventory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    worker = LifecycleWorker(stopped, [
        HostCapabilityError("BlueStacks Air manager row is missing or ambiguous"),
    ])

    with pytest.raises(HostCapabilityError, match="missing or ambiguous"):
        worker.start("Tiramisu64_20")

    assert worker.pressed == []
