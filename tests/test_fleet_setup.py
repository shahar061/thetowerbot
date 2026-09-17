"""UI setup persists only explicit, host-bound Fleet choices."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fleet.setup import FleetSetupStore, FleetSetupError, FleetSetupService
from fleet.dashboard import FleetRequestError
from web.app import create_app
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState


def qualification(root: Path, *, source: str = "Tiramisu64_6") -> None:
    directory = root / "m05-air6"
    directory.mkdir()
    (directory / "m05.json").write_text(json.dumps({
        "schema": 2, "state": "passed", "live": True, "source_instance": source,
        "source_evidence": {"tower_unopened": True, "lease_id": "source-lease"},
        "scope": {"host_id": "local-air", "bluestacks_version": "Air",
                  "source_lineage": "source", "source_version": "image",
                  "game_version": "29.0.3", "instance_config": {
                      "source_instance": source, "source_endpoint": "127.0.0.1:5615",
                      "source_lease": "source-lease"}},
    }))


def test_setup_discovers_existing_proof_and_persists_host_policy(tmp_path: Path) -> None:
    qualification(tmp_path)
    store = FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path)

    assert store.snapshot()["configured"] is False
    assert store.snapshot()["qualifications"][0]["id"] == "m05-air6"
    saved = store.configure(capacity=7, name_prefix="Tiramisu64_",
                            qualification_id="m05-air6", installed_prefix="Tiramisu64_",
                            host_count=5)

    assert saved["configured"] is True
    assert saved["settings"] == {"capacity": 7, "name_prefix": "Tiramisu64_",
                                  "qualification_id": "m05-air6"}
    assert FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path).settings() \
        == saved["settings"]
    assert (tmp_path / "fleet" / "settings.json").stat().st_mode & 0o777 == 0o600


def test_setup_rejects_arbitrary_source_and_prefix(tmp_path: Path) -> None:
    qualification(tmp_path)
    store = FleetSetupStore(tmp_path / "fleet", qualification_root=tmp_path)
    for values in (
        {"capacity": 5, "name_prefix": "Tiramisu64_", "qualification_id": "m05-air6"},
        {"capacity": 7, "name_prefix": "other_", "qualification_id": "m05-air6"},
        {"capacity": 7, "name_prefix": "Tiramisu64_", "qualification_id": "../other"},
    ):
        with pytest.raises(FleetSetupError):
            store.configure(**values, installed_prefix="Tiramisu64_", host_count=5)
    assert store.settings() is None


def test_start_instance_uses_exact_installed_row_and_never_opens_tower(tmp_path: Path) -> None:
    service = FleetSetupService(tmp_path / "fleet", qualification_root=tmp_path)
    started: list[str] = []
    service.controller = SimpleNamespace(adapter=SimpleNamespace(
        driver=SimpleNamespace(start=lambda name: started.append(name))))
    service.instances_snapshot = lambda: {"instances": [
        {"name": "Tiramisu64_6", "endpoint": "127.0.0.1:5615", "state": "running", "template": True},
        {"name": "Tiramisu64_18", "endpoint": "127.0.0.1:5735", "state": "stopped", "template": False},
    ], "can_start": True}

    service.start_instance("Tiramisu64_18")
    assert started == ["Tiramisu64_18"]
    with pytest.raises(FleetRequestError, match="instance_not_installed"):
        service.start_instance("Tiramisu64_99")
    assert started == ["Tiramisu64_18"]


def test_setup_api_routes_save_start_and_resume_before_generic_action() -> None:
    class Setup:
        def __init__(self) -> None:
            self.actions: list[str] = []

        def setup_snapshot(self) -> dict:
            return {"configured": False, "settings": None, "qualifications": [], "host": {}}

        def configure(self, *, capacity: int, name_prefix: str, qualification_id: str) -> dict:
            self.actions.append(f"save:{capacity}:{name_prefix}:{qualification_id}")
            return self.setup_snapshot()

        def start_source(self) -> dict:
            self.actions.append("start")
            return {"sources": [], "jobs": []}

        def resume_unopened_clone(self, job_id: str, index: int) -> dict:
            self.actions.append(f"resume:{job_id}:{index}")
            return {}

    setup = Setup()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=setup))
    assert client.get("/api/fleet/setup").status_code == 200
    assert client.post("/api/fleet/setup", json={"capacity": 7, "name_prefix": "Tiramisu64_",
                                                  "qualification_id": "m05-air6"}).status_code == 200
    assert client.post("/api/fleet/setup/start-source").status_code == 200
    assert client.post("/api/fleet/requests/job/targets/0/resume-first-launch").status_code == 200
    assert setup.actions == ["save:7:Tiramisu64_:m05-air6", "start", "resume:job:0"]


def test_manual_reroll_pool_api_routes_are_reachable() -> None:
    class Pool:
        def reroll_snapshot(self) -> dict:
            return {"candidates": [], "members": []}

        def reroll_add(self, names: list[str]) -> dict:
            return {"candidates": [], "members": [{"name": name} for name in names]}

        def reroll_remove(self, name: str) -> dict:
            return {"candidates": [{"name": name}], "members": []}

    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=Pool()))
    assert client.get("/api/fleet/reroll").json() == {"candidates": [], "members": []}
    assert client.post("/api/fleet/reroll/members", json={"names": ["Tiramisu64_20"]}).json()[
        "members"] == [{"name": "Tiramisu64_20"}]
    assert client.delete("/api/fleet/reroll/members/Tiramisu64_20").json()[
        "candidates"] == [{"name": "Tiramisu64_20"}]


def test_manual_reroll_actions_and_journal_routes() -> None:
    class Pool:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reroll_start(self, name: str | None = None) -> dict:
            self.calls.append(f"start:{name}")
            return {"members": []}

        def reroll_pause(self, name: str | None = None) -> dict:
            self.calls.append(f"pause:{name}")
            return {"members": []}

        def reroll_set_concurrency(self, limit: int) -> dict:
            self.calls.append(f"limit:{limit}")
            return {"concurrency_limit": limit}

        def reroll_journal(self, *, cursor: int | None = None) -> dict:
            return {"entries": [], "next_cursor": cursor or 0}

    pool = Pool()
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=pool))
    assert client.post("/api/fleet/reroll/start").status_code == 200
    assert client.post("/api/fleet/reroll/members/Tiramisu64_20/start").status_code == 200
    assert client.post("/api/fleet/reroll/pause").status_code == 200
    assert client.post("/api/fleet/reroll/members/Tiramisu64_20/pause").status_code == 200
    assert client.patch("/api/fleet/reroll/concurrency", json={"limit": 4}).json() == {
        "concurrency_limit": 4}
    assert client.get("/api/fleet/reroll/journal?cursor=5").json() == {
        "entries": [], "next_cursor": 5}
    assert pool.calls == ["start:None", "start:Tiramisu64_20", "pause:None",
                          "pause:Tiramisu64_20", "limit:4"]
