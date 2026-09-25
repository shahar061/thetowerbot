"""The coordinator previews without writing and publishes by revision."""

from __future__ import annotations

from pathlib import Path
import json
import time
from dataclasses import asdict, replace
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from events import EventBus
import db
from fleet.build_route import RouteDocument, resolve_route
from fleet.build_route_eval import RouteFacts, evaluate_workshop
from fleet.build_route_runtime import BuildRouteRuntime
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


def test_saved_strategy_assignment_pins_version_and_checks_identity(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    client = _client(tmp_path, active_names=("Air_38",))
    initial = client.get("/api/fleet/reroll/strategies").json()
    baseline = initial["templates"][0]["baseline"]
    saved = client.post("/api/fleet/reroll/strategies", json={
        "expected_revision": 0, "name": "My opening", "source_template": "opening", "baseline": baseline})
    assert saved.status_code == 200
    strategy = saved.json()["strategies"][0]
    assert client.get("/api/fleet/reroll/route").json()["revision"] == 0
    payload = {"expected_revision": 0, "strategy_id": strategy["id"], "strategy_version": 1,
               "workers": [{"worker": "Air_38", "account_id": "ACCOUNT-A"}]}
    assigned = client.post("/api/fleet/reroll/strategies/assign", json=payload)
    assert assigned.status_code == 200
    assert assigned.json()["revision"] == 1
    audit = json.loads((tmp_path / "build-route-history" / "1.json").read_text())["audit"]
    assert audit["changed_rule_ids"] == ["assignment.Air_38"]
    baseline["workshop"]["coin_spend_limit_pct"] = 25
    assert client.post("/api/fleet/reroll/strategies", json={
        "expected_revision": 1, "name": "Changed", "source_template": "opening",
        "baseline": baseline, "strategy_id": strategy["id"]}).status_code == 200
    current = client.get("/api/fleet/reroll/route").json()
    assert current["assignments"]["Air_38"]["baseline"]["workshop"]["coin_spend_limit_pct"] == 100
    assert current["assignments"]["Air_38"]["strategy_version"] == 1
    assert client.post("/api/fleet/reroll/strategies/assign", json=payload).status_code == 409
    payload["expected_revision"] = 1
    payload["workers"][0]["account_id"] = "ACCOUNT-B"
    assert client.post("/api/fleet/reroll/strategies/assign", json=payload).status_code == 409
    assert client.get("/api/fleet/reroll/route").json()["revision"] == 1


def test_strategy_api_rejects_invalid_and_protected_saves(tmp_path: Path) -> None:
    client = _client(tmp_path)
    template = client.get("/api/fleet/reroll/strategies").json()["templates"][0]
    payload = {"expected_revision": 0, "name": "Copy", "source_template": "opening",
               "baseline": template["baseline"]}
    assert client.post("/api/fleet/reroll/strategies", json={**payload, "strategy_id": "turtle"}).status_code == 422
    assert client.post("/api/fleet/reroll/strategies", json={**payload, "expected_revision": True}).status_code == 422
    assert client.post("/api/fleet/reroll/strategies", json=payload).status_code == 200
    assert client.post("/api/fleet/reroll/strategies", json=payload).status_code == 409
    strategy = client.get("/api/fleet/reroll/strategies").json()["strategies"][0]
    assert client.post("/api/fleet/reroll/strategies/assign", json={
        "expected_revision": 0, "strategy_id": strategy["id"], "strategy_version": 1,
        "workers": [{"worker": "not-in-pool", "account_id": "A"}]}).status_code == 422


def test_assignment_is_atomic_and_rejects_hidden_and_replaced_workers(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "A", "A", "Air_1")
    _registered_worker(tmp_path, "B", "REPLACED", "Air_2")
    service = FleetSetupService(tmp_path, qualification_root=tmp_path / "qualifications")
    service._reroll_pool = SimpleNamespace(members=lambda: [{"name": "Air_1"}, {"name": "Air_2"}])
    hidden: set[str] = {"Air_1"}
    service._reroll_runs = SimpleNamespace(hidden_names=lambda: hidden)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None, fleet=service))
    baseline = service.strategy_library().read()["templates"][0]["baseline"]
    strategy = service.strategy_library().save(expected_revision=0, name="Copy",
        source_template="opening", baseline=baseline)["strategies"][0]
    payload = {"expected_revision": 0, "strategy_id": strategy["id"], "strategy_version": 1,
               "workers": [{"worker": "Air_1", "account_id": "A"}]}
    assert client.post("/api/fleet/reroll/strategies/assign", json=payload).status_code == 422
    hidden.clear()
    payload["workers"].append({"worker": "Air_2", "account_id": "B"})
    assert client.post("/api/fleet/reroll/strategies/assign", json=payload).status_code == 409
    assert service.build_route_store().read().revision == 0
    assert not (tmp_path / "build-route.json").exists()


