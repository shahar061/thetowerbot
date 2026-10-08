"""Single-account overview and savings changes stay bound to one account."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.error import URLError

import pytest
from fastapi.testclient import TestClient

import db
from events import EventBus
from fleet.build_route import RouteDocument, StrategyAssignment, resolve_route
from fleet.build_route_store import BuildRouteStore
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def worker(root: Path, name: str, account_id: str, port: int) -> Path:
    directory = root / "workers" / name
    binding = directory / "checkpoints" / ("a" * 32 + ".json")
    binding.parent.mkdir(parents=True)
    identity = {"worker_id": name, "account_id": account_id,
                "endpoint": f"127.0.0.1:{port + 1000}", "lease_id": "lease"}
    binding.write_text(json.dumps(identity))
    (directory / "fleet-registration.json").write_text(json.dumps({
        **identity, "instance": name, "state": "registered", "binding": str(binding),
        "web_port": port,
    }))
    path = directory / "tower_bot.db"
    db.bind_account(path, account_id)
    return path


def client(root: Path, path: Path | None = None) -> TestClient:
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=path, fleet=SimpleNamespace(root=root)))


HEADERS = {"x-account-scope": "worker:Air_1", "x-expected-account-id": "ACCOUNT1"}


@pytest.fixture(autouse=True)
def worker_ports_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def closed(request: Any, timeout: float) -> io.BytesIO:
        raise URLError(ConnectionRefusedError("test worker is stopped"))
    monkeypatch.setattr("web.account_overview.urlopen", closed)


def test_state_reads_only_selected_offline_account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    one = worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    two = worker(tmp_path, "Air_2", "ACCOUNT2", 8002)
    for path, balance in ((one, 100), (two, 900)):
        with db.connect(path) as conn:
            conn.execute("INSERT INTO ledger(ts,kind,currency,delta,balance_after,dry_run) "
                         "VALUES (10,'RUN_PAYOUT','coins',?,?,0)", (balance, balance))
    calls: list[Any] = []

    def offline(request: Any, timeout: float) -> io.BytesIO:
        calls.append(request)
        raise OSError("offline")

    monkeypatch.setattr("web.account_overview.urlopen", offline)
    response = client(tmp_path).get("/api/account/state", headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == "ACCOUNT1"
    assert payload["account"]["id"] == "Air_1"
    assert payload["account"]["balances"]["coins"] == 100
    assert payload["account"]["online"] is False
    assert len(calls) == 1 and calls[0].full_url == "http://127.0.0.1:8001/api/status"
    assert calls[0].get_header("X-expected-account-id") == "ACCOUNT1"


def test_state_rejects_wrong_or_missing_scope_and_unattributed_history(tmp_path: Path) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    app = client(tmp_path)
    assert app.get("/api/account/state").json()["account"] is None
    assert app.get("/api/account/state", headers={**HEADERS, "x-expected-account-id": "OLD"}).status_code == 409
    assert app.get("/api/account/state", headers={"x-account-scope": "worker:Archive"}).status_code == 404


def test_state_rejects_account_replacement_during_status_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = worker(tmp_path, "Air_1", "ACCOUNT1", 8001)

    def replaced(request: Any, timeout: float) -> io.BytesIO:
        registration = path.parent / "fleet-registration.json"
        raw = json.loads(registration.read_text())
        raw["account_id"] = "REPLACED"
        registration.write_text(json.dumps(raw))
        return io.BytesIO(json.dumps({"screen": "IN_RUN", "wave": 999}).encode())

    monkeypatch.setattr("web.account_overview.urlopen", replaced)
    response = client(tmp_path).get("/api/account/state", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["detail"] == "selected_account_changed"


def test_savings_override_preserves_assigned_strategy_and_other_accounts(tmp_path: Path) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    worker(tmp_path, "Air_2", "ACCOUNT2", 8002)
    route = RouteDocument.compatibility()
    assigned_raw = route.baseline.to_dict()
    assigned_raw["rules"]["gems"]["keep"] = 125
    assigned_raw["rules"]["coins"]["workshop_spend_limit_pct"] = 45
    assigned_raw["workshop"]["coin_spend_limit_pct"] = 45
    assignment = StrategyAssignment.from_dict({"account_id": "ACCOUNT1", "strategy_id": "saved",
        "strategy_version": 2, "strategy_name": "Saved strategy", "baseline": assigned_raw})
    route.assignments["Air_1"] = assignment
    saved = BuildRouteStore(tmp_path).publish(route, 0, "test")
    app = client(tmp_path)
    before = app.get("/api/account/lab-savings", headers=HEADERS)
    assert before.status_code == 200
    assert before.json()["revision"] == 1 and before.json()["editable"] is True
    response = app.put("/api/account/lab-savings", headers=HEADERS, json={
        "expected_account_id": "ACCOUNT1", "expected_revision": 1,
        "share_mode": "save_pct", "share_pct": 35})
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2 and response.json()["share_pct"] == 35
    current = BuildRouteStore(tmp_path).read()
    assert current.assignments == saved.assignments
    assert current.baseline == saved.baseline
    effective = resolve_route(current, "Air_1", "ACCOUNT1")
    assert effective.rules.coins.lab_share.pct == 35
    assert effective.rules.coins.lab_share.mode == "save_pct"
    assert effective.rules.coins.workshop_spend_limit_pct == 45
    assert effective.rules.gems.keep == 125
    assert resolve_route(current, "Air_2", "ACCOUNT2").rules == saved.baseline.rules
    assert resolve_route(current, "Air_1", "REPLACED").rules.coins.lab_share.mode == "when_affordable"


@pytest.mark.parametrize("patch,status", [
    ({"expected_revision": 99}, 409), ({"expected_account_id": "OTHER"}, 409),
    ({"share_pct": 91}, 422), ({"share_pct": True}, 422),
    ({"share_mode": "just_in_time"}, 422),
])
def test_savings_rejects_stale_identity_revision_or_invalid_rules(tmp_path: Path, patch: dict, status: int) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    response = client(tmp_path).put("/api/account/lab-savings", headers=HEADERS, json={
        "expected_account_id": "ACCOUNT1", "expected_revision": 0,
        "share_mode": "save_pct", "share_pct": 25, **patch})
    assert response.status_code == status, response.text
    assert not (tmp_path / "build-route.json").exists()


def test_savings_requires_explicit_scope_and_handles_unattributed_history(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    db.connect(legacy).close()
    app = client(tmp_path, legacy)
    empty = app.get("/api/account/lab-savings", headers={"x-account-scope": "unattributed"})
    assert empty.status_code == 200 and empty.json()["editable"] is False
    response = app.put("/api/account/lab-savings", json={"expected_account_id": "ACCOUNT1",
        "expected_revision": 0, "share_mode": "save_pct", "share_pct": 25})
    assert response.status_code == 409
    assert not (tmp_path / "build-route.json").exists()


def test_existing_workshop_override_survives_savings_edit(tmp_path: Path) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    raw = RouteDocument.compatibility().to_dict()
    raw["overrides"] = {"Air_1": {"account_id": "ACCOUNT1", "patches": {
        "workshop.default": {"coin_spend_limit_pct": 40}}}}
    BuildRouteStore(tmp_path).publish(RouteDocument.from_dict(raw), 0, "test")
    result = client(tmp_path).put("/api/account/lab-savings", headers=HEADERS, json={
        "expected_account_id": "ACCOUNT1", "expected_revision": 1,
        "share_mode": "save_pct", "share_pct": 25})
    assert result.status_code == 200
    effective = resolve_route(BuildRouteStore(tmp_path).read(), "Air_1", "ACCOUNT1")
    assert effective.rules.coins.workshop_spend_limit_pct == 40
    assert effective.rules.coins.lab_share.pct == 25


def test_route_schema_rejects_jit_override_without_lab_list() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["overrides"] = {"Air_1": {"account_id": "ACCOUNT1", "patches": {},
        "lab_share": {"mode": "just_in_time", "pct": 25}}}
    with pytest.raises(ValueError, match="ranked lab list"):
        RouteDocument.from_dict(raw)


def test_stale_jit_override_does_not_break_new_account_assignment() -> None:
    raw = RouteDocument.compatibility().to_dict()
    raw["overrides"] = {"Air_1": {"account_id": "OLDACCOUNT", "patches": {},
        "lab_share": {"mode": "just_in_time", "pct": 25}}}
    raw["assignments"] = {"Air_1": {"account_id": "NEWACCOUNT", "strategy_id": "saved",
        "strategy_version": 1, "strategy_name": "New account", "baseline": raw["baseline"]}}
    route = RouteDocument.from_dict(raw)
    assert resolve_route(route, "Air_1", "NEWACCOUNT").rules.coins.lab_share.mode == "when_affordable"


@pytest.mark.parametrize("capabilities,allowed", [([], False), (["account_savings"], True)])
def test_saving_checks_other_listening_workers_before_new_schema_publication(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch, capabilities: list[str], allowed: bool) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    worker(tmp_path, "Air_2", "ACCOUNT2", 8002)

    def status(request: Any, timeout: float) -> io.BytesIO:
        if request.full_url.startswith("http://127.0.0.1:8001/"):
            raise URLError(ConnectionRefusedError("selected worker stopped"))
        return io.BytesIO(json.dumps({"bot": {"running": False},
            "runtime": {"capabilities": capabilities}}).encode())

    monkeypatch.setattr("web.account_overview.urlopen", status)
    result = client(tmp_path).put("/api/account/lab-savings", headers=HEADERS, json={
        "expected_account_id": "ACCOUNT1", "expected_revision": 0,
        "share_mode": "save_pct", "share_pct": 25})
    assert result.status_code == (200 if allowed else 412)
    assert (tmp_path / "build-route.json").exists() is allowed
    if not allowed:
        assert "Air_2" in result.json()["detail"] and "restart" in result.json()["detail"].lower()


def test_unreachable_worker_with_uncertain_status_blocks_schema_change(tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch) -> None:
    worker(tmp_path, "Air_1", "ACCOUNT1", 8001)

    def timeout(request: Any, timeout: float) -> io.BytesIO:
        raise TimeoutError("worker status timed out")

    monkeypatch.setattr("web.account_overview.urlopen", timeout)
    result = client(tmp_path).put("/api/account/lab-savings", headers=HEADERS, json={
        "expected_account_id": "ACCOUNT1", "expected_revision": 0,
        "share_mode": "save_pct", "share_pct": 25})
    assert result.status_code == 412
    assert not (tmp_path / "build-route.json").exists()
