"""WS /api/stream: the live video endpoint."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config
import db
from autopilot import AutopilotState
from events import EventBus
from sinks.sse import SseSink
from sinks.state import BotState
from stream.hub import StreamHub
from stream.messages import ConfigMessage, End, FrameMessage, Message
from stream.origin import origin_allowed
from stream.scrcpy_session import StreamError
from web.app import create_app

CONFIG_MSG = ConfigMessage("avc1.42C029", 576, 1280)
KEY = FrameMessage(True, 1000, b"\x00\x00\x00\x01\x65\x88")


class FakeSubscription:
    def __init__(self, messages: list[Message], on_get: Any = None) -> None:
        self.messages = list(messages)
        self.on_get = on_get
        self.gets = 0
        self.closed = threading.Event()

    def get(self, timeout: float) -> Message | None:
        self.gets += 1
        if self.on_get is not None:
            self.on_get(self.gets)
        if self.messages:
            return self.messages.pop(0)
        time.sleep(min(timeout, 0.02))
        return None

    def close(self) -> None:
        self.closed.set()


class FakeHub:
    def __init__(self, messages: list[Message] = (), on_get: Any = None) -> None:
        self.messages = list(messages)
        self.on_get = on_get
        self.subscriptions: list[FakeSubscription] = []

    def subscribe(self) -> FakeSubscription:
        sub = FakeSubscription(self.messages, self.on_get)
        self.subscriptions.append(sub)
        return sub


def client_for(hub: Any, **kwargs: Any) -> TestClient:
    kwargs.setdefault("db_path", None)
    return TestClient(create_app(state=BotState(), sse=SseSink(), bus=EventBus(),
                                 unknown_dir=config.UNKNOWN_DIR, stream_hub=hub, **kwargs))


def close_code(client: TestClient, url: str = "/api/stream", headers: dict[str, str] | None = None) -> int:
    with client.websocket_connect(url, headers=headers or {}) as ws:
        while True:
            message = ws.receive()
            if message["type"] == "websocket.close":
                return int(message["code"])


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


@pytest.mark.parametrize("origin, host, allowed", [
    (None, "127.0.0.1:10059", True),
    ("http://127.0.0.1:8765", "127.0.0.1:10059", True),
    ("http://localhost:3000", "127.0.0.1:10059", True),
    ("http://[::1]:8765", "[::1]:10059", True),
    ("https://mac.tail.ts.net", "mac.tail.ts.net:10059", True),
    ("https://evil.example", "127.0.0.1:10059", False),
    ("https://evil.example", "mac.tail.ts.net:10059", False),
    ("null", "127.0.0.1:10059", False),
    ("https://mac.tail.ts.net", None, False),
    ("http://[::1", "127.0.0.1:10059", False),
    ("http://evil.com", "[::1", False),
])
def test_origin_allowed(origin: str | None, host: str | None, allowed: bool) -> None:
    assert origin_allowed(origin, host) is allowed


def test_streams_the_config_then_binary_frames() -> None:
    hub = FakeHub([CONFIG_MSG, KEY])
    with client_for(hub).websocket_connect("/api/stream") as ws:
        assert json.loads(ws.receive_text()) == {"type": "config", "codec": "avc1.42C029",
                                                 "width": 576, "height": 1280}
        data = ws.receive_bytes()
    assert data[0] == 1
    assert int.from_bytes(data[1:9], "big") == 1000
    assert data[9:] == KEY.data


def test_an_unavailable_stream_closes_with_4503() -> None:
    hub = FakeHub([End.UNAVAILABLE])
    assert close_code(client_for(hub)) == 4503
    wait_until(hub.subscriptions[0].closed.is_set)


def test_a_worker_without_a_hub_closes_with_4503() -> None:
    assert close_code(client_for(None)) == 4503


def test_a_disabled_stream_closes_with_4503_without_subscribing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STREAM_ENABLED", False)
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub)) == 4503
    assert hub.subscriptions == []


def test_shutdown_closes_with_1001() -> None:
    assert close_code(client_for(FakeHub([End.GOING_AWAY]))) == 1001


def test_a_foreign_origin_closes_with_4403() -> None:
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub), headers={"origin": "https://evil.example"}) == 4403
    assert hub.subscriptions == []


def test_the_tailscale_host_is_accepted_via_forwarded_host() -> None:
    hub = FakeHub([CONFIG_MSG])
    headers = {"origin": "https://mac.tail.ts.net", "x-forwarded-host": "mac.tail.ts.net:10059"}
    with client_for(hub).websocket_connect("/api/stream", headers=headers) as ws:
        assert json.loads(ws.receive_text())["type"] == "config"


def test_an_expected_account_that_is_not_here_closes_with_4409() -> None:
    hub = FakeHub([CONFIG_MSG])
    assert close_code(client_for(hub), "/api/stream?expected_account_id=ACCOUNT_A") == 4409
    assert hub.subscriptions == []


def test_a_viewer_leaving_a_still_screen_releases_its_subscription() -> None:
    hub = FakeHub([CONFIG_MSG])  # then nothing: a still screen sends no frames
    with client_for(hub).websocket_connect("/api/stream") as ws:
        ws.receive_text()
        ws.close(1000)
        wait_until(hub.subscriptions[0].closed.is_set)


def test_a_real_hub_only_retries_a_failure_while_someone_is_subscribed() -> None:
    # This is the integration check for the hub's own backoff behaviour
    # (tests/test_stream_hub.py): with a real StreamHub wired into the
    # endpoint, closing the socket on 4503 must not leave the hub retrying
    # scrcpy with nobody watching.
    shutdown = threading.Event()
    starts: list[float] = []

    class FailingSession:
        def start(self) -> None:
            starts.append(time.monotonic())
            raise StreamError("boom")

        def events(self) -> Any:
            return iter(())

        def close(self) -> None:
            pass

    def wait(seconds: float) -> bool:
        time.sleep(0.001)
        return shutdown.is_set()

    hub = StreamHub(lambda: FailingSession(), shutdown=shutdown, wait=wait)
    client = client_for(hub)
    assert close_code(client) == 4503
    wait_until(lambda: hub.subscriber_count == 0)
    count = len(starts)
    time.sleep(0.3)
    assert len(starts) == count  # no further attempts with nobody subscribed
    shutdown.set()


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


def test_an_account_change_mid_stream_closes_with_4409(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "STREAM_ACCOUNT_RECHECK_SECONDS", 0.0)
    root = tmp_path / "fleet"
    path = _worker(root, "Tiramisu64_18", "ACCOUNT_A")
    db.bind_account(path, "ACCOUNT_A")
    runner = type("Runner", (), {"autopilot_state": AutopilotState(),
                                 "status": lambda self: {"running": True},
                                 "verified_account": lambda self: "ACCOUNT_A"})()

    def move_account(gets: int) -> None:
        if gets != 2:
            return
        # The same registration switch the MJPEG revalidation test makes.
        registration = path.parent / "fleet-registration.json"
        record = json.loads(registration.read_text())
        record["account_id"] = "ACCOUNT_B"
        registration.write_text(json.dumps(record))
        binding = Path(record["binding"])
        identity = json.loads(binding.read_text())
        identity["account_id"] = "ACCOUNT_B"
        binding.write_text(json.dumps(identity))

    hub = FakeHub([CONFIG_MSG], on_get=move_account)
    client = client_for(hub, db_path=path, runner=runner, fleet=type("Fleet", (), {"root": root})())
    url = "/api/stream?scope=worker:Tiramisu64_18&expected_account_id=ACCOUNT_A"
    assert close_code(client, url) == 4409