def test_protected_template_can_be_assigned_without_creating_a_copy(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    client = _client(tmp_path, active_names=("Air_38",))
    response = client.post("/api/fleet/reroll/strategies/assign", json={
        "expected_revision": 0, "strategy_id": "turtle", "strategy_version": 1,
        "workers": [{"worker": "Air_38", "account_id": "ACCOUNT-A"}]})
    assert response.status_code == 200
    assignment = response.json()["assignments"]["Air_38"]
    assert assignment["strategy_id"] == "turtle"
    assert assignment["strategy_version"] == 1
    assert assignment["baseline"]["workshop"]["mode"] == "blocks"
    assert client.get("/api/fleet/reroll/strategies").json()["revision"] == 0


def test_generic_publish_cannot_forge_or_tamper_strategy_snapshots(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    client = _client(tmp_path, active_names=("Air_38",))
    assigned = client.post("/api/fleet/reroll/strategies/assign", json={
        "expected_revision": 0, "strategy_id": "turtle", "strategy_version": 1,
        "workers": [{"worker": "Air_38", "account_id": "ACCOUNT-A"}]}).json()
    for field, value in (("strategy_version", 99), ("strategy_id", "invented"),
                         ("strategy_name", "Forged"), ("baseline", RouteDocument.compatibility().baseline.to_dict())):
        forged = json.loads(json.dumps(assigned))
        forged["assignments"]["Air_38"][field] = value
        response = client.put("/api/fleet/reroll/route", json={
            "expected_revision": 1, "actor": "operator", "route": forged})
        assert response.status_code == 422
        assert client.get("/api/fleet/reroll/route").json() == assigned
    # An unchanged immutable snapshot can accompany an ordinary baseline edit.
    assigned["baseline"]["workshop"]["coin_spend_limit_pct"] = 50
    assert client.put("/api/fleet/reroll/route", json={
        "expected_revision": 1, "actor": "operator", "route": assigned}).status_code == 200


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


def test_preview_respects_pending_choice_and_simulates_the_next_revision(tmp_path: Path) -> None:
    _registered_worker(tmp_path, "ACCOUNT-A", "ACCOUNT-A")
    service = FleetSetupService(tmp_path, qualification_root=tmp_path / "qualifications")
    service._reroll_pool = SimpleNamespace(members=lambda: [{"name": "Air_38"}])
    raw = RouteDocument.compatibility().to_dict()
    raw["baseline"]["workshop"].update(mode="priorities", priority_ids=["damage", "attack_speed"],
                                       draw_chance_pct=100)
    saved = service.build_route_store().publish(RouteDocument.from_dict(raw), 0, "test")
    now = time.time()
    facts = RouteFacts("ACCOUNT-A", "Air_38", "workshop", now, now, wallet_coins=100,
                       prices={"damage": 10, "attack_speed": 10}, visit_id="visit-1")
    current = evaluate_workshop(resolve_route(saved, "Air_38", "ACCOUNT-A"), facts, None)
    assert current.pending is not None
    runtime = BuildRouteRuntime(tmp_path, "Air_38", "ACCOUNT-A")
    runtime.remember_pending(current.pending)
    unavailable = replace(facts, prices={**facts.prices, current.pending.chosen_id: 200})
    runtime.publish_facts(unavailable)
    before = runtime.choice_path.read_bytes()
    preview = service.build_route_preview(saved)
    member = preview["members"][0]
    assert member["current"]["decision"] is None
    assert "pending" in member["current"]["trace"]["reason"].lower()
    assert member["proposed"]["pending"]["revision"] == saved.revision + 1
    assert member["proposed"]["pending"]["chosen_id"] != current.pending.chosen_id
    assert runtime.choice_path.read_bytes() == before
