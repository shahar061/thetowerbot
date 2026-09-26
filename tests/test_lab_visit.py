"""A lab visit must not call a research tap a purchase without fresh proof."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch

import cv2
import pytest

import config
from lab_screen import (LabConfirmationReading, LabHomeReading, LabPickerReading,
                        read_confirmation, read_home, read_picker)
from lab_visit import LabVisit
import ocr
from supervisor import RecoveryState
import screens
from strategy import Shopping
from tests.conftest import _shopping_bot
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


def test_lab_tab_state_distinguishes_visible_lock_from_unlocked_tab() -> None:
    visit, _ = setup()

    assert visit.tab_status(frame("main_menu")) == "locked"
    assert visit.tab_status(frame("menu_main_labs_unlocked")) == "unlocked"


def test_unreadable_lab_tab_stays_unknown() -> None:
    visit, _ = setup()
    unreadable = frame("main_menu")
    unreadable[:] = 0

    assert visit.tab_status(unreadable) == "unknown"


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


@pytest.mark.parametrize(("fixture", "expected_screen"), [
    ("menu_labs_game_speed_picker", "LAB_PICKER"),
    ("menu_labs_game_speed_confirmation", "LAB_CONFIRMATION"),
])
def test_active_lab_dialog_is_named_to_recovery_preflight(
    fixture: str, expected_screen: str,
) -> None:
    class RecordingSupervisor:
        current_account = "account-a"

        def __init__(self) -> None:
            self.screen: str | None = None

        def observe(self, **kwargs: object) -> RecoveryState:
            self.screen = str(kwargs["screen"])
            return RecoveryState.BLOCKED

    bot = _shopping_bot(fixture, state=screens.ScreenState.UNKNOWN,
                        policy=Shopping(), auto_navigate=False)
    bot.lab_visit = LabVisit(bot.templates)
    assert bot.lab_visit.request()
    guard = RecordingSupervisor()
    bot.supervisor = guard

    bot.run_once()

    assert guard.screen == expected_screen


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


@pytest.mark.parametrize(("after", "status"), [
    (114, "started"),  # "2.61K" was a real 2614; 2614 - 2500 = 114
    (60, "failed"),    # further from 110 than "2.61K" can hide
])
def test_research_debit_is_proved_within_the_abbreviated_coin_header(
    after: int, status: str,
) -> None:
    def picker(screen: object, text: tuple[ocr.TextBox, ...]) -> LabPickerReading:
        reading = read_picker(screen, text)
        if not reading.page:
            return reading
        assert reading.game_speed is not None
        return replace(reading, coin_balance=2610, buy_point=(290, 719),
                       game_speed=replace(reading.game_speed, level=2, cost=2500.,
                                          status="available"))

    def confirmation(screen: object, text: tuple[ocr.TextBox, ...]) -> LabConfirmationReading:
        reading = read_confirmation(screen, text)
        return replace(reading, coin_balance=2610, price=2500) if reading.page else reading

    def home(screen: object, text: tuple[ocr.TextBox, ...]) -> LabHomeReading:
        reading = read_home(screen, text)
        return (replace(reading, coin_balance=after)
                if reading.slot_status == "researching" else reading)

    visit = LabVisit(vision.TemplateCache(Path("templates")), home_reader=home,
                     picker_reader=picker, confirmation_reader=confirmation)
    device = Device()
    visit.request()
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        for step, name in enumerate((
                "menu_labs_slot1_affordable", "menu_labs_game_speed_affordable",
                "menu_labs_game_speed_affordable", "menu_labs_game_speed_confirmation",
                "menu_labs_game_speed_confirmation", "menu_labs_game_speed_running",
                "menu_labs_game_speed_running", "menu_labs_game_speed_running")):
            visit.advance(frame(name), boxes(name), device, 10 + step)
        result = visit.advance(frame("menu_main_labs_unlocked"), (), device, 20)
    assert result is not None
    assert result.status == status
    assert result.observed_coin_spend == (2500 if status == "started" else 0)


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


def test_labs_intro_popup_is_closed_before_lab_one_is_read() -> None:
    visit, device = setup()
    visit.request()
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        assert visit.advance(frame("menu_labs_intro_popup"),
                             boxes("menu_labs_intro_popup"), device, 10) is None
        assert visit.last_tap is not None and visit.last_tap[0] == "close_labs_intro"
        x, y = device.taps[-1]
        assert abs(x - 906) <= 4 and abs(y - 563) <= 4
        visit.advance(frame("menu_labs_slot1_idle"), boxes("menu_labs_slot1_idle"), device, 11)
    assert visit.last_tap is not None and visit.last_tap[0] == "open_lab_one"


def test_lab_one_is_checked_first_then_second_lab_unlocks_with_verified_debit() -> None:
    from lab_screen import read_home

    image = frame("menu_labs_slot1_idle")
    original = boxes("menu_labs_slot1_idle")
    locked = tuple(ocr.TextBox("119", box.confidence, box.rect)
                   if box.text == "65" else box for box in original)
    price = next(box for box in locked if box.text == "100")
    gem = next(box for box in locked if box.text == "119")
    owned = tuple(box for box in locked if box.text not in {"Unlock Znd lab", "100", "119"}) + (
        ocr.TextBox("19", gem.confidence, gem.rect),
        ocr.TextBox("Lab Offline", .99, config.Rect(394, 832, 291, 52)),
        ocr.TextBox("Lab 3", .99, config.Rect(25, 1046, 97, 39)),
        ocr.TextBox("Unlock 3rd lab", .99, config.Rect(351, 1174, 378, 51)),
    )
    assert read_home(image, locked).slot2_price == 100
    assert read_home(image, owned).slot2_status == "owned"
    visit, device = setup()
    visit.request()
    picker = frame("menu_labs_game_speed_picker")
    picker_text = boxes("menu_labs_game_speed_picker")
    with patch("lab_visit.tap", side_effect=lambda _device, x, y: device.taps.append((x, y))):
        visit.advance(image, locked, device, 10.)
        assert visit.last_tap is not None and visit.last_tap[0] == "open_lab_one"
        visit.advance(picker, picker_text, device, 11.)
        visit.advance(picker, picker_text, device, 12.)
        assert visit.last_tap is not None and visit.last_tap[0] == "close_picker"
        visit.advance(image, locked, device, 13.)
        assert visit.last_tap is None
        visit.advance(image, locked, device, 14.)
        assert visit.last_tap is not None and visit.last_tap[0] == "unlock_lab_two"
        assert device.taps[-1] == (price.rect.x + price.rect.w // 2,
                                   price.rect.y + price.rect.h // 2)
        visit.advance(image, owned, device, 15.)
        visit.advance(image, owned, device, 16.)
        visit.advance(image, owned, device, 17.)
        assert visit.last_tap is not None and visit.last_tap[0] == "return_to_battle"
        result = visit.advance(frame("menu_main_labs_unlocked"), (), device, 18.)
    assert result is not None
    assert result.status == "observed"
    assert result.decision.kind == "wait_coins"
    assert result.slot2_status == "owned"
    assert result.observed_gem_spend == 100
    assert result.gems_before == 119 and result.gem_balance == 19
