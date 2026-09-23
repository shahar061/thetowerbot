"""B06 recovery contracts around a real durable checkpoint and fake ADB transport."""

from __future__ import annotations

from pathlib import Path
import json

import numpy as np
import pytest

from supervisor import DeviceSupervisor, GuardedDevice, RecoveryState


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Device:
    def __init__(self, serial: str = "127.0.0.1:5555") -> None:
        self.serial = serial
        self.taps: list[tuple[int, int]] = []
        self.swipes: list[tuple[int, int, int, int, float]] = []

    def click(self, x: int, y: int) -> None:
        self.taps.append((x, y))

    def swipe(self, x: int, y: int, x2: int, y2: int, duration: float) -> None:
        self.swipes.append((x, y, x2, y2, duration))


def test_guarded_swipe_requires_fresh_evidence_and_checkpoints_action(tmp_path: Path) -> None:
    clock = Clock()
    device = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [device])
    sut.recover()
    guarded = GuardedDevice(sut)
    with pytest.raises(RuntimeError):
        guarded.swipe(1, 2, 3, 4, .35)
    assert device.swipes == []
    assert observed(sut, clock, "before") is RecoveryState.READY
    guarded.swipe(1, 2, 3, 4, .35)
    assert device.swipes == [(1, 2, 3, 4, .35)]
    assert sut.status().reason == "action_unconfirmed"
    with pytest.raises(RuntimeError):
        guarded.swipe(1, 2, 3, 4, .35)


def supervisor(path: Path, clock: Clock, devices: list[Device | Exception]) -> DeviceSupervisor:
    def connect() -> Device:
        outcome = devices.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return DeviceSupervisor(
        path=path, endpoint="127.0.0.1:5555", connect=connect,
        expected_account="account-a", clock=clock.time, sleep=clock.sleep,
        max_attempts=3, base_backoff=1.0,
    )


def observed(sut: DeviceSupervisor, clock: Clock, digest: str,
             *, account: str | None = "account-a", screen: str = "MAIN_MENU",
             online_required: bool = False) -> RecoveryState:
    return sut.observe(
        frame_digest=digest, observed_at=clock.time(), screen=screen,
        account_id=account, online_required=online_required,
    )


def test_correct_device_recovers_pending_action_after_process_restart(tmp_path: Path) -> None:
    clock = Clock()
    first = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [first])
    sut.recover()
    assert observed(sut, clock, "before") is RecoveryState.READY
    sut.tap(12, 34)
    assert first.taps == [(12, 34)]

    clock.now += 1
    second = Device()
    restarted = supervisor(tmp_path / "supervisor.json", clock, [second])
    assert restarted.status().state is RecoveryState.BLOCKED
    restarted.recover()
    assert observed(restarted, clock, "before") is RecoveryState.BLOCKED
    assert second.taps == []
    clock.now += 1
    assert observed(restarted, clock, "after") is RecoveryState.READY
    restarted.tap(56, 78)
    assert second.taps == [(56, 78)]


def test_disconnect_retries_with_bounded_backoff_then_recovers(tmp_path: Path) -> None:
    clock = Clock()
    good = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock,
                     [ConnectionError("down"), ConnectionError("down"), good])
    assert sut.recover() is RecoveryState.BLOCKED
    assert clock.slept == [1.0, 2.0]
    assert observed(sut, clock, "fresh") is RecoveryState.READY
    sut.tap(1, 2)
    assert good.taps == [(1, 2)]


