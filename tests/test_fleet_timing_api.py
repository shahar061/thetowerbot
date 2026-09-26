"""The Reroll Settings page sets one menu pace for the whole fleet."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from events import EventBus
from fleet import reroll_timing
from fleet.setup import FleetSetupService
from sinks.sse import SseSink
from sinks.state import BotState
from web.account_catalog import AccountChoice
from web.app import create_app


def _client(root: Path) -> TestClient:
    fleet = FleetSetupService(root, qualification_root=root / "qualifications")
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 db_path=None, fleet=fleet))


def test_get_returns_the_default_before_anything_is_saved(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/api/fleet/reroll/timing")
    assert response.status_code == 200
    assert response.json() == {"menu_interval": 2.0}


def test_put_saves_and_pushes_to_running_workers(tmp_path: Path, monkeypatch) -> None:
    choices = [
        AccountChoice("a", "acc-a", "Air_1", tmp_path / "a.db", "worker", 10001),
        AccountChoice("b", "acc-b", "Air_2", tmp_path / "b.db", "worker", 10002),
    ]
    monkeypatch.setattr("web.app.account_choices", lambda *_: choices)
    sent: list[int] = []

    def patch(port: int, body: dict) -> None:
        if port == 10002:
            raise OSError("refused")
        sent.append(port)

    monkeypatch.setattr(reroll_timing, "_patch", patch)
    client = _client(tmp_path)

    response = client.put("/api/fleet/reroll/timing", json={"menu_interval": 0.7})

    assert response.status_code == 200
    assert response.json() == {"menu_interval": 0.7, "workers_updated": 1,
                               "workers_not_running": 1}
    assert sent == [10001]
    assert client.get("/api/fleet/reroll/timing").json() == {"menu_interval": 0.7}


@pytest.mark.parametrize("value", [0.0, 5000, True, "fast"])
def test_put_rejects_an_invalid_pace_without_saving(tmp_path: Path, value: object) -> None:
    response = _client(tmp_path).put("/api/fleet/reroll/timing", json={"menu_interval": value})
    assert response.status_code == 422
    assert reroll_timing.load(tmp_path) is None


def test_timing_is_unavailable_without_a_fleet() -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None))
    assert client.get("/api/fleet/reroll/timing").status_code == 503
