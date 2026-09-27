"""Recorded Labs screens protect the slot-one purchase boundary."""

from __future__ import annotations

import json
from pathlib import Path

import cv2

import config
import ocr
import vision


FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name: str) -> tuple[ocr.TextBox, ...]:
    data = json.loads((FIXTURES / "ocr" / f"{name}.json").read_text())
    return tuple(ocr.TextBox(item["text"], item["confidence"],
                             config.Rect(*item["rect"])) for item in data)


def frame(name: str) -> object:
    result = cv2.imread(str(FIXTURES / f"{name}.png"))
    assert result is not None
    return result


def test_idle_slot_one_is_read_without_treating_lab_two_as_owned() -> None:
    import lab_screen

    result = lab_screen.read_home(frame("menu_labs_slot1_idle"),
                                  recorded("menu_labs_slot1_idle"))
    assert result.page
    assert result.slot_status == "idle"
    assert result.coin_balance == 122
    assert result.slots_owned == 1
    assert result.job is None
    assert result.slot_point is not None
    assert 20 < result.slot_point[0] < 1060
    assert 300 < result.slot_point[1] < 620


def test_active_slot_one_is_not_mistaken_for_idle() -> None:
    import lab_screen

    result = lab_screen.read_home(frame("menu_labs_active"),
                                  recorded("menu_labs_active"))
    assert result.page
    assert result.slot_status == "researching"
    assert result.job is not None
    assert result.job.slot == 1
    assert result.job.raw_name.startswith("Coins / Kill Bonus")
    assert result.slot_point is None


def test_all_five_recorded_slots_are_read_with_an_explicit_clock() -> None:
    from lab_screen import read_slots

    result = read_slots(frame("menu_labs_active"), recorded("menu_labs_active"),
                        observed_at=1000.)
    assert result.slots_owned == 5
    assert {j.slot for j in result.jobs if j.status == "idle"} == {2, 3, 4}
    assert {j.slot for j in result.jobs if j.status == "researching"} == {1, 5}
    assert result.strip_read()
    first = next(j for j in result.jobs if j.slot == 1)
    assert first.remaining_s == 5 * 86400 + 18 * 3600 + 11 * 60 + 13
    assert first.completes_at == 1000. + first.remaining_s
    assert (first.source_level, first.target_level) == (86, 87)
    assert first.native_repeat == "unknown"
    assert first.speed_multiplier is None


def test_slot_reader_refuses_duplicate_headers_and_unreadable_timers() -> None:
    from lab_screen import read_slots

    image, boxes = frame("menu_labs_active"), recorded("menu_labs_active")
    heading = next(b for b in boxes if b.text == "Lab 2")
    duplicated = read_slots(image, (*boxes, heading), observed_at=1000.)
    assert not duplicated.strip_read()
    assert next(j for j in duplicated.jobs if j.slot == 2).status == "unknown"
    unreadable = read_slots(image, tuple(b for b in boxes if b.text != "19d5h4m16s"),
                            observed_at=1000.)
    assert not unreadable.strip_read()
    assert next(j for j in unreadable.jobs if j.slot == 5).status == "unknown"


def test_partial_lab_frame_cannot_claim_the_whole_owned_strip() -> None:
    from lab_screen import read_slots

    image = frame("menu_labs_active")[:1800]
    # Simulates a stale full-frame OCR cache alongside a cropped screenshot.
    result = read_slots(image, recorded("menu_labs_active"), observed_at=1000.)
    assert result.slots_owned is None
    assert not result.strip_read()
    assert all(j.slot != 5 for j in result.jobs)
    assert {j.slot for j in result.jobs if j.status == "idle"} == {2, 3, 4}


def test_crop_below_last_timer_still_cannot_claim_a_complete_strip() -> None:
    from lab_screen import read_slots

    image = frame("menu_labs_active")[:2200]
    result = read_slots(image, recorded("menu_labs_active"), observed_at=1000.)
    assert len(result.jobs) == 5
    assert all(j.status in {"researching", "idle"} for j in result.jobs)
    assert result.slots_owned is None
    assert not result.strip_read()


def test_locked_next_slot_is_not_counted_as_owned() -> None:
    from lab_screen import read_slots

    result = read_slots(frame("menu_labs_slot1_idle"), recorded("menu_labs_slot1_idle"),
                        observed_at=1000.)
    assert result.slots_owned == 1
    assert [(j.slot, j.status) for j in result.jobs] == [(1, "idle")]
    assert result.strip_read()


def test_slot_reader_cannot_promote_a_research_picker_or_bad_clock() -> None:
    from lab_screen import read_slots

    result = read_slots(frame("menu_labs_game_speed_picker"),
                        recorded("menu_labs_game_speed_picker"), observed_at=1000.)
    assert result.slots_owned is None and not result.jobs
    result = read_slots(frame("menu_labs_active"), recorded("menu_labs_active"),
                        observed_at=float("nan"))
    assert result.slots_owned is None and not result.jobs


