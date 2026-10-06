from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest
from PIL import Image as PILImage

import device
from fleet.runtime import WorkerRuntime, reserve_endpoint, RuntimeIsolationError


@pytest.mark.parametrize("serials", [
    ["emulator-5556", "127.0.0.1:5557"],
    ["127.0.0.1:5555", "127.0.0.1:5555"],
])
def test_connect_refuses_missing_or_duplicate_requested_endpoint(monkeypatch, serials) -> None:
    client = MagicMock()
    client.device_list.return_value = [type("Attached", (), {"serial": serial})() for serial in serials]
    monkeypatch.setattr(device, "AdbClient", lambda **_: client)

    with pytest.raises(device.EmulatorError, match="identity incident"):
        device.connect_device(host="127.0.0.1", port=5555)


def test_capture_screen_converts_rgb_to_bgr() -> None:
    """adbutils returns PIL RGB; OpenCV needs BGR. A red pixel proves the swap."""
    fake = MagicMock()
    fake.screenshot.return_value = PILImage.new("RGB", (4, 4), (255, 0, 0))

    frame = device.capture_screen(fake)

    assert frame.shape == (4, 4, 3)
    # BGR: blue=0, green=0, red=255
    assert tuple(frame[0, 0]) == (0, 0, 255)


def test_capture_screen_demands_errors_not_black_frames() -> None:
    """error_ok=False matters: the default returns a black image on failure,
    which would leave the bot scanning blank frames forever."""
    fake = MagicMock()
    fake.screenshot.return_value = PILImage.new("RGB", (2, 2), (0, 0, 0))

    device.capture_screen(fake)

    fake.screenshot.assert_called_once_with(error_ok=False)


def test_transient_screencap_failure_is_recoverable_device_error() -> None:
    fake = MagicMock()
    fake.screenshot.side_effect = OSError("transport reset")

    with pytest.raises(device.EmulatorError, match="screencap failed"):
        device.capture_screen(fake)


def test_tap_delegates_to_the_device() -> None:
    fake = MagicMock()
    device.tap(fake, 100, 200)
    fake.click.assert_called_once_with(100, 200)


def test_standalone_device_lease_conflicts_with_fleet_lease(tmp_path) -> None:
    worker = WorkerRuntime.for_worker(tmp_path, "worker-a", 8765)
    with reserve_endpoint("127.0.0.1:5555"):
        with pytest.raises(RuntimeIsolationError, match="ADB endpoint"):
            with worker.reserve("127.0.0.1:5555"):
                pass


def test_standalone_entry_point_refuses_an_already_leased_device(monkeypatch) -> None:
    import tower_bot

    monkeypatch.setattr(tower_bot, "_main", lambda *_: 0)
    with reserve_endpoint("127.0.0.1:5555"):
        assert tower_bot.main(["--host", "127.0.0.1", "--port", "5555", "--once"]) == 1


def test_burst_command_is_one_shell_line() -> None:
    assert device.burst_command(10, 20, 3, 0) == "input tap 10 20; input tap 10 20; input tap 10 20"
    assert device.burst_command(10, 20, 2, 0.05) == "input tap 10 20; sleep 0.05; input tap 10 20"
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            device.burst_command(10, 20, bad, 0)
    with pytest.raises(ValueError):
        device.burst_command(10, 20, 2, -0.1)


def test_tap_burst_sends_one_shell_command_to_a_raw_device() -> None:
    fake = MagicMock(spec=["shell"])
    device.tap_burst(fake, 10, 20, 3, 0)
    fake.shell.assert_called_once_with("input tap 10 20; input tap 10 20; input tap 10 20")


def test_tap_burst_defers_to_a_supervised_device() -> None:
    fake = MagicMock(spec=["shell", "tap_burst"])
    device.tap_burst(fake, 10, 20, 3, 0.05)
    fake.tap_burst.assert_called_once_with(10, 20, 3, 0.05)
    fake.shell.assert_not_called()
