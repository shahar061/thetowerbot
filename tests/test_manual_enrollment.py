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


def _opened_tower(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import fleet.manual_enrollment as module

    member = {"name": "Tiramisu64_35", "endpoint": "127.0.0.1:5905", "lease_id": "lease"}
    runtime = WorkerRuntime.for_worker(tmp_path / "workers", member["name"], 10035)
    row = HostInstance(member["name"], member["endpoint"], member["lease_id"],
                       "running", f"manual:{member['lease_id']}")
    monkeypatch.setattr(module, "ManualAirWorker", lambda *args, **kwargs:
                        SimpleNamespace(inventory=lambda: [row]))
    monkeypatch.setattr(module, "tower_is_unopened", lambda device: False)
    monkeypatch.setattr(module, "create_first_launch_account", lambda **kwargs:
                        pytest.fail("an opened Tower must never get a second first launch"))
    device = SimpleNamespace(serial=member["endpoint"],
                             app_info=lambda package: SimpleNamespace(version_name="29.0.3"))
    return module, member, runtime, device


def _verified_first_launch(tmp_path: Path, member: dict, runtime: WorkerRuntime) -> Attempt:
    import json

    saved = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "saved-job")
    saved.persist(runtime.checkpoint_root / f"{saved.generation}.json",
                  IdentityEvidence("DDFD5D6F7D6EF094", saved.created_at + 1, "capture"))
    (runtime.checkpoint_root / ".first-launch-account.json").write_text(json.dumps({
        "state": "verified", "instance": member["name"], "account_id": "DDFD5D6F7D6EF094",
        "app_version": "29.0.3", "worker_id": saved.worker_id, "endpoint": saved.endpoint,
        "lease_id": saved.lease_id, "attempt_id": saved.attempt_id,
        "generation": saved.generation, "created_at": saved.created_at}))
    (tmp_path / "manual-first-launch-registry.json").write_text(json.dumps({
        member["name"]: {"instance": member["name"], "account_id": "DDFD5D6F7D6EF094"}}))
    return saved


def test_opened_tower_resumes_restart_proof_of_verified_first_launch(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module, member, runtime, device = _opened_tower(tmp_path, monkeypatch)
    runtime.ensure_directories()
    saved = _verified_first_launch(tmp_path, member, runtime)
    proved = []
    monkeypatch.setattr(module, "probe_clone_worker", lambda **kwargs: (
        proved.append((kwargs["candidate"].attempt, kwargs["account_id"]))
        or {"evidence_ref": "restart-capture"}))

    fresh = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "new-job")
    result = enroll_manual_instance(root=tmp_path, member=member, runtime=runtime,
        attempt=fresh, inventory=None, connect=lambda endpoint: device, protected_names=set())
    assert proved == [(saved, "DDFD5D6F7D6EF094")]
    assert result["account_id"] == "DDFD5D6F7D6EF094"
    assert result["job_id"] == "saved-job"
    assert result["binding"] == str(runtime.checkpoint_root / f"{saved.generation}.json")
    assert (runtime.root / "fleet-registration.json").exists()


@pytest.mark.parametrize("change", ["no_journal", "unverified", "registry_account",
                                    "app_version"])
def test_opened_tower_without_matching_verified_first_launch_is_not_resumed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    import json

    module, member, runtime, device = _opened_tower(tmp_path, monkeypatch)
    runtime.ensure_directories()
    if change != "no_journal":
        _verified_first_launch(tmp_path, member, runtime)
        journal = runtime.checkpoint_root / ".first-launch-account.json"
        audit = json.loads(journal.read_text())
        if change == "unverified":
            audit["state"] = "started"
        if change == "app_version":
            audit["app_version"] = "28.0.0"
        journal.write_text(json.dumps(audit))
        if change == "registry_account":
            (tmp_path / "manual-first-launch-registry.json").write_text(json.dumps({
                member["name"]: {"account_id": "SOMEONE-ELSE"}}))
    monkeypatch.setattr(module, "probe_clone_worker", lambda **kwargs:
                        pytest.fail("restart proof must not run"))

    fresh = Attempt.new(member["name"], member["endpoint"], member["lease_id"], "new-job")
    with pytest.raises(ValueError, match="worker_registration_missing_for_opened_tower"):
        enroll_manual_instance(root=tmp_path, member=member, runtime=runtime,
            attempt=fresh, inventory=None, connect=lambda endpoint: device,
            protected_names=set())
    assert not (runtime.root / "fleet-registration.json").exists()
