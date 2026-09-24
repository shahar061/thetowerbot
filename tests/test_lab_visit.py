"""A lab visit must not call a research tap a purchase without fresh proof."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import cv2

import config
from lab_screen import LabPickerReading, read_picker
from lab_visit import LabVisit
import ocr
import vision


FIXTURES = Path(__file__).parent / "fixtures"


class Device:
    def __init__(self) -> None:
        self.taps: list[tuple[int, int]] = []


def frame(name: str) -> object:
    image = cv2.imread(str(FIXTURES / f"{name}.png"))
    assert image is not None
    return image


def boxes(name: str) -> tuple[ocr.TextBox, ...]:
    entries = json.loads((FIXTURES / "ocr" / f"{name}.json").read_text())
    return tuple(ocr.TextBox(entry["text"], entry["confidence"],
                             config.Rect(*entry["rect"])) for entry in entries)


def setup() -> tuple[LabVisit, Device]:
    return LabVisit(vision.TemplateCache(Path("templates"))), Device()


def affordable(screen: object, text: tuple[ocr.TextBox, ...]) -> LabPickerReading:
    reading = read_picker(screen, text)
    if not reading.page:
        return reading
    assert reading.game_speed is not None
    return replace(reading, coin_balance=400,
                   game_speed=replace(reading.game_speed, status="available"),
                   buy_point=(290, 719))


def test_unaffordable_recorded_row_is_not_tapped_and_visit_returns() -> None:
    visit, device = setup()
    visit.request()
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        assert visit.advance(frame("menu_labs_slot1_idle"), boxes("menu_labs_slot1_idle"), device, 10) is None
        assert len(device.taps) == 1
        assert visit.advance(frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker"), device, 11) is None
        assert len(device.taps) == 1
        assert visit.advance(frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker"), device, 12) is None
        assert len(device.taps) == 2  # picker close only
        assert visit.advance(frame("menu_labs_slot1_idle"), boxes("menu_labs_slot1_idle"), device, 13) is None
        assert len(device.taps) == 3  # Battle tab
        result = visit.advance(frame("menu_main_labs_unlocked"), (), device, 14)
    assert result is not None
    assert result.status == "observed"
    assert result.decision.kind == "wait_coins"
    assert result.observed_coin_spend == 0


def test_purchase_requires_two_matching_affordable_frames_and_coin_delta() -> None:
    visit = LabVisit(vision.TemplateCache(Path("templates")))
    device = Device()
    visit.request()
    now = 10
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        visit.advance(frame("menu_labs_slot1_affordable"), boxes("menu_labs_slot1_affordable"), device, now)
        assert len(device.taps) == 1
        visit.advance(frame("menu_labs_game_speed_affordable"), boxes("menu_labs_game_speed_affordable"), device, now + 1)
        assert len(device.taps) == 1
        visit.advance(frame("menu_labs_game_speed_affordable"), boxes("menu_labs_game_speed_affordable"), device, now + 2)
        assert len(device.taps) == 2
        assert device.taps[-1] == (291, 711)
        visit.advance(frame("menu_labs_game_speed_confirmation"),
                      boxes("menu_labs_game_speed_confirmation"), device, now + 3)
        assert len(device.taps) == 2
        visit.advance(frame("menu_labs_game_speed_confirmation"),
                      boxes("menu_labs_game_speed_confirmation"), device, now + 4)
        assert len(device.taps) == 3
        for step in (5, 6, 7):
            visit.advance(frame("menu_labs_game_speed_running"),
                          boxes("menu_labs_game_speed_running"), device, now + step)
        result = visit.advance(frame("menu_main_labs_unlocked"), (), device, now + 8)
    assert result is not None
    assert result.status == "started"
    assert result.observed_coin_spend == 300
    assert len(result.confirmed_readings) == 2


def test_changed_confirmation_price_cancels_without_spending() -> None:
    visit = LabVisit(vision.TemplateCache(Path("templates")))
    device = Device()
    visit.request()
    confirmation = tuple(ocr.TextBox("301", box.confidence, box.rect)
                         if box.text == "300" else box
                         for box in boxes("menu_labs_game_speed_confirmation"))
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        visit.advance(frame("menu_labs_slot1_affordable"), boxes("menu_labs_slot1_affordable"), device, 10)
        visit.advance(frame("menu_labs_game_speed_affordable"), boxes("menu_labs_game_speed_affordable"), device, 11)
        visit.advance(frame("menu_labs_game_speed_affordable"), boxes("menu_labs_game_speed_affordable"), device, 12)
        visit.advance(frame("menu_labs_game_speed_confirmation"), confirmation, device, 13)
        visit.advance(frame("menu_labs_game_speed_confirmation"), confirmation, device, 14)
    assert visit.last_tap is not None
    assert visit.last_tap[0] == "cancel_confirmation"
    assert len(device.taps) == 3  # Lab 1, row, Cancel; never Research.


def test_research_tap_without_running_job_times_out_and_cancels() -> None:
    visit, device = setup()
    visit.request()
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        visit.advance(frame("menu_labs_slot1_affordable"), boxes("menu_labs_slot1_affordable"), device, 10)
        for second in (11, 12):
            visit.advance(frame("menu_labs_game_speed_affordable"),
                          boxes("menu_labs_game_speed_affordable"), device, second)
        for second in (13, 14):
            visit.advance(frame("menu_labs_game_speed_confirmation"),
                          boxes("menu_labs_game_speed_confirmation"), device, second)
        assert visit.last_tap is not None and visit.last_tap[0] == "confirm_game_speed"
        for second in range(15, 30):
            visit.advance(frame("menu_labs_game_speed_confirmation"),
                          boxes("menu_labs_game_speed_confirmation"), device, second)
        assert visit.last_tap is not None and visit.last_tap[0] == "cancel_confirmation"
        visit.advance(frame("menu_labs_game_speed_affordable"),
                      boxes("menu_labs_game_speed_affordable"), device, 30)
        visit.advance(frame("menu_labs_slot1_affordable"),
                      boxes("menu_labs_slot1_affordable"), device, 31)
        result = visit.advance(frame("menu_main_labs_unlocked"), (), device, 32)
    assert result is not None
    assert result.status == "failed"
    assert result.observed_coin_spend == 0


def test_changed_price_resets_confirmation_and_timeout_records_no_purchase() -> None:
    def changing(screen: object, text: tuple[ocr.TextBox, ...]) -> LabPickerReading:
        reading = affordable(screen, text)
        if not reading.page:
            return reading
        changing.calls += 1
        assert reading.game_speed is not None
        return replace(reading, game_speed=replace(reading.game_speed,
                       cost=300 if changing.calls % 2 else 301))
    changing.calls = 0
    visit = LabVisit(vision.TemplateCache(Path("templates")), picker_reader=changing)
    device = Device()
    visit.request()
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        visit.advance(frame("menu_labs_slot1_idle"), boxes("menu_labs_slot1_idle"), device, 10)
        visit.advance(frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker"), device, 11)
        visit.advance(frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker"), device, 12)
        assert len(device.taps) == 1
        result = visit.advance(frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker"), device, 101)
    assert result is not None
    assert result.status == "failed"
    assert result.observed_coin_spend == 0
