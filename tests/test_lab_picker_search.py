"""Scroll the picker until the target card is fully visible, within a bounded budget."""
from __future__ import annotations

from ocr import TextBox
import config
from lab_picker import MAX_SWIPES, PickerSearch
from lab_screen import PickerCard, PickerPage

W, H = 1080, 2400
VIEW = (440, 2172)


def card(lab_id: str, y: int, *, visible: bool = True) -> PickerCard:
    name = TextBox(f"{lab_id} Lv.1", .99, config.Rect(90, y, 300, 40))
    return PickerCard(lab_id, name.text, 1, 30, 15., False, (0, y - 72, 540, 300), name, visible, "white")


def page(*cards: PickerCard) -> PickerPage:
    return PickerPage(True, 500, VIEW, cards)


def test_a_fully_visible_target_is_found_without_swiping() -> None:
    search = PickerSearch("labs.health", W)
    assert search.step(page(card("labs.damage", 600), card("labs.health", 900))).kind == "found"
    assert search.swipes == 0


def test_a_clipped_or_absent_target_swipes_then_is_found() -> None:
    search = PickerSearch("labs.health", W)
    first = search.step(page(card("labs.damage", 600), card("labs.health", 2100, visible=False)))
    assert first.kind == "swipe" and search.swipes == 1
    found = search.step(page(card("labs.damage", 100), card("labs.health", 1100)))
    assert found.kind == "found"


def test_the_swipe_stays_inside_the_list_viewport() -> None:
    search = PickerSearch("labs.health", W)
    step = search.step(page(card("labs.damage", 600)))
    x, y1, x2, y2 = step.swipe
    top, bottom = VIEW
    assert x == x2 == W // 5
    assert top < y2 < y1 < bottom
    assert y1 - y2 == int((bottom - top) * .6)


def test_an_unmoving_list_after_a_swipe_is_the_end_of_the_list() -> None:
    search = PickerSearch("labs.missing", W)
    same = page(card("labs.damage", 600), card("labs.health", 900))
    assert search.step(same).kind == "swipe"
    assert search.step(same).kind == "not_found"


def test_the_search_gives_up_after_the_swipe_budget() -> None:
    search = PickerSearch("labs.missing", W)
    steps = [search.step(page(card(f"labs.x{i}", 600 + i))).kind for i in range(MAX_SWIPES + 1)]
    assert steps == ["swipe"] * MAX_SWIPES + ["not_found"]


def test_a_closed_picker_waits() -> None:
    assert PickerSearch("labs.health", W).step(PickerPage(False, None, None)).kind == "wait"
