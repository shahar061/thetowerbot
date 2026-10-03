"""Recorded native-repeat states bind each tap target to a visible running lab."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import cv2
import pytest

from config import Rect
from device import Image
import lab_screen
import ocr


FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[Image, tuple[ocr.TextBox, ...]]:
    screen = cv2.imread(str(FIXTURES / f"{name}.png"))
    assert screen is not None
    rows = json.loads((FIXTURES / "ocr" / f"{name}.json").read_text())
    boxes = tuple(ocr.TextBox(row["text"], row["confidence"], Rect(*row["rect"])) for row in rows)
    return screen, boxes


@pytest.mark.parametrize(("name", "states"), [
    ("menu_labs_native_repeat_on", {1: "enabled"}),
    ("menu_labs_native_repeat_off", {1: "disabled"}),
    ("menu_labs_active", {1: "enabled", 5: "enabled"}),
    ("menu_labs_game_speed_running", {1: "disabled"}),
])
def test_recorded_repeat_state_and_target_belong_to_running_lab(name: str, states: dict[int, str]) -> None:
    screen, boxes = recorded(name)
    controls = lab_screen.read_repeat_controls(screen, boxes)
    assert {item.slot: item.state for item in controls} == states
    reading = lab_screen.read_slots(screen, boxes, observed_at=1000.)
    assert {job.slot: job.native_repeat for job in reading.jobs if job.status == "researching"} == states
    for item in controls:
        job = next(job for job in reading.jobs if job.slot == item.slot)
        assert 60 <= item.point[0] <= 90  # Calibrated repeat icon, far left of Rush.
        assert job.rect[1] < item.point[1] < job.rect[1] + job.rect[3]


@pytest.mark.parametrize("name", ["menu_labs_slot1_idle", "menu_labs_game_speed_confirmation",
                                   "menu_labs_game_speed_picker", "menu_labs_intro_popup"])
def test_no_repeat_targets_on_idle_picker_or_modal_frames(name: str) -> None:
    screen, boxes = recorded(name)
    assert lab_screen.read_repeat_controls(screen, boxes) == ()


@pytest.mark.parametrize("factor", [0.3, 0.6])
def test_dimmed_modal_cannot_turn_an_enabled_icon_into_a_disabled_control(factor: float) -> None:
    screen, boxes = recorded("menu_labs_native_repeat_on")
    dimmed = (screen * factor).astype("uint8")
    assert lab_screen.read_repeat_controls(dimmed, boxes) == ()
    assert lab_screen.read_slots(dimmed, boxes, observed_at=1000.).jobs[0].native_repeat == "unknown"


@pytest.mark.parametrize("change", ["blank", "moved", "unsupported"])
def test_no_target_when_calibrated_icon_or_frame_geometry_is_missing(change: str) -> None:
    screen, boxes = recorded("menu_labs_native_repeat_on")
    if change == "unsupported":
        screen = screen[:2200]
    else:
        icon = screen[516:594, 36:114].copy()
        screen[510:600, 25:125] = 0
        if change == "moved":
            screen[516:594, 196:274] = icon
    assert lab_screen.read_repeat_controls(screen, boxes) == ()


@pytest.mark.parametrize("change", ["timer_missing", "name_unknown", "name_zero", "duplicate_slot", "title_untrusted"])
def test_repeat_control_requires_unique_trusted_lab_and_timer_evidence(change: str) -> None:
    screen, boxes = recorded("menu_labs_native_repeat_on")
    if change == "timer_missing":
        boxes = tuple(box for box in boxes if box.text != "5s")
    elif change == "name_unknown":
        boxes = tuple(replace(box, text="Mystery Research Lv.1") if "Lv.1" in box.text else box for box in boxes)
    elif change == "name_zero":
        boxes = tuple(replace(box, text=box.text.replace("Lv.1", "Lv.0")) for box in boxes)
    elif change == "duplicate_slot":
        boxes = (*boxes, next(box for box in boxes if box.text == "Lab 1"))
    else:
        boxes = tuple(replace(box, confidence=0.5) if box.text == "LAB" else box for box in boxes)
    assert lab_screen.read_repeat_controls(screen, boxes) == ()
