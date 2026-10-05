"""ADB device access: connect, capture, tap.

Everything here goes through ADB, so the emulator window never needs focus.
Verified: with the emulator app fully hidden (not merely unfocused),
screencap returns live frames and input tap registers.
"""

from __future__ import annotations

import logging
from typing import Any

import cv2
import numpy as np
from adbutils import AdbClient, AdbDevice, AdbError
from numpy.typing import NDArray

import config

logger = logging.getLogger("tower_bot.device")

Image = NDArray[np.uint8]


class EmulatorError(RuntimeError):
    """Raised when the emulator cannot be reached or does not respond."""


class IdentityError(EmulatorError):
    """The requested endpoint did not resolve to exactly one transport."""


def endpoint_matches(endpoint: str, serial: str | None) -> bool:
    """Accept only this endpoint or its deterministic local emulator alias."""
    aliases = {endpoint}
    host, separator, port_text = endpoint.rpartition(":")
    if separator and host in {"127.0.0.1", "localhost", "::1"} and port_text.isdigit():
        port = int(port_text)
        if port % 2 == 1:
            aliases.add(f"emulator-{port - 1}")
    return serial in aliases


def connect_device(
    host: str = config.DEVICE_HOST,
    port: int = config.DEVICE_PORT,
    adb_host: str = config.ADB_HOST,
    adb_port: int = config.ADB_PORT,
) -> AdbDevice:
    """Return a connected adbutils device for the local emulator.

    Accepts only the requested endpoint or its deterministic local emulator
    transport alias. A serial is never an account identity.
    """
    client = AdbClient(host=adb_host, port=adb_port)
    try:
        client.server_version()  # cheap round-trip that proves the server is up
    except Exception as exc:  # noqa: BLE001 - surface any socket/protocol error
        raise EmulatorError(
            f"No ADB server on {adb_host}:{adb_port}. Run `adb start-server` first."
        ) from exc

    serial = f"{host}:{port}"
    try:
        client.connect(serial, timeout=3.0)
    except AdbError:
        logger.debug("connect(%s) failed; checking attached transports", serial)

    # adbutils builds an AdbDevice for any serial without checking that it
    # exists, so confirm against the attached list before trusting it.
    attached = client.device_list()
    if not attached:
        message = f"identity incident: missing ADB endpoint {serial}; no devices attached"
        logger.error("%s", message)
        raise IdentityError(message)

    matches = [d for d in attached if endpoint_matches(serial, d.serial)]
    if len(matches) != 1:
        reason = "missing" if not matches else "duplicate"
        message = f"identity incident: {reason} ADB endpoint {serial}"
        logger.error("%s; attached=%s", message, [d.serial for d in attached])
        raise IdentityError(message)
    device = matches[0]

    logger.info("Connected to %s", device.serial)
    return device


def capture_screen(device: AdbDevice) -> Image:
    """Grab the current frame straight into memory as a BGR OpenCV image."""
    # error_ok=False matters: the default returns a *black* image when the
    # capture fails, which would leave the bot scanning blank frames forever.
    try:
        shot = device.screenshot(error_ok=False)
        # adbutils hands back a PIL image in RGB; OpenCV wants BGR. A
        # malformed image is just as unusable as a failed transport read.
        return cv2.cvtColor(np.asarray(shot.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception as exc:  # noqa: BLE001 - transports and image decoders vary
        raise EmulatorError(f"screencap failed: {exc}") from exc


def tap(device: AdbDevice, x: int, y: int) -> None:
    """Send an invisible tap. The emulator does not need focus."""
    device.click(x, y)


def burst_command(x: int, y: int, n: int, gap_s: float = 0.0) -> str:
    """One `adb shell` line tapping (x, y) `n` times, `gap_s` seconds apart."""
    if type(n) is not int or n < 1:
        raise ValueError("a tap burst needs at least one tap")
    if gap_s < 0:
        raise ValueError("a tap gap may not be negative")
    line = f"input tap {int(x)} {int(y)}"
    separator = f"; sleep {gap_s:g}; " if gap_s > 0 else "; "
    return separator.join([line] * n)


def tap_burst(device: Any, x: int, y: int, n: int, gap_s: float = 0.0) -> None:
    """Send `n` taps as one input.

    A supervised device brings its own `tap_burst`, which checkpoints the
    whole burst as a single action; a raw adbutils device gets one shell
    command.
    """
    burst = getattr(device, "tap_burst", None)
    if callable(burst):
        burst(x, y, n, gap_s)
    else:
        device.shell(burst_command(x, y, n, gap_s))
