"""Recorded BlueStacks host dialog; no Android account or session controls."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import shutil

import cv2
import pytest

import ocr
from fleet.bluestacks_upgrade import (MacOSUpgradeWindow, close_upgrade_popup,
                                     instance_for_endpoint, locate_upgrade_close)
from ocr import TextBox


FIXTURE = Path(__file__).parent / "fixtures" / "bluestacks" / "upgrade_dialog.png"


@pytest.fixture(scope="module")
def shot():
    image = cv2.imread(str(FIXTURE))
    assert image is not None
    return image, ocr.read(image, strict=True, min_confidence=0.)


def test_recorded_dialog_locates_only_the_close_x(shot) -> None:
    image, boxes = shot
    point = locate_upgrade_close(image, boxes)
    assert point is not None
    assert abs(point[0] - 708) <= 2 and abs(point[1] - 88) <= 2
    assert not (585 <= point[0] <= 694 and 344 <= point[1] <= 375)


@pytest.mark.parametrize("excluded", ["Upgrade available", "A new version of BlueStacks Air is",
                                      "Update", "Learnmore"])
def test_missing_corrobating_text_never_locates_close(shot, excluded: str) -> None:
    image, boxes = shot
    altered = tuple(box for box in boxes if box.text != excluded)
    assert locate_upgrade_close(image, altered) is None


def test_conflict_or_account_wording_blocks_host_click(shot) -> None:
    image, boxes = shot
    for wording in ("New session detected", "Cloud session different than local session",
                    "Change Account", "Force Cloud Save", "Enter password"):
        altered = boxes + (replace(boxes[0], text=wording),)
        assert locate_upgrade_close(image, altered) is None


def test_ambiguous_title_or_missing_x_blocks_host_click(shot) -> None:
    image, boxes = shot
    title = next(box for box in boxes if box.text == "Upgrade available")
    assert locate_upgrade_close(image, boxes + (title,)) is None
    erased = image.copy()
    erased[74:103, 694:723] = (64, 64, 64)
    assert locate_upgrade_close(erased, boxes) is None


class Window:
    def __init__(self, image, boxes, *, title: str = "Upgrade available",
                 instance: str = "Tiramisu64") -> None:
        self.image = image
        self.boxes = boxes
        self.title = title
        self.instance = instance
        self.presses: list[tuple[int, int]] = []
        self.open = True

    def capture(self):
        return self.image

    def press(self, point: tuple[int, int]) -> None:
        self.presses.append(point)
        self.open = False

    def present(self) -> bool:
        return self.open


def test_close_action_is_bound_to_named_window_and_verified(shot) -> None:
    image, boxes = shot
    window = Window(image, boxes)
    assert close_upgrade_popup(window, expected_instance="Tiramisu64",
                               read_text=lambda frame: ocr.read(frame, strict=True,
                                                                 min_confidence=0.)) == "closed"
    assert len(window.presses) == 1
    assert window.presses[0] == locate_upgrade_close(image, boxes)


def test_absent_popup_does_not_capture_or_press(shot) -> None:
    image, boxes = shot
    window = Window(image, boxes)
    window.open = False
    assert close_upgrade_popup(window, expected_instance="Tiramisu64",
                               read_text=lambda _: boxes) == "absent"
    assert window.presses == []


@pytest.mark.parametrize("title,instance", [("Other App", "Tiramisu64"),
                                            ("Upgrade available", "other")])
def test_wrong_host_window_never_pressed(shot, title: str, instance: str) -> None:
    image, boxes = shot
    window = Window(image, boxes, title=title, instance=instance)
    assert close_upgrade_popup(window, expected_instance="Tiramisu64",
                               read_text=lambda _: boxes) == "unavailable"
    assert window.presses == []


def test_original_instance_is_resolved_only_from_unique_exact_adb_port(tmp_path: Path) -> None:
    config = tmp_path / "bluestacks.conf"
    config.write_text('bst.instance.Tiramisu64.adb_port="5555"\n'
                      'bst.instance.Tiramisu64_2.adb_port="5575"\n'
                      'bst.instance.Tiramisu64.status.adb_port="5555"\n')
    assert instance_for_endpoint("127.0.0.1", 5555, config_path=config) == "Tiramisu64"
    assert instance_for_endpoint("localhost", 5575, config_path=config) == "Tiramisu64_2"
    assert instance_for_endpoint("remote", 5555, config_path=config) is None
    config.write_text(config.read_text() + 'bst.instance.other.adb_port="5555"\n')
    assert instance_for_endpoint("127.0.0.1", 5555, config_path=config) is None


def test_macos_bridge_targets_exact_process_window_and_retina_scaled_x(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import fleet.bluestacks_upgrade as upgrade

    calls: list[list[str]] = []
    window = {"id": 42, "title": "Upgrade available", "x": 558., "y": 361.,
              "width": 396., "height": 229.}
    def run(argv: list[str], **_: object):
        calls.append(argv)
        if argv[0] == "ps":
            return SimpleNamespace(stdout=(
                "123 /Applications/BlueStacks.app/Contents/MacOS/BlueStacks --instance Tiramisu64\n"
                "124 /Applications/BlueStacks.app/Contents/MacOS/BlueStacks --instance Tiramisu64_2\n"))
        if argv[0] == "swift":
            return SimpleNamespace(stdout=json.dumps(window) if argv[2] == "inspect" else "pressed")
        assert argv[0] == "screencapture" and argv[4] == "42"
        shutil.copyfile(FIXTURE, argv[5])
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(upgrade.sys, "platform", "darwin")
    monkeypatch.setattr(upgrade.subprocess, "run", run)
    host = MacOSUpgradeWindow("Tiramisu64")
    host.press((708, 88))
    pressed = next(argv for argv in calls if argv[0] == "swift" and argv[2] == "press")
    assert pressed[3:5] == ["123", "42"]
    assert float(pressed[5]) == pytest.approx(912.)
    assert float(pressed[6]) == pytest.approx(405.)
