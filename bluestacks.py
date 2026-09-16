"""Named BlueStacks instances and fail-closed host lifecycle boundaries.

BlueStacks Air's installed GUI is not evidence of a supported automation API.
The manual pool is deliberately read only; operators provision and label its
bounded instances outside the bot and record their endpoint and lease here.
"""

from __future__ import annotations

import fcntl
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from device import EmulatorError, IdentityError
from fleet.identity import Attempt


logger = logging.getLogger(__name__)


class HostCapabilityError(EmulatorError):
    """The installed host has no proven API for this operation."""


class HostIdentityError(IdentityError):
    """Instance, endpoint, or lease ownership cannot be proven."""


@dataclass(frozen=True)
class HostInstance:
    name: str
    endpoint: str
    lease_id: str
    state: str
    source_lineage: str | None = None


class ProvisionMode(str, Enum):
    FRESH = "fresh"
    CLONE = "clone"


class HostDriver(Protocol):
    def inventory(self) -> list[HostInstance]: ...
    def start(self, name: str) -> None: ...
    def stop(self, name: str) -> None: ...
    def create_fresh(self, name: str) -> HostInstance: ...
    def stage_clone(self, name: str, source: str) -> HostInstance: ...


class ManualPool:
    """Read-only, operator provisioned pool. No inferred host commands."""

    supports_lifecycle = False
    supports_fresh_provision = False
    supports_clone_staging = False
    supports_automatic_replenish = False

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def inventory(self) -> list[HostInstance]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload["instances"]
            if not isinstance(rows, list):
                raise ValueError("instances must be a list")
            instances = [HostInstance(**row) for row in rows]
            if any(not all((item.name, item.endpoint, item.lease_id))
                   or item.state not in {"running", "stopped"} for item in instances):
                raise ValueError("incomplete instance record")
            if len({item.name for item in instances}) != len(instances):
                raise ValueError("duplicate instance name")
            return instances
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise HostIdentityError("manual BlueStacks pool inventory is invalid") from exc

    def start(self, name: str) -> None:
        raise HostCapabilityError("manual pool: start requires operator action")

    def stop(self, name: str) -> None:
        raise HostCapabilityError("manual pool: stop requires operator action")

    def create_fresh(self, name: str) -> HostInstance:
        raise HostCapabilityError("manual pool: provision a fresh instance and add it to inventory")

    def stage_clone(self, name: str, source: str) -> HostInstance:
        raise HostCapabilityError("manual pool: clone staging has no proven host API")


