"""Account selection must never relabel shared or another worker's history."""

from __future__ import annotations

import json
import io
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

import db
from autopilot import AutopilotState
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def _worker(root: Path, name: str, account_id: str) -> Path:
    path = root / "workers" / name
    binding = path / "checkpoints" / ("a" * 32 + ".json")
    binding.parent.mkdir(parents=True)
    identity = {"worker_id": name, "account_id": account_id,
                "endpoint": "127.0.0.1:5555", "lease_id": "lease"}
    binding.write_text(json.dumps(identity))
    (path / "fleet-registration.json").write_text(json.dumps({
        **identity, "state": "registered", "instance": name, "binding": str(binding),
        "web_port": 10000 + int(name.rsplit("_", 1)[-1]),
    }))
    return path / "tower_bot.db"


def test_catalog_scopes_history_and_keeps_legacy_unattributed(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.db"
    conn = db.connect(legacy)
    db.start_run(conn, 1, 1.0)
    db.insert_event(conn, {"seq": 1, "run_id": 1, "ts": 1.0, "type": "Tapped",
                           "screen": "IN_RUN", "action": "Damage", "reason": None,
                           "score": None, "price": None, "wallet": None, "detail": "{}"})
    conn.close()
    fleet_root = tmp_path / "fleet"
    worker_db = _worker(fleet_root, "Tiramisu64_18", "ABC12345")
    db.bind_account(worker_db, "ABC12345")
    conn = db.connect(worker_db)
    db.start_run(conn, 2, 2.0)
    db.finish_run(conn, 2, started_at=2.0, ended_at=3.0, wave=12, coins=5,
                  tier=1, abandoned=False, scan_count=1, tap_count=0)
    conn.close()
    fleet = type("Fleet", (), {"root": fleet_root})()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=legacy, fleet=fleet))

    catalog = client.get("/api/accounts?local_only=true").json()
    assert catalog["active"] is None
    assert {item["key"] for item in catalog["accounts"]} == {
        "worker:Tiramisu64_18", "unattributed"}
    assert client.get("/api/runs", headers={"x-account-scope": "worker:Tiramisu64_18"}).json()[0]["id"] == 2
    assert client.get("/api/runs/1/events", headers={"x-account-scope": "worker:Tiramisu64_18"}).json() == []
    assert [row["id"] for row in client.get("/api/stats", headers={"x-account-scope": "worker:Tiramisu64_18"}).json()["runs"]] == [2]
    assert client.get("/api/runs", headers={"x-account-scope": "unattributed"}).json()[0]["id"] == 1
    assert client.get("/api/runs", headers={"x-account-scope": "worker:missing"}).status_code == 404
    assert client.get("/api/status", headers={"x-account-scope": "worker:Tiramisu64_18"}).status_code == 409
    assert client.get("/api/frame.jpg?scope=worker:Tiramisu64_18").status_code == 409


def test_duplicate_or_changed_binding_cannot_claim_account(tmp_path: Path) -> None:
    root = tmp_path / "fleet"
    first = _worker(root, "Tiramisu64_18", "ABC12345")
    _worker(root, "Tiramisu64_19", "ABC12345")
    db.connect(first).close()
    fleet = type("Fleet", (), {"root": root})()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    assert [item["key"] for item in client.get("/api/accounts?local_only=true").json()["accounts"]] == [
        "unattributed:Tiramisu64_18"]


def test_old_worker_history_is_unattributed_until_database_is_bound(tmp_path: Path) -> None:
    root = tmp_path / "fleet"
    path = _worker(root, "Tiramisu64_18", "ABC12345")
    conn = db.connect(path)
    db.start_run(conn, 4, 4.0)
    conn.close()
    fleet = type("Fleet", (), {"root": root})()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    accounts = client.get("/api/accounts?local_only=true").json()["accounts"]
    assert {item["key"] for item in accounts} == {
        "worker:Tiramisu64_18", "unattributed:Tiramisu64_18"}
    assert client.get("/api/runs", headers={"x-account-scope": "worker:Tiramisu64_18"}).json() == []
    assert client.get("/api/runs", headers={"x-account-scope": "unattributed:Tiramisu64_18"}).json()[0]["id"] == 4
    with pytest.raises(ValueError, match="unattributed history"):
        db.bind_account(path, "ABC12345")


def test_catalog_recognizes_only_matching_running_worker_dashboard(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "fleet"
    path = _worker(root, "Tiramisu64_18", "ABC12345")
    db.bind_account(path, "ABC12345")
    fleet = type("Fleet", (), {"root": root})()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=fleet))
    payload = {"active": "worker:Tiramisu64_18", "accounts": [{
        "key": "worker:Tiramisu64_18", "account_id": "ABC12345", "running": True,
    }]}
    monkeypatch.setattr("web.app.urlopen", lambda url, timeout: io.BytesIO(json.dumps(payload).encode()))
    result = client.get("/api/accounts").json()
    assert result["active"] == "worker:Tiramisu64_18"
    assert result["accounts"][0]["dashboard_url"] == "http://127.0.0.1:10018/"
    payload["accounts"][0]["account_id"] = "WRONG"
    assert client.get("/api/accounts").json()["active"] is None


def test_observations_alone_prevent_rebinding(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO account_observations(revision_id, detail) VALUES (1, '{}')")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="unattributed history"):
        db.bind_account(path, "ABC12345")


def test_local_running_worker_is_default_even_when_remote_sorts_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "fleet"
    remote = _worker(root, "Tiramisu64_18", "ABC12345")
    local = _worker(root, "Tiramisu64_19", "DEF67890")
    db.bind_account(remote, "ABC12345")
    db.bind_account(local, "DEF67890")
    fleet = type("Fleet", (), {"root": root})()
    runner = type("Runner", (), {"autopilot_state": AutopilotState(),
                                 "status": lambda self: {"running": True},
                                 "verified_account": lambda self: "DEF67890"})()
    payload = {"active": "worker:Tiramisu64_18", "accounts": [{
        "key": "worker:Tiramisu64_18", "account_id": "ABC12345", "running": True,
    }]}
    monkeypatch.setattr("web.app.urlopen", lambda url, timeout: io.BytesIO(json.dumps(payload).encode()))
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=local, fleet=fleet, runner=runner))
    assert client.get("/api/accounts").json()["active"] == "worker:Tiramisu64_19"
