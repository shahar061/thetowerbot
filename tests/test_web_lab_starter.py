"""The owner's starter-rollout actions over HTTP."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from events import EventBus
from fleet.setup import FleetSetupService
from lab_starter_rollout import LabStarterRollout
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


def fleet_client(root: Path, monkeypatch) -> TestClient:
    """Build a TestClient over a fleet app rooted at `root`, mirroring
    test_labs_view.py's `_client` (no visible workers needed here). There is
    no tests/test_web_app.py in this repo yet, so this is that fixture
    builder, defined once for the starter-rollout HTTP tests."""
    del monkeypatch  # kept for signature parity with the brief; unused
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    fleet._reroll_pool = SimpleNamespace(members=lambda: [])
    fleet._reroll_runs = SimpleNamespace(hidden_names=lambda: set())
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def client(tmp_path: Path, monkeypatch) -> TestClient:
    return fleet_client(tmp_path, monkeypatch)


def test_reset_puts_a_halted_slot_back_to_dry_run(tmp_path: Path, monkeypatch) -> None:
    web = client(tmp_path, monkeypatch)
    starter = LabStarterRollout(tmp_path)
    starter.halt("start:2", "Start was not proven")
    response = web.post("/api/fleet/labs/starter-rollout/start:2/reset")
    assert response.status_code == 200
    assert response.json()["starter_rollout"][1]["stage"] == "dry_run"
    assert web.post("/api/fleet/labs/starter-rollout/start:2/reset").status_code == 409
    assert web.post("/api/fleet/labs/starter-rollout/start:9/reset").status_code == 422


def test_accept_and_reset_act_on_one_lab(tmp_path: Path, monkeypatch) -> None:
    web = client(tmp_path, monkeypatch)
    starter = LabStarterRollout(tmp_path)
    starter.note_start_dry_run(2, "Air_1", "a", "labs.ban-perks", 1, 5000, 60., 1.)
    assert web.post("/api/fleet/labs/rehearsed/labs.ban-perks/accept").status_code == 200
    assert starter.state().lab("labs.ban-perks").status == "rehearsed"
    assert web.post("/api/fleet/labs/rehearsed/labs.ban-perks/reset").status_code == 409
