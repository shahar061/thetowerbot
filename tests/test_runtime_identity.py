from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
import runtime_identity
from control import Controls
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from strategy import Strategy
from web import app as web_app


@pytest.mark.parametrize("relative", [
    "fleet/recovery.py", "catalog/labs.v1.json", "catalog/workshop-prices.v1.json",
])
def test_execution_inputs_change_hash(tmp_path: Path, relative: str) -> None:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("one")
    previous = runtime_identity.source_hash(tmp_path)
    path.write_text("two")
    assert runtime_identity.source_hash(tmp_path) != previous


def test_hash_ignores_mutable_runtime_files(tmp_path: Path) -> None:
    (tmp_path / "worker.py").write_text("source")
    previous = runtime_identity.source_hash(tmp_path)
    for relative in ("worker.log", "screenshots/frame.json", "runtime/status.json",
                     ".venv/lib/generated.py", "web/ui/node_modules/package.json"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("mutable")
    assert runtime_identity.source_hash(tmp_path) == previous


def test_hash_is_independent_of_file_creation_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, names in ((first, ("fleet/deep/b.py", "catalog/a.json", "a.py")),
                        (second, ("a.py", "catalog/a.json", "fleet/deep/b.py"))):
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
    assert runtime_identity.source_hash(first) == runtime_identity.source_hash(second)


def test_backend_hash_includes_runtime_concept_metadata(tmp_path: Path) -> None:
    (tmp_path / "worker.py").write_text("VERSION = 1\n")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    data = catalog / "concepts.v1.json"
    data.write_text('{"registry_version": "one"}')
    before = runtime_identity.source_hash(tmp_path)
    data.write_text('{"registry_version": "two"}')
    assert runtime_identity.source_hash(tmp_path) != before


def test_captured_backend_identity_does_not_follow_later_source_edits(tmp_path: Path) -> None:
    (tmp_path / "worker.py").write_text("OLD = True\n")
    captured = runtime_identity.capture_backend_identity(tmp_path, started_at=123.0)

    (tmp_path / "worker.py").write_text("NEW = True\n")

    assert captured.started_at == 123.0
    assert captured.source_hash != runtime_identity.source_hash(tmp_path)


def test_backend_hash_includes_web_python_but_excludes_ui_dependencies(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    source = web / "advisor.py"
    source.write_text("VERSION = 1\n")
    before = runtime_identity.source_hash(tmp_path)
    source.write_text("VERSION = 2\n")
    assert runtime_identity.source_hash(tmp_path) != before

    before_dependency = runtime_identity.source_hash(tmp_path)
    dependency = web / "ui" / "node_modules" / "package"
    dependency.mkdir(parents=True)
    (dependency / "helper.py").write_text("UNRELATED = True\n")
    assert runtime_identity.source_hash(tmp_path) == before_dependency


def test_frontend_manifest_is_read_each_time(tmp_path: Path) -> None:
    manifest = tmp_path / ".build-manifest.json"
    manifest.write_text(json.dumps({"hash": "first", "backend_hash": "one", "built_at": 1}))
    assert runtime_identity.read_frontend_identity(manifest).source_hash == "first"

    manifest.write_text(json.dumps({"hash": "second", "backend_hash": "two", "built_at": 2}))

    served = runtime_identity.read_frontend_identity(manifest)
    assert (served.source_hash, served.expected_backend_hash, served.built_at) == (
        "second", "two", 2.0
    )


def test_non_object_frontend_manifest_is_reported_as_unknown(tmp_path: Path) -> None:
    manifest = tmp_path / ".build-manifest.json"
    manifest.write_text("[]")

    assert runtime_identity.read_frontend_identity(manifest) == runtime_identity.FrontendIdentity()


class Runner:
    autopilot_state = None

    def __init__(self, running: bool = True) -> None:
        from autopilot import AutopilotState

        self.autopilot_state = AutopilotState()
        self.running = running

    def status(self) -> dict:
        return {"running": self.running, "since": 1.0 if self.running else None, "error": None}

    def identity(self) -> dict:
        return {"serial": "emulator-5554", "game_version": None}


def _client(monkeypatch, tmp_path: Path, *, runner: Runner | None, controls: Controls | None) -> TestClient:
    manifest = tmp_path / ".build-manifest.json"
    manifest.write_text(json.dumps({"hash": "ui-hash", "backend_hash": "backend-hash", "built_at": 5}))
    monkeypatch.setattr(web_app, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(
        web_app,
        "PROCESS_IDENTITY",
        runtime_identity.BackendIdentity("rev", "backend-hash", 10.0),
    )
    return TestClient(web_app.create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, runner=runner, controls=controls,
    ))


def test_status_reports_runtime_contract_and_honest_observing_state(monkeypatch, tmp_path: Path) -> None:
    controls = Controls(strategy=Strategy.from_config())
    client = _client(monkeypatch, tmp_path, runner=Runner(), controls=controls)

    runtime = client.get("/api/status").json()["runtime"]

    assert runtime == {
        "api_version": 1,
        "backend": {"revision": "rev", "source_hash": "backend-hash", "started_at": 10.0},
        "frontend": {"source_hash": "ui-hash", "expected_backend_hash": "backend-hash", "built_at": 5.0},
        "capabilities": ["control", "lifecycle", "autopilot", "advisor"],
        "profile": controls.snapshot().strategy.name,
        "device": {"serial": "emulator-5554", "game_version": None},
        "readiness": {
            "mode": "observing",
            "reasons": ["waiting for an in-run screen observation"],
        },
    }


def test_status_reloads_the_served_manifest(monkeypatch, tmp_path: Path) -> None:
    client = _client(monkeypatch, tmp_path, runner=None, controls=None)
    manifest = tmp_path / ".build-manifest.json"
    manifest.write_text(json.dumps({"hash": "replacement", "backend_hash": "new", "built_at": 7}))

    frontend = client.get("/api/status").json()["runtime"]["frontend"]

    assert frontend == {"source_hash": "replacement", "expected_backend_hash": "new", "built_at": 7.0}


def test_in_run_with_every_automatic_policy_disabled_is_observing(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace
    import events

    strategy = Strategy.from_config()
    controls = Controls(strategy=replace(
        strategy,
        actions=tuple(replace(action, enabled=False) for action in strategy.actions),
        auto_navigate=False,
        autopilot=replace(strategy.autopilot, enabled=False),
        shopping=replace(strategy.shopping, enabled=False),
    ))
    state = BotState()
    state.apply(events.ScreenChanged(prev="UNKNOWN", curr="IN_RUN", confidence=1, scores={}))
    client = _client(monkeypatch, tmp_path, runner=Runner(), controls=controls)
    # Replace the helper's default state with the explicit IN_RUN fixture.
    app = web_app.create_app(
        state=state, sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, runner=Runner(), controls=controls,
    )

    readiness = TestClient(app).get("/api/status").json()["runtime"]["readiness"]

    assert readiness == {
        "mode": "observing",
        "reasons": ["all automation policies are disabled for IN_RUN"],
    }


def test_game_over_with_navigation_enabled_is_automation_enabled(monkeypatch, tmp_path: Path) -> None:
    from dataclasses import replace
    import events

    controls = Controls(strategy=replace(Strategy.from_config(), auto_navigate=True))
    state = BotState()
    state.apply(events.ScreenChanged(prev="IN_RUN", curr="GAME_OVER", confidence=1, scores={}))
    _client(monkeypatch, tmp_path, runner=Runner(), controls=controls)
    app = web_app.create_app(
        state=state, sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, runner=Runner(), controls=controls,
    )

    readiness = TestClient(app).get("/api/status").json()["runtime"]["readiness"]

    assert readiness == {"mode": "automation_enabled", "reasons": []}


@pytest.mark.parametrize(
    "shopping_active, expected_mode",
    [(True, "automation_enabled"), (False, "observing")],
)
def test_unknown_screen_reflects_an_active_shopping_visit(
    monkeypatch, tmp_path: Path, shopping_active: bool, expected_mode: str
) -> None:
    from dataclasses import replace

    controls = Controls(strategy=replace(
        Strategy.from_config(),
        shopping=replace(Strategy.from_config().shopping, enabled=True),
    ))
    shopping = type("Shopping", (), {"active": shopping_active, "disabled_reason": None})()
    _client(monkeypatch, tmp_path, runner=Runner(), controls=controls)
    app = web_app.create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, runner=Runner(), controls=controls,
        shopping=shopping,
    )

    readiness = TestClient(app).get("/api/status").json()["runtime"]["readiness"]

    assert readiness["mode"] == expected_mode


@pytest.mark.parametrize("headers", [
    {"X-Tower-Backend-Hash": "old"},
    {"X-Tower-Api-Version": "99"},
    {"X-Tower-Ui-Hash": "old-ui"},
])
def test_mismatched_browser_headers_reject_command_before_it_is_queued(
    monkeypatch, tmp_path: Path, headers: dict[str, str]
) -> None:
    controls = Controls(strategy=Strategy.from_config())
    client = _client(monkeypatch, tmp_path, runner=None, controls=controls)

    response = client.post(
        "/api/control/command",
        json={"command": "speed_up"},
        headers=headers,
    )

    assert response.status_code == 409
    assert controls.drain() == ()


def test_unheadered_legacy_client_remains_supported(monkeypatch, tmp_path: Path) -> None:
    controls = Controls(strategy=Strategy.from_config())
    client = _client(monkeypatch, tmp_path, runner=None, controls=controls)

    assert client.post("/api/control/command", json={"command": "speed_up"}).status_code == 200
    assert controls.drain() == ("speed_up",)


def test_emergency_pause_is_allowed_with_stale_headers(monkeypatch, tmp_path: Path) -> None:
    controls = Controls(strategy=Strategy.from_config())
    client = _client(monkeypatch, tmp_path, runner=None, controls=controls)

    response = client.patch(
        "/api/control", json={"paused": True},
        headers={"X-Tower-Backend-Hash": "old", "X-Tower-Api-Version": "99"},
    )

    assert response.status_code == 200
    assert controls.snapshot().paused is True


def test_numeric_pause_value_does_not_bypass_compatibility_guard(monkeypatch, tmp_path: Path) -> None:
    controls = Controls(strategy=Strategy.from_config())
    client = _client(monkeypatch, tmp_path, runner=None, controls=controls)

    response = client.patch(
        "/api/control", json={"paused": 1},
        headers={"X-Tower-Backend-Hash": "old"},
    )

    assert response.status_code == 409
    assert controls.snapshot().paused is False
