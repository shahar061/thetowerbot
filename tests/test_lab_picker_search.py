"""Scroll the picker until the target card is fully visible, within a bounded budget."""
from __future__ import annotations

from dataclasses import dataclass

from ocr import TextBox
import config
from lab_picker import MAX_SWIPES, MAX_UP_SWIPES, PickerSearch
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
    assert first.kind == "swipe" and search.up_swipes == 1
    found = search.step(page(card("labs.damage", 100), card("labs.health", 1100)))
    assert found.kind == "found"


def test_the_down_swipe_stays_inside_the_list_viewport() -> None:
    search = PickerSearch("labs.health", W)
    top_of_list = page(card("labs.damage", 600))
    assert search.step(top_of_list).kind == "swipe"  # up: the list does not move
    step = search.step(top_of_list)
    x, y1, x2, y2 = step.swipe
    top, bottom = VIEW
    assert x == x2 == W // 5
    assert top < y2 < y1 < bottom
    assert y1 - y2 == int((bottom - top) * .6)


def test_an_unmoving_list_after_a_swipe_is_the_end_of_the_list() -> None:
    search = PickerSearch("labs.missing", W)
    same = page(card("labs.damage", 600), card("labs.health", 900))
    assert search.step(same).kind == "swipe"  # up: unmoved, so this is the top
    assert search.step(same).kind == "swipe"  # down: unmoved, so this is the end
    assert search.step(same).kind == "not_found"
    assert (search.up_swipes, search.swipes) == (1, 1)


def test_the_search_gives_up_after_the_swipe_budget() -> None:
    search = PickerSearch("labs.missing", W)
    budget = MAX_UP_SWIPES + MAX_SWIPES
    steps = [search.step(page(card(f"labs.x{i}", 600 + i))).kind for i in range(budget + 1)]
    assert steps == ["swipe"] * budget + ["not_found"]


def test_a_closed_picker_waits() -> None:
    assert PickerSearch("labs.health", W).step(PickerPage(False, None, None)).kind == "wait"


@dataclass
class Card:
    fully_visible: bool = True


class Page:
    """A fake picker list: `offset` cards scrolled; the target is card `at`."""
    def __init__(self, offset: int, at: int, size: int = 30) -> None:
        self.offset, self.at, self.size = offset, at, size
        self.open, self.viewport = True, (300, 1500)

    def card(self, lab_id: str) -> Card | None:
        return Card() if self.offset <= self.at < self.offset + 6 else None

    def signature(self) -> tuple[str, int]:
        return ("page", self.offset)


def drive(search: PickerSearch, offset: int, at: int, limit: int = 40) -> tuple[str, int]:
    for _ in range(limit):
        step = search.step(Page(offset, at))
        if step.kind != "swipe":
            return step.kind, offset
        x1, y1, x2, y2 = step.swipe
        offset = max(0, offset - 5) if y2 > y1 else min(24, offset + 5)  # up swipe: y grows
    return "limit", offset


def test_scrolled_picker_scrolls_up_to_game_speed() -> None:
    # The incident: the list was left 20 cards down; Game Speed is card 0.
    assert drive(PickerSearch("labs.game-speed", 1080), offset=20, at=0) == ("found", 0)


def test_target_below_still_found_after_reaching_top() -> None:
    assert drive(PickerSearch("labs.coins-wave", 1080), offset=10, at=18)[0] == "found"


def test_absent_target_ends_not_found_not_waiting() -> None:
    assert drive(PickerSearch("labs.missing", 1080), offset=10, at=999)[0] == "not_found"


def test_visible_target_needs_no_swipe() -> None:
    assert PickerSearch("labs.game-speed", 1080).step(Page(0, 0)).kind == "found"


def test_the_up_swipe_stays_inside_the_list_viewport() -> None:
    search = PickerSearch("labs.health", W)
    x, y1, x2, y2 = search.step(page(card("labs.damage", 600))).swipe
    top, bottom = VIEW
    assert x == x2 == W // 5
    assert top < y1 < y2 < bottom
    assert y2 - y1 == int((bottom - top) * .6)
