"""Named BlueStacks host contracts using a simulated host and ADB transport."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bluestacks import (
    BlueStacksAdapter, HostBoundConnect, HostCapabilityError, HostIdentityError,
    HostInstance, ManualPool, ProvisionMode,
)
from fleet.identity import Attempt
from supervisor import DeviceSupervisor, RecoveryState


class FakeHost:
    supports_lifecycle = True
    supports_fresh_provision = True
    supports_clone_staging = True

    def __init__(self) -> None:
        self.instances = [HostInstance("alpha", "127.0.0.1:5555", "lease-a", "running"),
                          HostInstance("beta", "127.0.0.1:5557", "lease-b", "running")]
        self.calls: list[tuple[str, str]] = []

    def inventory(self) -> list[HostInstance]:
        return list(self.instances)

    def start(self, name: str) -> None:
        self.calls.append(("start", name))
        self.instances[0] = HostInstance("alpha", "127.0.0.1:5555", "lease-a", "running")

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))

    def create_fresh(self, name: str) -> HostInstance:
        self.calls.append(("fresh", name))
        return HostInstance(name, "127.0.0.1:5559", "lease-c", "stopped")

    def stage_clone(self, name: str, source: str) -> HostInstance:
        self.calls.append(("clone", source))
        return HostInstance(name, "127.0.0.1:5559", "lease-c", "stopped")


def test_provision_defaults_to_fresh_and_clone_requires_exclusive_staging_lease(tmp_path: Path) -> None:
    host = FakeHost()
    adapter = BlueStacksAdapter(host, staging_root=tmp_path)
    assert adapter.provision("gamma").name == "gamma"
    assert host.calls == [("fresh", "gamma")]
    with pytest.raises(HostCapabilityError):
        adapter.provision("delta", source="alpha", mode=ProvisionMode.CLONE)
    with adapter.staging_lease():
        adapter.provision("delta", source="alpha", mode=ProvisionMode.CLONE)
        with pytest.raises(HostCapabilityError, match="staging lease"):
            with BlueStacksAdapter(host, staging_root=tmp_path).staging_lease():
                pass
    assert host.calls[-1] == ("clone", "alpha")


def test_shared_staging_leases_coexist_but_exclude_clone_staging(tmp_path: Path) -> None:
    host = FakeHost()
    first, second = (BlueStacksAdapter(host, staging_root=tmp_path) for _ in range(2))
    with first.staging_lease(shared=True), second.staging_lease(shared=True):
        with pytest.raises(HostCapabilityError, match="clone staging requires"):
            first.provision("delta", source="alpha", mode=ProvisionMode.CLONE)
        with pytest.raises(HostCapabilityError, match="staging lease"):
            with BlueStacksAdapter(host, staging_root=tmp_path).staging_lease():
                pass
    with first.staging_lease():
        with pytest.raises(HostCapabilityError, match="staging lease"):
            with second.staging_lease(shared=True):
                pass


def test_manual_pool_is_bounded_and_reports_unavailable_host_automation(tmp_path: Path) -> None:
    path = tmp_path / "pool.json"
    path.write_text(json.dumps({"instances": [
        {"name": "alpha", "endpoint": "127.0.0.1:5555", "lease_id": "lease-a", "state": "running"},
    ]}), encoding="utf-8")
    adapter = BlueStacksAdapter(ManualPool(path), staging_root=tmp_path)
    assert adapter.inventory()[0].name == "alpha"
    with pytest.raises(HostCapabilityError, match="manual"):
        adapter.provision("new")
    with pytest.raises(HostCapabilityError):
        adapter.start("alpha", Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a"))


def test_connector_binds_exact_name_endpoint_and_lease_and_restarts_only_that_instance(tmp_path: Path) -> None:
    host = FakeHost()
    adapter = BlueStacksAdapter(host, staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    calls = 0

    def connect() -> SimpleNamespace:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("ADB lost")
        return SimpleNamespace(serial="127.0.0.1:5555")

    connector = HostBoundConnect(adapter, "alpha", attempt, connect,
                                 timeout=0, restart_after=2)
    with pytest.raises(ConnectionError):
        connector()
    assert connector().serial == attempt.endpoint
    assert host.calls == [("stop", "alpha"), ("start", "alpha")]
    host.instances[0] = HostInstance("alpha", attempt.endpoint, "other-lease", "running")
    with pytest.raises(HostIdentityError):
        connector()
    assert calls == 2


def test_connector_does_not_adopt_another_instance_at_same_endpoint(tmp_path: Path) -> None:
    host = FakeHost()
    host.instances[1] = HostInstance("beta", "127.0.0.1:5555", "lease-b", "running")
    adapter = BlueStacksAdapter(host, staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    with pytest.raises(HostIdentityError, match="duplicate"):
        HostBoundConnect(adapter, "alpha", attempt,
                         lambda: SimpleNamespace(serial=attempt.endpoint), timeout=0)()


def test_host_popup_check_runs_after_exact_binding_and_before_adb_connect(tmp_path: Path) -> None:
    adapter = BlueStacksAdapter(FakeHost(), staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    events: list[str] = []
    connector = HostBoundConnect(
        adapter, "alpha", attempt,
        lambda: (events.append("connect"), SimpleNamespace(serial=attempt.endpoint))[1],
        before_connect=lambda: events.append("popup"), timeout=0,
    )
    assert connector().serial == attempt.endpoint
    assert events == ["popup", "connect"]

    wrong = Attempt.new("worker-a", "127.0.0.1:5555", "wrong", "run-a")
    with pytest.raises(HostIdentityError):
        HostBoundConnect(adapter, "alpha", wrong,
                         lambda: pytest.fail("connected before identity check"),
                         before_connect=lambda: pytest.fail("popup before identity check"))()


def test_unavailable_host_popup_check_does_not_block_adb_connection(tmp_path: Path) -> None:
    adapter = BlueStacksAdapter(FakeHost(), staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    def unavailable() -> None:
        raise RuntimeError("Screen Recording permission missing")
    connector = HostBoundConnect(adapter, "alpha", attempt,
                                 lambda: SimpleNamespace(serial=attempt.endpoint),
                                 before_connect=unavailable, timeout=0)
    assert connector().serial == attempt.endpoint


def test_host_mutations_require_designated_endpoint_and_lease(tmp_path: Path) -> None:
    host = FakeHost()
    adapter = BlueStacksAdapter(host, staging_root=tmp_path)
    wrong = Attempt.new("worker-a", "127.0.0.1:5555", "other-lease", "run-a")
    with pytest.raises(HostIdentityError):
        adapter.stop("alpha", wrong)
    with pytest.raises(HostIdentityError):
        adapter.start("alpha", wrong)
    assert host.calls == []


def test_named_instance_rejects_adb_alias_as_an_unproven_binding(tmp_path: Path) -> None:
    adapter = BlueStacksAdapter(FakeHost(), staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    with pytest.raises(HostIdentityError):
        HostBoundConnect(adapter, "alpha", attempt,
                         lambda: SimpleNamespace(serial="emulator-5554"), timeout=0)()


def test_crashed_named_instance_starts_then_supervisor_relaunches_only_its_game(tmp_path: Path) -> None:
    host = FakeHost()
    host.instances[0] = HostInstance("alpha", "127.0.0.1:5555", "lease-a", "stopped")
    adapter = BlueStacksAdapter(host, staging_root=tmp_path)
    attempt = Attempt.new("worker-a", "127.0.0.1:5555", "lease-a", "run-a")
    launched: list[str] = []
    taps: list[tuple[int, int]] = []
    device = SimpleNamespace(
        serial=attempt.endpoint,
        app_current=lambda: SimpleNamespace(package="launcher"),
        app_start=lambda package: launched.append(package),
        click=lambda x, y: taps.append((x, y)),
    )
    connector = HostBoundConnect(adapter, "alpha", attempt, lambda: device, timeout=0)
    supervisor = DeviceSupervisor(
        path=tmp_path / "supervisor.json", endpoint=attempt.endpoint,
        connect=connector, expected_account="account-a",
        game_package="com.example.tower", quarantine_on_exhaustion=True,
    )
    assert supervisor.recover() is RecoveryState.BLOCKED
    assert host.calls == [("start", "alpha")]
    assert launched == ["com.example.tower"]
    assert supervisor.status().reason == "game_relaunched"
    with pytest.raises(RuntimeError):
        supervisor.tap(1, 2)
    assert taps == []
