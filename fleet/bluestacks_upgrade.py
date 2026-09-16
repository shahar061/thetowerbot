"""Close only the measured BlueStacks Air *host* update notice.

This is separate from Android/ADB automation. A session, cloud, account or
credential prompt can never authorize a click here.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

import ocr
from ocr import TextBox


class UpgradeWindow(Protocol):
    title: str
    instance: str

    def capture(self) -> np.ndarray: ...
    def press(self, point: tuple[int, int]) -> None: ...
    def present(self) -> bool: ...


_BLOCKED_TEXT = (
    "new session", "cloud session", "cloud save", "change account",
    "password", "credential", "logged in elsewhere", "another device",
)


def _one(boxes: Sequence[TextBox], text: str) -> TextBox | None:
    matches = [box for box in boxes if box.text.casefold().strip() == text.casefold()
               and box.confidence >= .9]
    return matches[0] if len(matches) == 1 else None


def _visible_x(image: np.ndarray, point: tuple[int, int], scale: float) -> bool:
    """Require both bright diagonal strokes and the quiet corner pixels."""
    x, y = point
    radius = round(14 * scale)
    if radius < 8 or x - radius < 0 or y - radius < 0:
        return False
    if x + radius >= image.shape[1] or y + radius >= image.shape[0]:
        return False
    patch = image[y - radius:y + radius + 1, x - radius:x + radius + 1]
    size = max(25, round(29 * scale))
    patch = cv2.resize(patch, (size, size), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    bright = gray >= 170
    center = size // 2
    steps = range(-round(size * .34), round(size * .34) + 1, 2)
    supported = 0
    total = 0
    for offset in steps:
        for diagonal_y in (offset, -offset):
            total += 1
            sx, sy = center + offset, center + diagonal_y
            supported += bool(bright[sy - 1:sy + 2, sx - 1:sx + 2].any())
    quiet = (bright[2:5, center - 1:center + 2],
             bright[-5:-2, center - 1:center + 2],
             bright[center - 1:center + 2, 2:5],
             bright[center - 1:center + 2, -5:-2])
    return supported >= total * .9 and all(not area.any() for area in quiet)


def locate_upgrade_close(image: np.ndarray, boxes: Sequence[TextBox]) -> tuple[int, int] | None:
    """Return the X center only for the exact measured update dialog."""
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        return None
    if any(term in box.text.casefold() for box in boxes for term in _BLOCKED_TEXT):
        return None
    title = _one(boxes, "Upgrade available")
    body = _one(boxes, "A new version of BlueStacks Air is")
    continuation = _one(boxes, "available.")
    learn = next((box for box in boxes if box.text.casefold().replace(" ", "") == "learnmore"
                  and box.confidence >= .9), None)
    update = _one(boxes, "Update")
    if any(box is None for box in (title, body, continuation, learn, update)):
        return None
    assert title and body and continuation and learn and update
    scale = title.rect.h / 36.
    if not .75 <= scale <= 2.5:
        return None
    if not (title.rect.y < body.rect.y < continuation.rect.y
            < learn.rect.y < update.rect.y
            and title.rect.x < body.rect.x < update.rect.x
            and abs((update.rect.x + update.rect.w) -
                    (title.rect.x + 625 * scale)) <= 40 * scale):
        return None
    point = (round(update.rect.x + update.rect.w + 14 * scale),
             round(title.rect.y + title.rect.h / 2 - scale))
    return point if _visible_x(image, point, scale) else None


def close_upgrade_popup(
    window: UpgradeWindow, *, expected_instance: str,
    read_text: Callable[[np.ndarray], Sequence[TextBox]] = ocr.read,
) -> str:
    """Press one host X, then verify the exact notice disappeared.

    Returns ``absent``, ``closed``, ``unconfirmed`` or ``unavailable``. Never
    presses any other control, including the Update button.
    """
    if window.title != "Upgrade available" or window.instance != expected_instance:
        return "unavailable"
    if not window.present():
        return "absent"
    before = window.capture()
    point = locate_upgrade_close(before, read_text(before))
    if point is None:
        return "unconfirmed"
    window.press(point)
    return "unconfirmed" if window.present() else "closed"


class HostPopupUnavailable(RuntimeError):
    """The exact host window or macOS control could not be verified."""


class MacOSUpgradeWindow:
    """Capture and press only one named BlueStacks Air host window."""

    title = "Upgrade available"
    _HELPER = Path(__file__).with_name("macos_bluestacks_window.swift")
    _EXECUTABLE = "/Applications/BlueStacks.app/Contents/MacOS/BlueStacks"

    def __init__(self, instance: str) -> None:
        if sys.platform != "darwin" or not re.fullmatch(r"[A-Za-z0-9_]+", instance):
            raise HostPopupUnavailable("unsupported host or instance name")
        self.instance = instance
        rows = subprocess.run(["ps", "-axo", "pid=,command="], check=True,
                              text=True, capture_output=True, timeout=5).stdout.splitlines()
        matches = []
        for row in rows:
            parts = row.strip().split(maxsplit=1)
            if (len(parts) == 2 and parts[0].isdigit()
                    and parts[1] == f"{self._EXECUTABLE} --instance {instance}"):
                matches.append(int(parts[0]))
        if len(matches) != 1:
            raise HostPopupUnavailable("named BlueStacks process missing or ambiguous")
        self.pid = matches[0]
        self._window: dict[str, float | int | str] | None = None

    def _helper(self, *args: str) -> str:
        result = subprocess.run(["swift", str(self._HELPER), *args],
                                check=True, text=True, capture_output=True, timeout=20)
        return result.stdout.strip()

    def _inspect(self) -> dict[str, float | int | str]:
        try:
            row = json.loads(self._helper("inspect", str(self.pid)))
            if (row.get("title") != self.title or not isinstance(row.get("id"), int)
                    or any(not isinstance(row.get(key), (int, float)) or row[key] <= 0
                           for key in ("width", "height"))):
                raise ValueError("window metadata incomplete")
            return row
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise HostPopupUnavailable("exact BlueStacks upgrade window unavailable") from exc

    def present(self) -> bool:
        try:
            count = int(self._helper("count", str(self.pid)))
            if count > 1:
                raise HostPopupUnavailable("multiple upgrade windows are ambiguous")
            return count == 1
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise HostPopupUnavailable("upgrade window presence unavailable") from exc

    def capture(self) -> np.ndarray:
        self._window = self._inspect()
        with tempfile.TemporaryDirectory(prefix="thetowerbot-upgrade-") as directory:
            path = Path(directory) / "window.png"
            try:
                subprocess.run(["screencapture", "-x", "-o", "-l",
                                str(self._window["id"]), str(path)],
                               check=True, capture_output=True, timeout=10)
                image = cv2.imread(str(path))
            except (subprocess.SubprocessError, OSError) as exc:
                raise HostPopupUnavailable("BlueStacks window capture unavailable") from exc
        if image is None or image.ndim != 3:
            raise HostPopupUnavailable("BlueStacks window capture unreadable")
        return image

    def press(self, point: tuple[int, int]) -> None:
        # Re-observe immediately before the host action. If the dialog moved,
        # disappeared or changed, no Accessibility press is issued.
        current = self.capture()
        fresh = locate_upgrade_close(current, ocr.read(current, strict=True, min_confidence=0.))
        if fresh is None or abs(fresh[0] - point[0]) > 2 or abs(fresh[1] - point[1]) > 2:
            raise HostPopupUnavailable("upgrade dialog changed before close")
        assert self._window is not None
        scale_x = current.shape[1] / float(self._window["width"])
        scale_y = current.shape[0] / float(self._window["height"])
        if (not 1. <= scale_x <= 3. or not 1. <= scale_y <= 3.
                or abs(scale_x - scale_y) > .15):
            raise HostPopupUnavailable("window capture scale is ambiguous")
        x = float(self._window["x"]) + point[0] / scale_x
        y = float(self._window["y"]) + point[1] / scale_y
        try:
            self._helper("press", str(self.pid), str(self._window["id"]), str(x), str(y))
        except subprocess.CalledProcessError as exc:
            if "Accessibility permission required" in (exc.stderr or ""):
                raise HostPopupUnavailable("macOS Accessibility permission required") from exc
            raise HostPopupUnavailable("exact close button could not be pressed") from exc
        except (subprocess.SubprocessError, OSError) as exc:
            raise HostPopupUnavailable("exact close button could not be pressed") from exc


def close_upgrade_for_instance(instance: str) -> str:
    """One guarded host action for a named, already running BlueStacks instance."""
    return close_upgrade_popup(MacOSUpgradeWindow(instance), expected_instance=instance)


_HOST_CONFIG = Path("/Users/Shared/Library/Application Support/BlueStacks/bluestacks.conf")
_ADB_PORT = re.compile(r'^bst\.instance\.([A-Za-z0-9_]+)\.adb_port="(\d+)"$')


def instance_for_endpoint(host: str, port: int, *, config_path: Path = _HOST_CONFIG) -> str | None:
    """Resolve only one local, exact BlueStacks instance for standalone ADB."""
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
        return None
    try:
        matches = [match.group(1) for line in config_path.read_text(encoding="utf-8").splitlines()
                   if (match := _ADB_PORT.fullmatch(line.strip())) is not None
                   and int(match.group(2)) == port]
    except OSError:
        return None
    return matches[0] if len(matches) == 1 else None


def close_upgrade_for_endpoint(host: str, port: int) -> str:
    """Guarded close for the uniquely mapped standalone local ADB endpoint."""
    instance = instance_for_endpoint(host, port)
    return close_upgrade_for_instance(instance) if instance is not None else "unavailable"


def main(argv: list[str] | None = None) -> int:
    """One-shot host-only close; no game or account automation is started."""
    import argparse

    parser = argparse.ArgumentParser(description="Close the measured BlueStacks Air update notice")
    parser.add_argument("--instance", required=True, help="exact BlueStacks instance name")
    args = parser.parse_args(argv)
    try:
        result = close_upgrade_for_instance(args.instance)
    except (HostPopupUnavailable, subprocess.SubprocessError, OSError, RuntimeError) as exc:
        print(f"unavailable: {exc}")
        return 1
    print(result)
    return 0 if result in {"closed", "absent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
