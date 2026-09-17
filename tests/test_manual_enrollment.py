"""Manual enrollment may never touch the protected source or a changed host."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from fleet.identity import Attempt
from fleet.identity import IdentityEvidence
from fleet.manual_enrollment import enroll_manual_instance
from fleet.runtime import WorkerRuntime
from bluestacks import HostInstance


def test_protected_template_rejected_before_any_host_access(tmp_path: Path) -> None:
    member = {"name": "Tiramisu64_6", "endpoint": "127.0.0.1:5615", "lease_id": "lease"}
    attempt = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "job")
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10006)

    with pytest.raises(ValueError, match="manual_instance_identity_invalid"):
        enroll_manual_instance(root=tmp_path, member=member, runtime=runtime,
                               attempt=attempt, inventory=None,
                               connect=lambda endpoint: pytest.fail("host was contacted"),
                               protected_names={"Tiramisu64_6"})
    assert not runtime.root.exists()


def test_endpoint_change_rejected_before_any_host_access(tmp_path: Path) -> None:
    member = {"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "lease"}
    attempt = Attempt.new(member["name"], "127.0.0.1:5756", member["lease_id"], "job")
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10020)

    with pytest.raises(ValueError, match="manual_instance_identity_invalid"):
        enroll_manual_instance(root=tmp_path, member=member, runtime=runtime,
                               attempt=attempt, inventory=None,
                               connect=lambda endpoint: pytest.fail("host was contacted"),
                               protected_names=set())
    assert not runtime.root.exists()


def test_verified_first_launch_persists_exact_worker_registration(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.manual_enrollment as module

    member = {"name": "Tiramisu64_20", "endpoint": "127.0.0.1:5755", "lease_id": "lease"}
    attempt = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "job")
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10020)
    row = HostInstance(member["name"], member["endpoint"], member["lease_id"],
                       "running", f"manual:{member['lease_id']}")
    monkeypatch.setattr(module, "ManualAirWorker", lambda *args, **kwargs:
                        SimpleNamespace(inventory=lambda: [row]))
    monkeypatch.setattr(module, "tower_is_unopened", lambda device: True)
    device = SimpleNamespace(serial=member["endpoint"],
                             app_info=lambda package: SimpleNamespace(version_name="29.0.3"))

    def first_launch(**kwargs: object) -> dict:
        assert kwargs["instance"] == member["name"]
        assert kwargs["source_lineage"] == "manual:lease"
        attempt.persist(runtime.checkpoint_root / f"{attempt.generation}.json",
                        IdentityEvidence("123456", attempt.created_at + 1, "capture"))
        return {"account_id": "123456"}

    monkeypatch.setattr(module, "create_first_launch_account", first_launch)
    monkeypatch.setattr(module, "probe_clone_worker", lambda **kwargs: {
        "evidence_ref": "restart-capture"})

    result = enroll_manual_instance(root=tmp_path, member=member, runtime=runtime,
        attempt=attempt, inventory=None, connect=lambda endpoint: device,
        protected_names={"Tiramisu64_6"})
    assert result["account_id"] == "123456"
    assert result["evidence_ref"] == "restart-capture"
    assert (runtime.root / "fleet-registration.json").exists()
