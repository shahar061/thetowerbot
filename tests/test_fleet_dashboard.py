"""Fleet dashboard requests stay behind live qualification and identity proof."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from bluestacks import BlueStacksAdapter, HostInstance
from events import EventBus
from fleet.clone_qualification import QualificationScope
from fleet.dashboard import FleetController, FleetRequestError
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


class Driver:
    supports_clone_staging = True
    supports_lifecycle = True
    supports_m05_live_qualification = True

    def __init__(self, scope: QualificationScope, *, drift: bool = True) -> None:
        self.scope = scope
        self.drift = drift
        self.rows = [HostInstance("seed", "127.0.0.1:5555", "seed-lease", "running", "seed-v1")]
        self.staged: list[str] = []

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


def controller(tmp_path: Path, *, qualified: bool = True, verify=None,
               drift: bool = True) -> tuple[FleetController, Driver]:
    scope = QualificationScope("host", "air", "seed-v1", "v1", "game-v1",
                               {"configuration_digest": "original", "source_instance": "seed"})
    driver = Driver(scope, drift=drift)
    (tmp_path / "m05.json").write_text('{"source_instance":"seed","account_ids":["SOURCE"]}')
    fleet = FleetController(BlueStacksAdapter(driver, staging_root=tmp_path / "locks"),
                            qualification_path=tmp_path / "m05.json", state_path=tmp_path / "jobs.json",
                            qualification_gate=lambda path, scope: qualified,
                            verify_clone=verify)
    return fleet, driver


def test_unqualified_source_cannot_stage(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path, qualified=False)
    assert fleet.snapshot()["sources"][0]["state"] == "blocked"
    with pytest.raises(FleetRequestError):
        fleet.request("seed", 1)
    assert driver.staged == []


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

    fleet, _ = controller(tmp_path, verify=verify, drift=False)
    job = fleet.request("seed", 1)
    fleet.run_pending(job["id"])
    assert fleet.snapshot()["jobs"][0]["clones"][0]["state"] == "ready"


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
    response = client.post("/api/fleet/clones", json={"source": "seed", "count": 1})
    assert response.status_code == 409
    assert driver.staged == []


def test_api_background_request_reports_blocked_staged_clone(tmp_path: Path) -> None:
    fleet, driver = controller(tmp_path)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    response = client.post("/api/fleet/clones", json={"source": "seed", "count": 1})
    assert response.status_code == 202
    assert driver.staged == ["seed_1"]
    assert client.get("/api/fleet").json()["jobs"][0]["clones"][0]["state"] == "blocked"
