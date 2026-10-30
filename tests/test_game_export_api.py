"""Account fencing and background collection for official export reporting."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from tests.test_single_account_state import HEADERS, client, worker
from web.account_catalog import registered_worker
from evidence_scope import FactScope
from account_state import AccountState
from fleet.identity import IdentityEvidence
from web.game_exports import ExportSource, ExportCollector, collect_once, registered_source, read_bound_projection, local_source


def status(endpoint: str = "127.0.0.1:9001") -> dict[str, Any]:
    return {"bot": {"running": True, "since": 100.0},
            "runtime": {"device": {"serial": endpoint}},
            "reporting_scope": {"account_id": "ACCOUNT1", "endpoint": endpoint,
                "lease_id": "lease", "generation": "gen1", "epoch": 1,
                "verified_since": 110.0}}


def register(tmp_path: Path) -> Any:
    path = worker(tmp_path, "Air_1", "ACCOUNT1", 8001)
    record = json.loads((path.parent / "fleet-registration.json").read_text())
    binding = Path(record["binding"])
    binding.write_text(json.dumps({**json.loads(binding.read_text()),
                                   "generation": "gen1", "observed_at": 110.0}))
    return registered_worker(path.parent)


def test_registered_source_requires_verified_account_endpoint_and_generation(tmp_path: Path) -> None:
    choice = register(tmp_path)
    source = registered_source(choice, status())
    assert source is not None
    assert source.account_id == "ACCOUNT1" and source.verified_since == 110
    assert registered_source(choice, status("127.0.0.1:9999")) is None
    stopped = status()
    stopped["bot"]["running"] = False
    assert registered_source(choice, stopped) is None
    assert registered_source(replace(choice, account_id="REPLACED"), status()) is None


def test_remote_source_requires_current_scope_even_when_account_is_known(tmp_path: Path) -> None:
    choice = register(tmp_path)
    invalidated = status()
    invalidated["reporting_scope"] = None
    assert registered_source(choice, invalidated) is None
    del invalidated["reporting_scope"]
    assert registered_source(choice, invalidated) is None


def test_local_capture_uses_current_reverification_time_not_old_checkpoint(tmp_path: Path) -> None:
    choice = register(tmp_path)
    runner = SimpleNamespace(verified_account=lambda: "ACCOUNT1",
        status=lambda: status()["bot"], identity=lambda: status()["runtime"]["device"])
    accounts = SimpleNamespace(verified_scope=FactScope("ACCOUNT1", "lease", "gen1", 2),
                               verified_identity_at=125.0)
    source = local_source(choice, runner, accounts)
    assert source is not None and source.verified_since == 125


def test_current_identity_time_clears_and_advances_with_reverification() -> None:
    state = AccountState()
    scope = FactScope("ACCOUNT1", "lease", "gen1", 0)
    state.bind_scope(scope, identity=IdentityEvidence("ACCOUNT1", 110, "first-proof"))
    assert state.verified_identity_at == 110
    state.invalidate_scope("reconnect")
    assert state.verified_identity_at is None and state.verified_scope is None
    state.bind_scope(replace(scope, epoch=1), identity=IdentityEvidence("ACCOUNT1", 125, "new-proof"))
    assert state.verified_identity_at == 125


def test_rejected_collection_cannot_mark_revoked_source_online(tmp_path: Path, monkeypatch: Any) -> None:
    choice = register(tmp_path)
    proof = registered_source(choice, status())
    current = [proof]

    async def revoked(*_args: Any, **_kwargs: Any) -> bool:
        current[0] = None
        return False

    monkeypatch.setattr("web.game_exports.collect_once", revoked)
    collector = ExportCollector(lambda: [choice], lambda _: current[0])
    asyncio.run(collector.poll())
    assert choice.key not in collector.online


def test_capture_rejects_snapshot_from_before_verified_generation(tmp_path: Path) -> None:
    choice = register(tmp_path)
    source = registered_source(choice, status())
    assert source is not None

    async def read(_endpoint: str) -> bytes:
        return json.dumps({"ExportVersion": 1, "ExportedAt": "1970-01-01T00:01:40Z"}).encode()

    assert asyncio.run(collect_once(choice, lambda _: source, read_file=read, now=120)) is False
    assert not (choice.db_path.parent / "game-export.sqlite3").exists()


def test_capture_rechecks_scope_after_read(tmp_path: Path) -> None:
    choice = register(tmp_path)
    source = registered_source(choice, status())
    assert source is not None
    current: list[ExportSource | None] = [source]

    async def read(_endpoint: str) -> bytes:
        current[0] = replace(source, scope_key="replacement")
        return json.dumps({"ExportVersion": 1, "ExportedAt": "1970-01-01T00:02:00Z"}).encode()

    assert asyncio.run(collect_once(choice, lambda _: current[0], read_file=read, now=121)) is False
    assert not (choice.db_path.parent / "game-export.sqlite3").exists()


def test_capture_persists_once_and_new_lease_preserves_historical_account_data(tmp_path: Path) -> None:
    choice = register(tmp_path)
    source = registered_source(choice, status())
    assert source is not None

    async def read(_endpoint: str) -> bytes:
        return json.dumps({"ExportVersion": 1, "ExportedAt": "1970-01-01T00:02:00Z",
            "CurrentWave": 42, "CurrentTier": 1, "RoundActiveBool": True}).encode()

    assert asyncio.run(collect_once(choice, lambda _: source, read_file=read, now=121)) is True
    assert asyncio.run(collect_once(choice, lambda _: source, read_file=read, now=122)) is False
    projection = read_bound_projection(choice, now=122)
    assert projection["available"] is True
    assert projection["exported_at"] == 120 and projection["captured_at"] == 121
    assert projection["current_run"]["wave"] == 42
    assert projection["current_run"]["estimated_wave"] is None
    record_path = choice.db_path.parent / "fleet-registration.json"
    record = json.loads(record_path.read_text())
    binding_path = Path(record["binding"])
    binding = json.loads(binding_path.read_text())
    record["endpoint"] = binding["endpoint"] = "127.0.0.1:9009"
    record_path.write_text(json.dumps(record))
    binding_path.write_text(json.dumps(binding))
    historical = read_bound_projection(choice, online=True, now=123)
    assert historical["available"] is True and historical["historical_only"] is True
    assert historical["source_online"] is False and historical["stale"] is True
    assert historical["current_run"]["estimated_wave"] is None


def test_selected_export_is_explicitly_missing_and_account_scoped(tmp_path: Path) -> None:
    register(tmp_path)
    app = client(tmp_path)
    response = app.get("/api/game-export", headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert payload["account_id"] == "ACCOUNT1" and payload["available"] is False
    assert app.get("/api/game-export").json()["available"] is False
    assert app.get("/api/game-export", headers={**HEADERS,
        "x-expected-account-id": "OTHER"}).status_code == 409
    assert not (tmp_path / "workers/Air_1/game-export.sqlite3").exists()


def test_fleet_exports_keep_missing_accounts_separate(tmp_path: Path) -> None:
    register(tmp_path)
    worker(tmp_path, "Air_2", "ACCOUNT2", 8002)
    response = client(tmp_path).get("/api/game-exports")
    assert response.status_code == 200
    rows = response.json()["accounts"]
    assert {row["account_id"] for row in rows} == {"ACCOUNT1", "ACCOUNT2"}
    assert all(row["available"] is False for row in rows)


def test_collector_is_cancelled_with_app_lifespan(tmp_path: Path, monkeypatch: Any) -> None:
    register(tmp_path)
    events: list[str] = []

    async def background(_self: Any) -> None:
        events.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            events.append("cancelled")

    monkeypatch.setattr(ExportCollector, "run", background)
    with client(tmp_path) as app:
        assert app.get("/api/game-export", headers=HEADERS).status_code == 200
        assert events == ["started"]
    assert events == ["started", "cancelled"]