def test_duplicate_slot_ids_never_make_a_complete_strip() -> None:
    from dataclasses import replace
    from lab_screen import read_slots

    reading = read_slots(frame("menu_labs_active"), recorded("menu_labs_active"),
                         observed_at=1000.)
    assert not replace(reading, jobs=(reading.jobs[0],) * 5).strip_read()


def test_recorded_game_speed_job_and_coin_debit_are_readable() -> None:
    import lab_screen

    result = lab_screen.read_home(frame("menu_labs_game_speed_running"),
                                  recorded("menu_labs_game_speed_running"))
    assert result.page
    assert result.slot_status == "researching"
    assert result.job is not None
    assert result.job.concept_id == "labs.game-speed"
    assert result.job.slot == 1
    assert result.coin_balance == 313
    assert result.slots_owned == 1


def test_picker_pairs_game_speed_with_its_own_price_and_wallet() -> None:
    import lab_screen

    result = lab_screen.read_picker(frame("menu_labs_game_speed_picker"),
                                    recorded("menu_labs_game_speed_picker"))
    assert result.page
    assert result.game_speed is not None
    assert result.game_speed.concept_id == "labs.game-speed"
    assert result.game_speed.level == 1
    assert result.game_speed.cost == 300
    assert result.coin_balance == 122
    assert result.buy_point is None  # 122 coins cannot fund the 300-coin row.


def test_affordable_recorded_game_speed_row_has_measured_buy_point() -> None:
    import lab_screen

    result = lab_screen.read_picker(frame("menu_labs_game_speed_affordable"),
                                    recorded("menu_labs_game_speed_affordable"))
    assert result.page
    assert result.game_speed is not None
    assert result.game_speed.status == "available"
    assert result.game_speed.cost == 300
    assert result.coin_balance == 613
    assert result.buy_point is not None
    assert 200 < result.buy_point[0] < 400
    assert 650 < result.buy_point[1] < 800


def test_game_speed_confirmation_requires_its_own_coin_price_and_button() -> None:
    import lab_screen

    result = lab_screen.read_confirmation(frame("menu_labs_game_speed_confirmation"),
                                           recorded("menu_labs_game_speed_confirmation"))
    assert result.page
    assert result.name == "Game Speed Lv.1"
    assert (result.coin_balance, result.price) == (613, 300)
    assert result.research_point is not None
    assert result.cancel_point is not None
    assert result.research_point[0] > result.cancel_point[0]


def test_confirmation_with_unreadable_price_cannot_spend() -> None:
    import lab_screen

    boxes = tuple(box for box in recorded("menu_labs_game_speed_confirmation")
                  if box.text != "300")
    result = lab_screen.read_confirmation(frame("menu_labs_game_speed_confirmation"), boxes)
    assert result.page
    assert result.research_point is None


def test_ambiguous_confirmation_retains_only_its_cancel_control() -> None:
    import lab_screen

    boxes = recorded("menu_labs_game_speed_confirmation")
    name = next(box for box in boxes if box.text == "Game Speed Lv.1")
    result = lab_screen.read_confirmation(frame("menu_labs_game_speed_confirmation"),
                                           (*boxes, name))
    assert result.page
    assert result.research_point is None
    assert result.cancel_point is not None


def test_duplicate_game_speed_text_cannot_offer_a_purchase_point() -> None:
    import lab_screen

    boxes = recorded("menu_labs_game_speed_picker")
    name = next(box for box in boxes if box.text == "Game Speed Lv.1")
    result = lab_screen.read_picker(frame("menu_labs_game_speed_picker"),
                                    (*boxes, name))
    assert result.buy_point is None


def test_disabled_red_card_stays_untappable_with_stale_wallet_ocr() -> None:
    import lab_screen

    boxes = recorded("menu_labs_game_speed_picker")
    changed = tuple(ocr.TextBox("400", box.confidence, box.rect)
                    if box.text == "122" else box for box in boxes)
    result = lab_screen.read_picker(frame("menu_labs_game_speed_picker"), changed)
    assert result.coin_balance == 400
    assert result.buy_point is None


def test_explicit_max_label_on_game_speed_row_stops_research() -> None:
    import lab_screen

    original = recorded("menu_labs_game_speed_picker")
    changed = tuple(ocr.TextBox("MAXED", box.confidence, box.rect)
                    if box.text == "300" else box for box in original)
    result = lab_screen.read_picker(frame("menu_labs_game_speed_picker"), changed)
    assert result.game_speed is not None
    assert result.game_speed.status == "maxed"
    assert result.buy_point is None


def test_workshop_is_not_a_labs_page() -> None:
    import lab_screen

    workshop = frame("menu_workshop_attack")
    assert not lab_screen.read_home(workshop, ()).page
    assert not lab_screen.read_picker(workshop, ()).page


def test_labs_navigation_and_page_anchor_match_recorded_frames() -> None:
    import pages

    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    main = frame("menu_main_labs_unlocked")
    target = vision.locate_template(main, cache.get(config.NAV_TARGETS["LABS"]), .8)
    assert target is not None
    assert 740 < target.center[0] < 890
    assert 2220 < target.center[1] < 2380
    assert pages.classify_page(frame("menu_labs_slot1_idle"), cache).page == "LABS"
