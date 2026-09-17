"""Fleet dashboard requests stay behind live qualification and identity proof."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
import time

import pytest
from fastapi.testclient import TestClient

from bluestacks import BlueStacksAdapter, HostCapabilityError, HostInstance
from events import EventBus
from fleet.clone_qualification import QualificationScope
from fleet.dashboard import FleetController, FleetPolicy, FleetRequestError
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


class Driver:
    supports_clone_staging = True
    supports_fresh_provision = True
    supports_lifecycle = True
    supports_m05_live_qualification = True

    def __init__(self, scope: QualificationScope, *, drift: bool = True) -> None:
        self.scope = scope
        self.drift = drift
        self.rows = [HostInstance("seed", "127.0.0.1:5555", "seed-lease", "running", "seed-v1")]
        self.staged: list[str] = []
        self.fresh: list[str] = []

    def qualification_scope(self) -> QualificationScope | None:
        return self.scope

    def inventory(self) -> list[HostInstance]:
        return list(self.rows)

    def stage_clone(self, name: str, source: str) -> HostInstance:
        self.staged.append(name)
        row = HostInstance(name, "127.0.0.1:5557", "clone-lease", "running", "seed-v1")
        self.rows.append(row)
        if self.drift:
            self.scope = replace(self.scope, instance_config={"configuration_digest": "changed"})
        return row

    def create_fresh(self, name: str) -> HostInstance:
        self.fresh.append(name)
        row = HostInstance(name, "127.0.0.1:5559", "fresh-lease", "running")
        self.rows.append(row)
        return row

    def start(self, name: str) -> None:
        self.rows = [replace(row, state="running") if row.name == name else row
                     for row in self.rows]


def controller(tmp_path: Path, *, qualified: bool = True, verify=None,
               drift: bool = True, capacity: int = 5,
               register=None) -> tuple[FleetController, Driver]:
    scope = QualificationScope("host", "air", "seed-v1", "v1", "game-v1",
                               {"configuration_digest": "original", "source_instance": "seed"})
    driver = Driver(scope, drift=drift)
    (tmp_path / "m05.json").write_text('{"source_instance":"seed","account_ids":["SOURCE"]}')
    fleet = FleetController(BlueStacksAdapter(driver, staging_root=tmp_path / "locks"),
                            qualification_path=tmp_path / "m05.json", state_path=tmp_path / "jobs.json",
                            policy=FleetPolicy(capacity=capacity, name_prefix="seed_"),
                            qualification_gate=lambda path, scope: qualified,
                            verify_clone=verify, register_worker=register)
    return fleet, driver


def test_unopened_clone_requires_first_launch_verifier(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, drift=False)
    (tmp_path / "m05.json").write_text(
        '{"schema":2,"source_instance":"seed",'
        '"source_evidence":{"tower_unopened":true},"account_ids":["A","B"]}'
    )
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    row = fleet.snapshot()["jobs"][0]["clones"][0]
    assert driver.staged == ["seed_1"]
    assert row["state"] == "blocked"
    assert row["reason"] == "first_launch_clone_verifier_required"


def test_unopened_clone_starts_after_manager_creates_it_stopped(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, drift=False)
    (tmp_path / "m05.json").write_text(
        '{"schema":2,"source_instance":"seed",'
        '"source_evidence":{"tower_unopened":true},"account_ids":["A","B"]}'
    )
    original = driver.stage_clone

    def stage_stopped(name: str, source: str) -> HostInstance:
        original(name, source)
        driver.rows[-1] = replace(driver.rows[-1], state="stopped")
        return driver.rows[-1]

    driver.stage_clone = stage_stopped  # type: ignore[method-assign]
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    assert driver.rows[-1].state == "running"
    assert fleet.snapshot()["jobs"][0]["clones"][0]["reason"] == "first_launch_clone_verifier_required"


def test_unqualified_source_cannot_stage(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, qualified=False)
    assert fleet.snapshot()["sources"][0]["state"] == "blocked"
    with pytest.raises(FleetRequestError):
        fleet.request("seed", 1)
    assert driver.staged == []


def test_preview_reserves_explicit_names_and_capacity_without_host_mutation(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, capacity=3)
    preview = fleet.preview("clone", "seed", 2)
    assert preview["targets"] == ["seed_1", "seed_2"]
    assert preview["state"] == "eligible"
    assert fleet.snapshot()["capacity"] == {"limit": 3, "used": 1, "available": 2}
    assert driver.staged == []
    with pytest.raises(FleetRequestError, match="capacity"):
        fleet.request("seed", 3)
    assert driver.staged == []


def test_preview_uses_installed_host_names_after_old_clones_were_removed(tmp_path: Path) -> None:
    fleet, _ = controller(tmp_path)
    fleet._jobs = [{"id": "old", "clones": [
        {"instance": "seed_18", "state": "ready"},
        {"instance": "seed_19", "state": "ready"},
    ]}]
    assert fleet.preview("clone", "seed", 2)["targets"] == ["seed_1", "seed_2"]


def test_preview_uses_manager_counter_when_it_exceeds_installed_names(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    driver.next_clone_name = lambda source: "seed_20"  # type: ignore[attr-defined]

    assert fleet.preview("clone", "seed", 2)["targets"] == ["seed_20", "seed_21"]


def test_slow_source_check_does_not_hold_job_state_lock(tmp_path: Path) -> None:
    fleet, _ = controller(tmp_path)
    entered = Event()
    release = Event()
    original = fleet._source

    def slow_source() -> tuple[str | None, str]:
        entered.set()
        release.wait(2)
        return original()

    fleet._source = slow_source  # type: ignore[method-assign]
    reader = Thread(target=fleet.snapshot)
    reader.start()
    try:
        assert entered.wait(1)
        assert fleet._lock.acquire(timeout=.2), "slow host check held the job state lock"
        fleet._lock.release()
    finally:
        release.set()
        reader.join(timeout=2)
    assert not reader.is_alive()


def test_live_capabilities_are_attested_once_per_source_check(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    calls = 0

    def attest() -> bool:
        nonlocal calls
        calls += 1
        return True

    driver.attest_clone_capabilities = attest  # type: ignore[attr-defined]
    assert fleet._source() == ("seed", "qualified")
    assert calls == 1


def test_fresh_request_is_available_without_clone_qualification_but_stays_blocked_for_identity(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, qualified=False)
    assert fleet.preview("fresh", None, 1)["targets"] == ["seed_1"]
    job = fleet.request(None, 1, mode="fresh")
    fleet.run_pending(job["id"])
    clone = fleet.snapshot()["jobs"][0]["clones"][0]
    assert driver.fresh == ["seed_1"]
    assert clone["state"] == "blocked"
    assert clone["reason"] == "first_launch_identity_required"


def test_unreadable_fleet_audit_closes_request_gate(tmp_path: Path) -> None:
    (tmp_path / "jobs.json").write_text("{broken")
    fleet, driver = controller(tmp_path)
    assert fleet.snapshot()["sources"][0]["reason"] == "fleet_audit_unreadable"
    with pytest.raises(FleetRequestError):
        fleet.request("seed", 1)
    assert driver.staged == []


def test_request_stages_through_driver_and_blocks_without_identity_proof(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    job = fleet.request("seed", 2)
    fleet.run_pending(job["id"])
    rows = fleet.snapshot()["jobs"][0]["clones"]
    assert driver.staged == ["seed_1"]
    assert rows[0]["state"] == "blocked"
    assert rows[0]["reason"] == "identity_reset_and_evidence_required"
    assert rows[1]["state"] == "blocked"
    assert rows[1]["reason"] == "qualification_scope_changed"


def test_only_verified_identity_and_recovery_becomes_ready(tmp_path: Path) -> None:
    def verify(row: HostInstance, source_id: str) -> dict:
        now = time.time()
        time.sleep(0.002)
        return {"state": "verified", "account_id": "NEW", "source_account_id": source_id,
                "endpoint": row.endpoint, "lease_id": row.lease_id,
                "identity_reset": True, "recovery_verified": True, "evidence_ref": "capture",
                "recovery_evidence_ref": "capture-after", "identity_observed_at": now,
                "recovered_at": time.time()}

    def register(row: HostInstance, proof: dict, job_id: str) -> dict:
        assert job_id
        return {"state": "registered", "instance": row.name, "endpoint": row.endpoint,
                "lease_id": row.lease_id, "account_id": proof["account_id"],
                "evidence_ref": "registration-record"}

    fleet, _ = controller(tmp_path, verify=verify, drift=False, register=register)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    assert fleet.snapshot()["jobs"][0]["clones"][0]["state"] == "ready"


def test_verified_identity_without_worker_registration_stays_blocked(tmp_path: Path) -> None:
    def verify(row: HostInstance, source_id: str) -> dict:
        now = time.time()
        time.sleep(0.002)
        return {"state": "verified", "account_id": "NEW", "source_account_id": source_id,
                "endpoint": row.endpoint, "lease_id": row.lease_id,
                "identity_reset": True, "recovery_verified": True, "evidence_ref": "first",
                "recovery_evidence_ref": "second", "identity_observed_at": now,
                "recovered_at": time.time()}

    fleet, _ = controller(tmp_path, verify=verify, drift=False)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    row = fleet.snapshot()["jobs"][0]["clones"][0]
    assert row["state"] == "blocked"
    assert row["reason"] == "worker_registration_required"
    assert [step["state"] for step in row["steps"]] == ["queued", "staging", "verifying", "blocked"]


def test_failed_target_can_only_retry_if_exact_host_name_is_absent(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    with pytest.raises(FleetRequestError, match="existing_host_instance"):
        fleet.resolve(job["id"], 0, "retry")
    resolved = fleet.resolve(job["id"], 0, "quarantine")
    assert resolved["clones"][0]["state"] == "quarantined"
    assert driver.staged == ["seed_1"]


def test_exact_target_retry_is_allowed_before_any_host_create(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    job = fleet.request("seed", 1)
    def fail_before_create(name: str, source: str) -> HostInstance:
        raise ValueError("manager_unavailable")
    driver.stage_clone = fail_before_create  # type: ignore[method-assign]
    fleet.run_pending(job["id"])
    assert fleet.snapshot()["jobs"][0]["clones"][0]["state"] == "quarantined"
    retried = fleet.resolve(job["id"], 0, "retry")
    assert retried["clones"][0]["instance"] == "seed_1"
    assert retried["clones"][0]["state"] == "queued"


def test_absent_quarantined_target_can_be_dismissed_then_replaced(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    job = fleet.request("seed", 1)

    def fail_before_create(name: str, source: str) -> HostInstance:
        raise HostCapabilityError("BlueStacks Air next clone name is required")

    driver.stage_clone = fail_before_create  # type: ignore[method-assign]
    fleet.run_pending(job["id"])
    assert fleet.snapshot()["jobs"][0]["clones"][0]["detail"] == "BlueStacks Air next clone name is required"
    with pytest.raises(FleetRequestError, match="provisioning_or_quarantine_requires_review"):
        fleet.preview("clone", "seed", 1)
    dismissed = fleet.resolve(job["id"], 0, "dismiss")
    assert dismissed["clones"][0]["state"] == "dismissed"
    assert fleet.preview("clone", "seed", 1)["targets"] == ["seed_1"]
    assert fleet.request("seed", 1)["clones"][0]["instance"] == "seed_1"


def test_created_host_instance_cannot_be_dismissed(tmp_path: Path) -> None:
    fleet, _ = controller(tmp_path)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    with pytest.raises(FleetRequestError, match="existing_host_instance_requires_quarantine"):
        fleet.resolve(job["id"], 0, "dismiss")


def test_identity_confirmation_failure_is_quarantined_without_leaking_detail(tmp_path: Path) -> None:
    def needs_review(row: HostInstance, source_id: str) -> dict:
        raise RuntimeError("private credential in integration error")

    fleet, _ = controller(tmp_path, verify=needs_review, drift=False)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    clone = fleet.snapshot()["jobs"][0]["clones"][0]
    assert clone["state"] == "quarantined"
    assert "private credential" not in clone["reason"]


def test_api_exposes_safe_fleet_state_and_rejects_unqualified_request(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, qualified=False)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    assert client.get("/api/fleet").json()["sources"][0]["state"] == "blocked"
    response = client.post("/api/fleet/requests", json={"mode": "clone", "source": "seed",
                                                        "count": 1, "targets": ["seed_1"]})
    assert response.status_code == 409
    assert driver.staged == []


def test_api_background_request_reports_blocked_staged_clone(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    response = client.post("/api/fleet/requests", json={"mode": "clone", "source": "seed",
                                                        "count": 1, "targets": ["seed_1"]})
    assert response.status_code == 202
    assert driver.staged == ["seed_1"]
    assert client.get("/api/fleet").json()["jobs"][0]["clones"][0]["state"] == "blocked"


def test_api_previews_named_fresh_request_and_stages_it_without_clone_qualification(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, qualified=False)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    preview = client.get("/api/fleet/preview", params={"mode": "fresh", "count": 1})
    assert preview.status_code == 200
    assert preview.json()["targets"] == ["seed_1"]
    response = client.post("/api/fleet/requests", json={"mode": "fresh", "count": 1,
                                                        "source": None, "targets": ["seed_1"]})
    assert response.status_code == 202
    assert driver.fresh == ["seed_1"]
