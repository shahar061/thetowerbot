"""Exact live host binding for one operator-provisioned BlueStacks Air worker."""

from __future__ import annotations

import time
from typing import Any, Callable

from bluestacks import HostCapabilityError, HostInstance
from fleet.bluestacks_air import (
    _REOBSERVABLE, BlueStacksAirInventory, MacOSMultiInstanceManager, ManagerRowObservation,
    _adb_endpoint_present,
)
import ocr


class ManualAirWorker:
    supports_lifecycle = True
    supports_fresh_provision = False
    supports_clone_staging = False
    _PRESS_RETRIES = 2
    _RETRY_SETTLE_SECONDS = .5
    # Two pre-press observations disagreed (e.g. the Manager moved); nothing was pressed.
    _REOBSERVABLE = _REOBSERVABLE + ("manual pool lifecycle evidence changed",)

    def __init__(self, name: str, endpoint: str, lease_id: str, *,
                 inventory: Any, manager: Any | None = None,
                 endpoint_present: Callable[[str], bool] = _adb_endpoint_present,
                 timeout: float = 120.) -> None:
        self.name = name
        self.endpoint = endpoint
        self.lease_id = lease_id
        self.inventory_source = inventory
        self.manager = manager if manager is not None else MacOSMultiInstanceManager()
        self.endpoint_present = endpoint_present
        self.timeout = timeout

    def inventory(self) -> list[HostInstance]:
        try:
            rows = self.inventory_source.instances()
        except Exception as exc:
            raise HostCapabilityError("manual pool live inventory unavailable") from exc
        matching = [row for row in rows if row.name == self.name]
        if (len(matching) != 1 or matching[0].endpoint != self.endpoint
                or matching[0].lease_id != self.lease_id
                or sum(row.endpoint == self.endpoint for row in rows) != 1):
            raise HostCapabilityError("manual pool host identity changed")
        row = matching[0]
        return [HostInstance(row.name, row.endpoint, row.lease_id, row.state,
                             f"manual:{row.lease_id}")]

    def _control(self, action: str) -> Any:
        display_name = getattr(self.inventory_source, "display_name", None)
        label = display_name(self.name) if callable(display_name) else self.name
        return ManagerRowObservation(self.manager, ocr.read).row_control(label, action=action)

    def _change_state(self, *, action: str, before: str, after: str) -> None:
        if self.timeout <= 0:
            raise ValueError("manual worker lifecycle requires a bounded timeout")
        for attempt in range(self._PRESS_RETRIES + 1):
            try:
                self._press(action=action, before=before)
                break
            except HostCapabilityError as exc:
                # Every step before the click only observes, and the press revalidates
                # the window before clicking, so a moving Manager means nothing was pressed.
                if not str(exc).startswith(self._REOBSERVABLE) or attempt == self._PRESS_RETRIES:
                    raise
                time.sleep(self._RETRY_SETTLE_SECONDS)
        deadline = time.monotonic() + self.timeout
        while True:
            current = self.inventory()[0]
            if current.state == after and self.endpoint_present(self.endpoint) == (after == "running"):
                try:
                    self._control("Stop" if after == "running" else "Start")
                    return
                except HostCapabilityError as exc:
                    if not str(exc).startswith(self._REOBSERVABLE):
                        raise
            if time.monotonic() >= deadline:
                raise HostCapabilityError("manual pool lifecycle postcondition was not proven")
            time.sleep(.2)

    def _press(self, *, action: str, before: str) -> None:
        self.manager.activate()
        row = self.inventory()[0]
        if row.state != before or self.endpoint_present(self.endpoint) != (before == "running"):
            raise HostCapabilityError("manual pool lifecycle precondition changed")
        control = self._control(action)
        again = self.inventory()[0]
        if (again != row or self._control(action) != control
                or self.endpoint_present(self.endpoint) != (before == "running")):
            raise HostCapabilityError("manual pool lifecycle evidence changed")
        self.manager.press(control.window_id, control.point, action)

    def start(self, name: str) -> None:
        if name != self.name:
            raise HostCapabilityError("manual pool exact instance required")
        self._change_state(action="Start", before="stopped", after="running")

    def stop(self, name: str) -> None:
        if name != self.name:
            raise HostCapabilityError("manual pool exact instance required")
        self._change_state(action="Stop", before="running", after="stopped")

    def create_fresh(self, name: str) -> HostInstance:
        raise HostCapabilityError("manual pool: instance creation is unavailable")

    def stage_clone(self, name: str, source: str) -> HostInstance:
        raise HostCapabilityError("manual pool: cloning is unavailable")