def test_retry_exhaustion_is_durable_and_cannot_tap(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "supervisor.json"
    sut = supervisor(path, clock, [ConnectionError("down")] * 3)
    assert sut.recover() is RecoveryState.BLOCKED
    assert clock.slept == [1.0, 2.0]
    assert sut.status().reason == "device_unavailable"
    with pytest.raises(RuntimeError):
        sut.tap(1, 2)
    restarted = supervisor(path, clock, [])
    assert restarted.status().reason == "device_unavailable"
    assert restarted.recover() is RecoveryState.BLOCKED
    assert restarted.status().attempts == 3


def test_restart_preserves_backoff_after_the_first_failed_connect(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "supervisor.json"
    first = supervisor(path, clock, [ConnectionError("down")])
    first.sleep = lambda _: (_ for _ in ()).throw(RuntimeError("process stopped"))
    with pytest.raises(RuntimeError, match="process stopped"):
        first.recover()
    assert first.status().attempts == 1

    device = Device()
    restarted = supervisor(path, clock, [device])
    assert restarted.recover() is RecoveryState.BLOCKED
    assert clock.slept == [1.0]
    assert observed(restarted, clock, "fresh") is RecoveryState.READY


@pytest.mark.parametrize("screen, online_required, account, reason, state", [
    ("UNKNOWN", False, "account-a", "unknown_screen", RecoveryState.BLOCKED),
    ("MAIN_MENU", True, "account-a", "online_required", RecoveryState.BLOCKED),
    ("MAIN_MENU", False, None, "account_unverified", RecoveryState.BLOCKED),
    ("MAIN_MENU", False, "account-b", "wrong_account", RecoveryState.QUARANTINED),
])
def test_ambiguous_online_or_wrong_account_never_taps(
    tmp_path: Path, screen: str, online_required: bool,
    account: str | None, reason: str, state: RecoveryState,
) -> None:
    clock = Clock()
    device = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [device])
    sut.recover()
    assert observed(sut, clock, "frame", account=account, screen=screen,
                    online_required=online_required) is state
    assert sut.status().reason == reason
    with pytest.raises(RuntimeError):
        sut.tap(1, 2)
    assert device.taps == []


def test_stale_frame_after_action_stays_blocked_without_duplicate_tap(tmp_path: Path) -> None:
    clock = Clock()
    device = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [device])
    sut.recover()
    assert observed(sut, clock, "before") is RecoveryState.READY
    sut.tap(1, 2)
    clock.now += 1
    assert observed(sut, clock, "before") is RecoveryState.BLOCKED
    assert sut.status().reason == "stale_frame"
    with pytest.raises(RuntimeError):
        sut.tap(1, 2)
    assert device.taps == [(1, 2)]


def test_wrong_device_quarantines_even_if_frame_and_account_look_right(tmp_path: Path) -> None:
    clock = Clock()
    wrong = Device("127.0.0.1:5557")
    sut = supervisor(tmp_path / "supervisor.json", clock, [wrong])
    assert sut.recover() is RecoveryState.QUARANTINED
    assert observed(sut, clock, "frame") is RecoveryState.QUARANTINED
    with pytest.raises(RuntimeError):
        sut.tap(1, 2)
    assert wrong.taps == []


def test_invalid_retry_checkpoint_is_quarantined_without_connecting(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps({
        "endpoint": "127.0.0.1:5555", "expected_account": "account-a",
        "attempts": -100, "state": "blocked",
    }), encoding="utf-8")
    sut = supervisor(path, clock, [Device()])
    assert sut.recover() is RecoveryState.QUARANTINED
    assert sut.status().reason == "invalid_checkpoint"


def test_reconnect_relaunches_only_the_configured_game_and_holds_actions(tmp_path: Path) -> None:
    clock = Clock()
    device = Device()
    device.app_current = lambda: type("App", (), {"package": "launcher"})()
    launched: list[str] = []
    device.app_start = lambda package: launched.append(package)
    sut = DeviceSupervisor(
        path=tmp_path / "supervisor.json", endpoint="127.0.0.1:5555",
        connect=lambda: device, expected_account="account-a", clock=clock.time,
        sleep=clock.sleep, game_package="com.example.tower",
    )
    assert sut.recover() is RecoveryState.BLOCKED
    assert launched == ["com.example.tower"]
    assert sut.status().reason == "game_relaunched"
    with pytest.raises(RuntimeError):
        sut.tap(1, 2)


def test_disconnect_requires_new_account_evidence_from_the_reconnected_device(tmp_path: Path) -> None:
    clock = Clock()
    first, second = Device(), Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [first, second])
    sut.recover()
    sut.verify_account("account-a", observed_at=clock.time())
    old_evidence_time = clock.time()
    clock.now += 3
    sut.disconnected()
    sut.recover()
    with pytest.raises(RuntimeError):
        sut.verify_account("account-a", observed_at=old_evidence_time)
    assert observed(sut, clock, "new", account=None) is RecoveryState.BLOCKED
    assert second.taps == []


def test_screencap_disconnect_blocks_clicks_until_reconnected(tmp_path: Path) -> None:
    clock = Clock()
    first, second = Device(), Device()
    first.screenshot = lambda **_: (_ for _ in ()).throw(ConnectionError("ADB lost"))
    sut = supervisor(tmp_path / "supervisor.json", clock, [first, second])
    sut.recover()
    guarded = GuardedDevice(sut)

    with pytest.raises(RuntimeError, match="screencap failed"):
        guarded.screenshot(error_ok=False)
    assert sut.status().reason == "device_disconnected"
    with pytest.raises(RuntimeError):
        guarded.click(1, 2)
    assert sut.recover() is RecoveryState.BLOCKED
    assert second.taps == []


