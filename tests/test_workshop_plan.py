"""The Workshop plan graph's read endpoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

import db
from events import EventBus
from fleet.workshop_plan import load_workshop_plan
from fleet.build_route_runtime import BuildRouteRuntime
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
        "budget": {"wallet": 0, "jar": 0}, "strategy": {"id": None, "name": "Baseline", "mode": "blocks"},
        "upgrade_names": {},
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


def test_a_record_missing_a_required_section_reads_as_none(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    _write(worker, "ACCOUNT_A", 16, 16)
    path = worker / "build-route-workshop.json"
    record = json.loads(path.read_text())
    for key in ("budget", "strategy", "upgrade_names"):
        path.write_text(json.dumps({k: v for k, v in record.items() if k != key}))
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


def _selection(worker: Path, at: float, upgrade_id: str = "health") -> dict[str, Any]:
    _write(worker, "ACCOUNT_A", 16, 16, at)
    record = json.loads((worker / "build-route-workshop.json").read_text())
    record.update(visit_id="visit-1", decision_sequence=0, purchase_count=0)
    record["evaluation"] = {"account_id": "ACCOUNT_A", "decision": {
        "account_id": "ACCOUNT_A", "state": "buy", "upgrade_id": upgrade_id,
        "item": "Health", "price": 280}, "trace": {"selection": "value", "candidates": [
            {"upgrade_id": upgrade_id, "name": "Health", "score": 20.0,
             "weight": 14, "odds": None, "chosen": True}]}}
    return record


def test_selection_history_deduplicates_refreshes_and_keeps_original_time(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    runtime = BuildRouteRuntime(tmp_path, worker.name, "ACCOUNT_A")
    first = _selection(worker, 100)
    runtime.publish_workshop_plan(first)
    refresh = {**first, "written_at": 120, "budget": {"wallet": 999, "jar": 0},
               "evaluation": {**first["evaluation"], "trace": {"selection": "value", "candidates": [
                   {"upgrade_id": "health", "score": 99, "chosen": True}]}}}
    runtime.publish_workshop_plan(refresh)
    second = {**first, "written_at": 150, "purchase_count": 1}
    runtime.publish_workshop_plan(second)
    history = load_workshop_plan(worker, "ACCOUNT_A", 160)["history"]
    assert len(history) == 1  # Current selection stays in the current graph.
    assert history[0]["selected_at"] == 100
    assert history[0]["score"] == 20.0
    assert history[0]["outcome"] == "buy_selected"
    assert history[0]["plan"]["evaluation"]["trace"]["selection"] == "value"
    assert history[0]["plan"]["budget"]["wallet"] == 0


def test_history_links_only_explicit_confirmed_purchases_and_preserves_legacy_buys(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    runtime = BuildRouteRuntime(tmp_path, worker.name, "ACCOUNT_A")
    record = _selection(worker, 100)
    runtime.publish_workshop_plan(record)
    saved = json.loads((worker / "build-route-workshop.json").read_text())
    with db.connect(worker / "tower_bot.db") as conn:
        for at, dry, verdict, plan_id in [(110, 0, "bought", saved["id"]),
                                         (90, 0, "bought", None),
                                         (130, 1, "bought", saved["id"]),
                                         (140, 0, "inconclusive", saved["id"])]:
            conn.execute("INSERT INTO ledger(ts,kind,item,category,currency,dry_run,price,detail) "
                         "VALUES(?, 'WORKSHOP_BUY','Health','DEFENSE','coins',?,280,?)",
                         (at, dry, json.dumps({"verdict": verdict, "workshop_plan_id": plan_id})))
    history = load_workshop_plan(worker, "ACCOUNT_A", 160)["history"]
    assert [row["purchased_at"] for row in history] == [110, 90]
    assert history[0]["score"] == 20.0
    assert history[0]["outcome"] == "bought"
    assert history[1]["score"] is None
    assert history[1]["plan"] is None


def test_history_is_bounded_and_account_scoped_without_a_current_plan(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    runtime = BuildRouteRuntime(tmp_path, worker.name, "ACCOUNT_A")
    record = _selection(worker, 100)
    for index in range(105):
        runtime.publish_workshop_plan({**record, "written_at": 100 + index, "purchase_count": index})
    (worker / "build-route-workshop.json").unlink()
    history = load_workshop_plan(worker, "ACCOUNT_A", 220)["history"]
    assert len(history) == 100
    assert history[0]["selected_at"] == 204
    assert history[-1]["selected_at"] == 105
    assert load_workshop_plan(worker, "ACCOUNT_B", 220) == {"plan": None}


def test_corrupt_history_does_not_hide_current_graph_or_legacy_purchases(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    _write(worker, "ACCOUNT_A", 16, 16)
    path = worker / "build-route-workshop-history.json"
    path.write_text('{"account_id":')
    assert load_workshop_plan(worker, "ACCOUNT_A", 160)["plan"] is not None
    path.write_text(json.dumps({"account_id": "ACCOUNT_A", "records": [None, {}, {
        **_selection(worker, 100), "id": "bad-time", "selected_at": float("nan")}, {
        **_selection(worker, 100), "id": "foreign", "selected_at": 100, "account_id": "ACCOUNT_B"}]}))
    assert load_workshop_plan(worker, "ACCOUNT_A", 160)["history"] == []


def test_malformed_nested_snapshots_are_skipped_individually(tmp_path: Path) -> None:
    worker = _worker(tmp_path, "Tiramisu64_18", "ACCOUNT_A")
    record = _selection(worker, 100)
    good = {**record, "id": "good", "selected_at": 100}
    corrupt = [{**good, "id": "bad-trace", "evaluation": {"decision": {}, "trace": []}},
               {**good, "id": "bad-decision", "evaluation": {"decision": "health", "trace": {}}},
               {**good, "id": "bad-candidates", "evaluation": {"decision": {}, "trace": {"candidates": None}}}]
    (worker / "build-route-workshop-history.json").write_text(json.dumps({
        "account_id": "ACCOUNT_A", "records": [good, *corrupt]}))
    response = load_workshop_plan(worker, "ACCOUNT_A", 160)
    assert response["plan"] is not None
    assert [row["id"] for row in response["history"]] == ["good"]