class BlueStacksAdapter:
    """Thin host boundary; mutating methods are supplied by a proven driver."""

    def __init__(self, driver: HostDriver, *, staging_root: Path) -> None:
        self.driver = driver
        self.staging_root = Path(staging_root)
        self._staging_handle: Any | None = None

    def inventory(self) -> list[HostInstance]:
        return self.driver.inventory()

    @property
    def supports_lifecycle(self) -> bool:
        return getattr(self.driver, "supports_lifecycle", False) is True

    def designated(self, name: str, attempt: Attempt) -> HostInstance:
        instances = self.inventory()
        matches = [instance for instance in instances if instance.name == name]
        if len(matches) != 1:
            raise HostIdentityError("designated BlueStacks instance is missing or duplicated")
        instance = matches[0]
        if instance.endpoint != attempt.endpoint or instance.lease_id != attempt.lease_id:
            raise HostIdentityError("designated BlueStacks endpoint or lease changed")
        if sum(item.endpoint == attempt.endpoint for item in instances) != 1:
            raise HostIdentityError("duplicate BlueStacks endpoint in inventory")
        return instance

    def start(self, name: str, attempt: Attempt) -> None:
        self.designated(name, attempt)
        if not self.supports_lifecycle:
            raise HostCapabilityError("named BlueStacks start has no proven host API")
        self.driver.start(name)

    def stop(self, name: str, attempt: Attempt) -> None:
        self.designated(name, attempt)
        if not self.supports_lifecycle:
            raise HostCapabilityError("named BlueStacks stop has no proven host API")
        self.driver.stop(name)

    def provision(self, name: str, *, mode: ProvisionMode = ProvisionMode.FRESH,
                  source: str | None = None) -> HostInstance:
        if not name.strip() or any(item.name == name for item in self.inventory()):
            raise HostIdentityError("new instance name must be unique")
        if mode is ProvisionMode.FRESH:
            if source is not None:
                raise ValueError("fresh provisioning cannot use a source")
            if getattr(self.driver, "supports_fresh_provision", False) is not True:
                raise HostCapabilityError("manual pool: fresh provisioning requires operator action")
            return self.driver.create_fresh(name)
        if mode is not ProvisionMode.CLONE or not source:
            raise ValueError("clone staging needs a named source")
        if self._staging_handle is None:
            raise HostCapabilityError("clone staging requires an exclusive staging lease")
        if getattr(self.driver, "supports_clone_staging", False) is not True:
            raise HostCapabilityError("clone staging has no proven host API")
        if not any(item.name == source for item in self.inventory()):
            raise HostIdentityError("clone source is missing")
        return self.driver.stage_clone(name, source)

    @contextmanager
    def staging_lease(self) -> Iterator[None]:
        self.staging_root.mkdir(parents=True, exist_ok=True)
        with (self.staging_root / ".bluestacks-staging.lock").open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostCapabilityError("BlueStacks staging lease is already held") from None
            self._staging_handle = handle
            try:
                yield
            finally:
                self._staging_handle = None
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class HostBoundConnect:
    """Wait for the exact named instance and use only its leased ADB endpoint."""

    def __init__(
        self, adapter: BlueStacksAdapter, name: str, attempt: Attempt,
        connect: Callable[[], Any], *, timeout: float = 10.0,
        poll_interval: float = 0.5, restart_after: int = 2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        before_connect: Callable[[], None] | None = None,
    ) -> None:
        if timeout < 0 or poll_interval <= 0 or restart_after < 1:
            raise ValueError("bounded host recovery settings required")
        self.adapter, self.name, self.attempt, self.connect = adapter, name, attempt, connect
        self.timeout, self.poll_interval = timeout, poll_interval
        self.restart_after, self.clock, self.sleep = restart_after, clock, sleep
        self._failed_connects = 0
        self._host_restart_used = False
        self.before_connect = before_connect

    def __call__(self) -> Any:
        instance = self.adapter.designated(self.name, self.attempt)
        if instance.state == "stopped":
            self.adapter.start(self.name, self.attempt)
            self._host_restart_used = True
        elif instance.state != "running":
            raise HostIdentityError("designated BlueStacks instance state is unknown")
        elif (self.adapter.supports_lifecycle and self._failed_connects >= self.restart_after - 1
              and not self._host_restart_used):
            self.adapter.stop(self.name, self.attempt)
            self.adapter.start(self.name, self.attempt)
            self._host_restart_used = True
        if self.before_connect is not None:
            try:
                self.before_connect()
            except Exception:
                logger.warning("BlueStacks host popup check unavailable for %s", self.name,
                               exc_info=True)
        deadline = self.clock() + self.timeout
        while True:
            instance = self.adapter.designated(self.name, self.attempt)
            if instance.state == "running":
                try:
                    device = self.connect()
                    if getattr(device, "serial", None) != self.attempt.endpoint:
                        raise HostIdentityError("connected ADB device differs from designated instance")
                    self.adapter.designated(self.name, self.attempt)
                    self._failed_connects = 0
                    return device
                except HostIdentityError:
                    raise
                except Exception:
                    self._failed_connects += 1
                    if self.clock() >= deadline:
                        raise
            if self.clock() >= deadline:
                raise EmulatorError("designated BlueStacks endpoint unavailable")
            self.sleep(min(self.poll_interval, deadline - self.clock()))
