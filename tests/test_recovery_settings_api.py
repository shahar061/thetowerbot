"""Nonsecret recovery settings API stays off without a scan producer."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from web.app import create_app


class _Fleet:
    def __init__(self, root: Path) -> None:
        self.root = root


def test_recovery_api_default_unknown_and_revisioned_update(tmp_path: Path) -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=_Fleet(tmp_path)))
    first = client.get("/api/fleet/recovery/settings")
    assert first.status_code == 200
    before = first.json()
    assert before["settings"]["mode"] == "off"
    assert before["status"]["producer"] == "unknown"
    assert before["status"]["workers"] == []
    assert before["policy"]["active"]["daily_limit_microusd"] == 1_000_000
    assert before["daily_budget"]["remaining_microusd"] == 1_000_000
    assert before["budget_observed_at_utc"]
    assert "api_key" not in str(before).lower()

    settings = dict(before["settings"], mode="shadow", daily_limit_microusd=500_000)
    body = {"settings": settings, "shadow_worker": "worker-a", "expected_settings_revision": 1,
            "expected_policy_revision": 1}
    saved = client.put("/api/fleet/recovery/settings", json=body)
    assert saved.status_code == 200
    assert saved.json()["settings_revision"] == 2
    assert saved.json()["shadow_worker"] == "worker-a"
    assert saved.json()["policy"]["active"]["revision"] == 2
    assert client.put("/api/fleet/recovery/settings", json=body).status_code == 409
    assert client.get("/api/fleet/recovery/settings").json()["settings"]["mode"] == "shadow"


def test_recovery_api_rejects_assist_without_canary_and_secret_field(tmp_path: Path) -> None:
    client = TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                   db_path=None, fleet=_Fleet(tmp_path)))
    settings = client.get("/api/fleet/recovery/settings").json()["settings"]
    assist = client.put("/api/fleet/recovery/settings", json={
        "settings": dict(settings, mode="assist"), "expected_settings_revision": 1,
        "expected_policy_revision": 1})
    assert assist.status_code == 422
    assert assist.json()["detail"] == "assist_uncalibrated"
    secret = client.put("/api/fleet/recovery/settings", json={
        "settings": dict(settings, api_key="synthetic"), "expected_settings_revision": 1,
        "expected_policy_revision": 1})
    assert secret.status_code == 422
    assert "synthetic" not in secret.text
