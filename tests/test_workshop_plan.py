"""The Workshop plan graph's read endpoint."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import db
from events import EventBus
from fleet.workshop_plan import load_workshop_plan
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def _worker(root: Path, name: str, account_id: str) -> Path:
    path = root / "workers" / name
    binding = path / "checkpoints" / ("a" * 32 + ".json")
    binding.parent.mkdir(parents=True)
    identity = {"worker_id": name, "account_id": account_id, "endpoint": "127.0.0.1:5555", "lease_id": "lease"}
    binding.write_text(json.dumps(identity))
    (path / "fleet-registration.json").write_text(json.dumps({
        **identity, "state": "registered", "instance": name, "binding": str(binding), "web_port": 10018}))
    db.bind_account(path / "tower_bot.db", account_id)
    return path


def _write(worker: Path, account_id: str, revision: int, applied: int | None, written_at: float = 100.0) -> None:
    (worker / "build-route-workshop.json").write_text(json.dumps({
        "account_id": account_id, "revision": revision, "written_at": written_at,
        "evaluation": {"decision": None, "trace": {"matched_rule_id": "blocks", "reason": "Wait"}}}))
    if applied is not None:
        (worker / "build-route-applied.json").write_text(json.dumps({"account_id": account_id, "revision": applied}))


def test_no_file_reads_as_none(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    assert load_workshop_plan(worker, "ACCOUNT_A", 200.0) == {"plan": None}


def test_matching_revision_is_fresh_and_aged(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    _write(worker, "ACCOUNT_A", 16, 16)
    loaded = load_workshop_plan(worker, "ACCOUNT_A", 160.0)
    assert (loaded["stale"], loaded["current_revision"], loaded["age_seconds"]) == (False, 16, 60.0)
    assert loaded["plan"]["revision"] == 16


def test_changed_strategy_is_stale(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    _write(worker, "ACCOUNT_A", 15, 16)
    assert load_workshop_plan(worker, "ACCOUNT_A", 160.0)["stale"] is True


def test_corrupt_or_foreign_plan_reads_as_none(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    (worker / "build-route-workshop.json").write_text('{"account_id": "ACCOUNT_A", "revis')
    assert load_workshop_plan(worker, "ACCOUNT_A", 160.0) == {"plan": None}
    _write(worker, "ACCOUNT_B", 16, 16)
    assert load_workshop_plan(worker, "ACCOUNT_A", 160.0) == {"plan": None}
    (worker / "build-route-workshop.json").write_text(json.dumps(["not", "a", "record"]))
    assert load_workshop_plan(worker, "ACCOUNT_A", 160.0) == {"plan": None}


def test_endpoint_is_scoped_to_the_selected_worker(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    _write(worker, "ACCOUNT_A", 16, 16)
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=type("Fleet", (), {"root": tmp_path})()))
    scope = {"x-account-scope": "worker:Tiramisu64_18"}
    response = client.get("/api/workshop-plan", headers={**scope, "x-expected-account-id": "ACCOUNT_A"})
    assert response.status_code == 200
    assert response.json()["plan"]["account_id"] == "ACCOUNT_A"
    assert client.get("/api/workshop-plan", headers={**scope, "x-expected-account-id": "ACCOUNT_B"}).status_code == 409
    assert client.get("/api/workshop-plan").json() == {"plan": None}