def test_named_host_exhaustion_quarantines_only_its_worker(tmp_path: Path) -> None:
    clock = Clock()
    healthy = supervisor(tmp_path / "healthy.json", clock, [Device("127.0.0.1:5555")])
    healthy.recover()
    failed = DeviceSupervisor(
        path=tmp_path / "failed.json", endpoint="127.0.0.1:5557",
        connect=lambda: (_ for _ in ()).throw(ConnectionError("instance down")),
        expected_account="account-b", clock=clock.time, sleep=clock.sleep,
        max_attempts=2, base_backoff=0, quarantine_on_exhaustion=True,
    )
    assert failed.recover() is RecoveryState.QUARANTINED
    assert failed.status().reason == "host_recovery_exhausted"
    assert observed(healthy, clock, "healthy") is RecoveryState.READY
    with pytest.raises(RuntimeError):
        failed.tap(1, 2)


def test_session_conflict_quarantines_without_clicking_either_response(tmp_path: Path) -> None:
    clock = Clock()
    raw = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [raw])
    sut.recover()
    assert sut.observe(frame_digest="conflict", observed_at=clock.time(),
                       screen="MAIN_MENU", account_id="account-a",
                       session_conflict=True) is RecoveryState.QUARANTINED
    assert sut.status().reason == "session_conflict"
    with pytest.raises(RuntimeError):
        sut.tap(10, 10)
    assert raw.taps == []
    assert supervisor(tmp_path / "supervisor.json", clock, []).status().state is RecoveryState.QUARANTINED


@pytest.mark.parametrize("modal_text, reason", [
    ("Online connection required", "online_required"),
    ("Your account is logged in on another device", "session_conflict"),
    ("New session detected", "session_conflict"),
    ("Cloud Session Different Than Local Session", "session_conflict"),
])
def test_bot_holds_a_recovery_modal_before_any_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, modal_text: str, reason: str,
) -> None:
    import events
    import ocr
    import screens
    import tower_bot
    from config import Rect

    clock = Clock()
    import time

    raw = Device()
    sut = supervisor(tmp_path / "supervisor.json", clock, [raw])
    sut.clock = time.time
    sut.recover()
    sut.verify_account("account-a", observed_at=time.time())
    bot = tower_bot.TowerBot(
        device=GuardedDevice(sut), templates=object(), bus=events.EventBus(),
        supervisor=sut,
    )
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    monkeypatch.setattr(bot, "refresh_screen", lambda: setattr(bot, "_screen", frame) or frame)
    monkeypatch.setattr(screens, "classify", lambda *_: screens.ScreenReading(
        screens.ScreenState.MAIN_MENU, 1.0, {},
    ))
    monkeypatch.setattr(ocr, "read", lambda *_, **__: (
        ocr.TextBox(modal_text, 0.99, Rect(0, 0, 8, 8)),
    ))

    assert bot.run_once() is False
    assert sut.status().reason == reason
    assert raw.taps == []


def cooling_supervisor(path: Path, clock: Clock, outcomes: list[Device | Exception]) -> DeviceSupervisor:
    def connect() -> Device:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return DeviceSupervisor(
        path=path, endpoint="127.0.0.1:5555", connect=connect,
        expected_account="account-a", clock=clock.time, sleep=clock.sleep,
        max_attempts=2, base_backoff=0, quarantine_on_exhaustion=True,
        exhaustion_cooldown=60.0,
    )


def test_exhaustion_with_cooldown_retries_after_the_cooldown(tmp_path: Path) -> None:
    clock = Clock()
    device = Device()
    outcomes: list[Device | Exception] = [ConnectionError("adb lost"), ConnectionError("adb lost"), device]
    sut = cooling_supervisor(tmp_path / "supervisor.json", clock, outcomes)
    assert sut.recover() is RecoveryState.BLOCKED
    assert sut.status().reason == "host_recovery_exhausted"
    assert sut.recover() is RecoveryState.BLOCKED
    assert outcomes == [device]
    clock.now += 60.0
    assert sut.recover() is RecoveryState.BLOCKED
    assert sut.device is device
    assert observed(sut, clock, "after") is RecoveryState.READY


def test_cooldown_releases_a_persisted_exhaustion_quarantine(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps({
        "endpoint": "127.0.0.1:5555", "expected_account": "account-a",
        "serial": "127.0.0.1:5555", "state": "quarantined",
        "reason": "host_recovery_exhausted", "attempts": 2,
        "next_retry_at": None, "pending_digest": None, "last_digest": None,
    }))
    device = Device()
    sut = cooling_supervisor(path, clock, [device])
    assert sut.recover() is RecoveryState.BLOCKED
    assert sut.device is device
