"""The coordinator previews without writing and publishes by revision."""

from __future__ import annotations

from pathlib import Path
import json
import time
from dataclasses import asdict
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from events import EventBus
import db
from fleet.build_route import RouteDocument
from fleet.build_route_eval import RouteFacts
from fleet.setup import FleetSetupService
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def _client(root: Path, *, active_names: tuple[str, ...] = ()) -> TestClient:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    if active_names:
        fleet._reroll_pool = SimpleNamespace(members=lambda: [
            {"name": name} for name in active_names])
        fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set())
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def test_read_and_preview_compatibility_route_without_persisting(tmp_path: Path) -> None:
    client = _client(tmp_path)
    read = client.get("/api/fleet/reroll/route")
    assert read.status_code == 200
    assert read.json()["revision"] == 0
    preview = client.post("/api/fleet/reroll/route/preview", json={
        "route": RouteDocument.compatibility().to_dict(), "expected_revision": 0})
    assert preview.status_code == 200
    assert preview.json()["saved_revision"] == 0
    assert preview.json()["members"] == []
    assert not (tmp_path / "build-route.json").exists()
    assert not (tmp_path / "reroll-runs.json").exists()


def test_publish_conflict_returns_current_revision(tmp_path: Path) -> None:
    client = _client(tmp_path)
    payload = {"expected_revision": 0, "actor": "operator",
               "route": RouteDocument.compatibility().to_dict()}
    first = client.put("/api/fleet/reroll/route", json=payload)
    assert first.status_code == 200
    assert first.json()["revision"] == 1
    second = client.put("/api/fleet/reroll/route", json=payload)
    assert second.status_code == 409
    assert second.json()["detail"]["current_revision"] == 1


def test_invalid_upgrade_is_rejected_and_previous_route_survives(tmp_path: Path) -> None:
    client = _client(tmp_path)
    route = RouteDocument.compatibility().to_dict()
    route["baseline"]["workshop"]["priority_ids"] = ["invented"]
    response = client.put("/api/fleet/reroll/route", json={
        "expected_revision": 0, "actor": "operator", "route": route})
    assert response.status_code == 422
    assert client.get("/api/fleet/reroll/route").json()["revision"] == 0


def test_rollback_creates_new_revision(tmp_path: Path) -> None:
    client = _client(tmp_path)
    route = RouteDocument.compatibility().to_dict()
    route["baseline"]["workshop"]["coin_spend_limit_pct"] = 30
    assert client.put("/api/fleet/reroll/route", json={
        "expected_revision": 0, "actor": "operator", "route": route}).status_code == 200
    assert client.post("/api/fleet/reroll/route/rollback", json={
        "revision": 1, "expected_revision": 1, "actor": "operator"}).json()["revision"] == 2
    assert [row["revision"] for row in client.get("/api/fleet/reroll/route/revisions").json()["revisions"]] == [1, 2]


def test_route_api_is_unavailable_without_fleet_capability() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None))
    assert client.get("/api/fleet/reroll/route").status_code == 503


def _registered_worker(root: Path, account_id: str, db_account_id: str,
                       worker_name: str = "Air_38") -> None:
    worker = root / "workers" / worker_name
    checkpoints = worker / "checkpoints"
    checkpoints.mkdir(parents=True)
    endpoint = "127.0.0.1:5555"
    lease_id = "lease"
    binding = checkpoints / f"{uuid4().hex}.json"
    binding.write_text(json.dumps({"worker_id": worker_name, "account_id": account_id,
                                   "endpoint": endpoint, "lease_id": lease_id}))
    (worker / "fleet-registration.json").write_text(json.dumps({
        "state": "registered", "instance": worker_name, "account_id": account_id,
        "web_port": 8081, "binding": str(binding), "endpoint": endpoint,
        "lease_id": lease_id}))
    db.bind_account(worker / "tower_bot.db", db_account_id)


def test_preview_only_includes_current_pool_members(tmp_path: Path) -> None:
    for name in ("Air_18", "Air_39", "Air_40"):
        _registered_worker(tmp_path, name, name, name)
    service = FleetSetupService(tmp_path, qualification_root=tmp_path / "qualifications")
    service._reroll_pool = SimpleNamespace(members=lambda: [
        {"name": "Air_39"}, {"name": "Air_40"}])
    preview = service.build_route_preview(RouteDocument.compatibility())

    assert [(member["worker"], member["account_id"])
            for member in preview["members"]] == [
                ("Air_39", "Air_39"), ("Air_40", "Air_40")]
    assert preview["members"][0]["current"]["trace"]["reason"] == (
        "Waiting for first verified Workshop observation")
    assert preview["members"][0]["current_battle"]["trace"]["reason"] == (
        "Waiting for first verified battle observation")


def test_publish_rejects_override_when_worker_database_changed_accounts(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-B")
    client = _client(tmp_path)
    route = RouteDocument.compatibility().to_dict()
    route["overrides"] = {"Air_38": {"account_id": "ACCOUNT-A", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 30}}}}
    response = client.put("/api/fleet/reroll/route", json={
        "expected_revision": 0, "actor": "operator", "route": route})
    assert response.status_code == 409


def test_rebind_preview_is_read_only_and_requires_verified_new_account(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-B", "ACCOUNT-B")
    client = _client(tmp_path)
    route = RouteDocument.compatibility().to_dict()
    route["overrides"] = {"Air_38": {"account_id": "ACCOUNT-A", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 30}}}}
    payload = {"route": route, "expected_revision": 0, "worker": "Air_38",
               "old_account_id": "ACCOUNT-A", "new_account_id": "ACCOUNT-B"}
    response = client.post("/api/fleet/reroll/route/rebind-preview", json=payload)
    assert response.status_code == 200
    assert response.json()["old_effective"]["workshop"]["coin_spend_limit_pct"] == 30
    assert response.json()["new_effective"]["workshop"]["coin_spend_limit_pct"] == 100
    assert client.get("/api/fleet/reroll/route").json()["revision"] == 0
    assert client.post("/api/fleet/reroll/route/rebind-preview", json={
        **payload, "new_account_id": "INVENTED"}).status_code == 409


def test_preview_compares_resource_path_with_same_verified_facts(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    worker = tmp_path / "workers" / "Air_38"
    now = time.time()
    (worker / "build-route-resource-facts.json").write_text(json.dumps(asdict(
        RouteFacts("ACCOUNT-A", "Air_38", "main_menu", now, now,
                   wallet_gems=20, lab_slot2_owned=True, game_speed_maxed=True))))
    draft = RouteDocument.compatibility().to_dict()
    draft["baseline"]["gems"]["steps"] = ["unlock_lab_slot_2", "cards"]
    preview = _client(tmp_path, active_names=("Air_38",)).post("/api/fleet/reroll/route/preview", json={
        "route": draft, "expected_revision": 0})
    assert preview.status_code == 200
    member = preview.json()["members"][0]
    assert member["current_resources"]["gem_step"]["action"] == "lab_slot_2_owned"
    assert member["proposed_resources"]["gem_step"]["action"] == "cards"
    assert member["proposed_resources"]["gem_step"]["status"] == "planned"
    assert preview.json()["proposed_revision"] == 1
