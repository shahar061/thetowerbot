"""Start, stop, and shut down - three verbs where there was one.

The old POST /api/control/stop meant both "stop the bot" and "shut the
process down". Once the dashboard outlives the bot those are different
things, and one route cannot mean both.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import config
from autopilot import AutopilotState
from control import Controls
from events import EventBus
from runner import RunnerError
from sinks.sse import SseSink
from sinks.state import BotState
from strategy import ActionRule, Strategy
from web.app import create_app


class FakeRunner:
    """Records what the routes asked for, without any threads."""

    def __init__(self) -> None:
        self.autopilot_state = AutopilotState()
        self.running = False
        self.starts = 0
        self.stops = 0
        self.fail_with: RunnerError | None = None

    def start(self) -> dict:
        self.starts += 1
        if self.fail_with is not None:
            raise self.fail_with
        self.running = True
        return {"running": True, "since": 1.0, "error": None}

    def stop(self) -> dict:
        self.stops += 1
        self.running = False
        return {"running": False, "since": None, "error": None}

    def status(self) -> dict:
        return {"running": self.running, "since": 1.0 if self.running else None,
                "error": None}


@pytest.fixture
def wired():
    runner = FakeRunner()
    shutdown = threading.Event()
    controls = Controls(strategy=Strategy(
        name="test",
        actions=(ActionRule(name="Damage", template="upgrade_damage.png"),),
    ))
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, shutdown=shutdown,
        controls=controls, checks={"brightness": object(), "digits": None},
        runner=runner,
    )
    return TestClient(app), runner, shutdown


def test_start_starts_the_bot_and_not_the_shutdown(wired) -> None:
    client, runner, shutdown = wired
    body = client.post("/api/bot/start")
    assert body.status_code == 200
    assert body.json()["running"] is True
    assert runner.starts == 1
    assert not shutdown.is_set()


def test_starting_a_running_bot_is_a_409(wired) -> None:
    client, runner, _ = wired
    runner.fail_with = RunnerError("already running", 409)
    response = client.post("/api/bot/start")
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_a_dead_emulator_is_a_503_not_a_500(wired) -> None:
    client, runner, _ = wired
    runner.fail_with = RunnerError("no emulator at 127.0.0.1:5555", 503)
    response = client.post("/api/bot/start")
    assert response.status_code == 503
    assert "emulator" in response.json()["detail"]


def test_stopping_the_bot_leaves_the_server_up(wired) -> None:
    """The whole point of the split. Stop must not kill the dashboard you
    pressed it from."""
    client, runner, shutdown = wired
    client.post("/api/bot/start")
    response = client.post("/api/bot/stop")
    assert response.status_code == 200
    assert response.json()["running"] is False
    assert runner.stops == 1
    assert not shutdown.is_set()
    # Still serving.
    assert client.get("/api/status").status_code == 200


def test_shutdown_sets_the_process_flag(wired) -> None:
    client, _, shutdown = wired
    assert client.post("/api/shutdown").json() == {"stopping": True}
    assert shutdown.is_set()


def test_status_reports_the_bot_block(wired) -> None:
    client, _, _ = wired
    assert client.get("/api/status").json()["bot"]["running"] is False
    client.post("/api/bot/start")
    assert client.get("/api/status").json()["bot"]["running"] is True


def test_status_has_a_bot_block_even_with_no_runner() -> None:
    """--once, --tui and every test predating this pass no runner. The key
    must still be there, so the browser needs no special case."""
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR,
    )
    body = TestClient(app).get("/api/status").json()
    assert body["bot"] == {"running": False, "since": None, "error": None}


def test_the_lifecycle_routes_are_absent_without_a_runner() -> None:
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR,
    )
    client = TestClient(app)
    # 404, not a 405 or a 500: the route never existed. StaticFiles itself
    # would answer a stray POST with 405 ("wrong method"), which is why
    # web/app.py registers its own catch-all 404 for unmatched /api writes
    # ahead of the mount - see the test right below this one.
    assert client.post("/api/bot/start").status_code == 404


def test_the_unmatched_api_catch_all_does_not_shadow_real_routes() -> None:
    """web/app.py's catch-all ("/api/{_path:path}", write verbs -> 404),
    added so a truly-unregistered route like /api/bot/start reads as 404
    rather than the static mount's 405, has to stay the LAST /api route
    registered - Starlette matches in order, and that pattern swallows
    every write-verb request under /api, real or not.

    If a future route (the strategy CRUD routes are next, and three of
    those five are write verbs) is ever registered *after* the catch-all
    instead of before it, this test fails: /api/bot/stop would 404 even
    though a runner is wired and the route very much exists. That failure
    mode is worse than a normal regression because it looks exactly like
    "the route was never added" rather than "the route is shadowed" -
    hence pinning the ordering here instead of trusting the comment above
    the catch-all alone.
    """
    runner = FakeRunner()
    app = create_app(
        state=BotState(), sse=SseSink(), bus=EventBus(), db_path=None,
        unknown_dir=config.UNKNOWN_DIR, runner=runner,
    )
    client = TestClient(app)
    response = client.post("/api/bot/stop")
    assert response.status_code == 200
    assert runner.stops == 1


def test_the_ledger_route_is_not_shadowed_by_the_api_catch_all(wired) -> None:
    client = wired[0]

    assert client.get("/api/ledger").status_code == 200


def test_the_director_route_is_not_shadowed_by_the_api_catch_all(wired) -> None:
    """The catch-all above only matches POST/PUT/PATCH/DELETE, so a GET was
    never actually at risk from it - the real shadowing hazard for a GET is
    the SPA mount ("/", StaticFiles(..., html=True)) registered after every
    /api route. This exercises the real risk (GET, not a write verb) rather
    than the one the catch-all's own comment names, so a regression that
    moved /api/director below the StaticFiles mount would fail here."""
    client = wired[0]

    assert client.get("/api/director").status_code == 200
