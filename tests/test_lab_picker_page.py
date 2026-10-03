"""The whole visible picker, card by card, for search and selection."""
from __future__ import annotations

import lab_screen
from tests.test_lab_visit import boxes, frame


def page(name: str = "menu_labs_game_speed_picker") -> lab_screen.PickerPage:
    return lab_screen.read_picker_page(frame(name), boxes(name))


def test_every_visible_card_is_read_with_its_price_and_border() -> None:
    result = page()
    assert result.open and result.balance == 122
    by_id = {card.lab_id: card for card in result.cards}
    assert by_id["labs.game-speed"].price == 300 and by_id["labs.game-speed"].border == "red"
    assert by_id["labs.starting-cash"].price == 30 and by_id["labs.starting-cash"].border == "white"
    assert by_id["labs.labs-coin-discount"].price == 40
    assert by_id["labs.attack-speed"].level == 1 and by_id["labs.attack-speed"].seconds == 15.
    assert all(card.fully_visible for card in result.cards if card.lab_id == "labs.health")


def test_a_card_cut_off_by_the_panel_bottom_is_not_fully_visible() -> None:
    # OCR merges the clipped card's text to "DefenseAbsoluteLv.1" with no spaces at
    # all; it must still resolve to its registry id and be read as not fully visible.
    result = page()
    defense = result.card("labs.defense-absolute")
    assert defense is not None and defense.raw_name == "DefenseAbsoluteLv.1"
    assert not defense.fully_visible


def test_selection_is_a_lookup_on_the_page_and_needs_a_white_border() -> None:
    image, text = frame("menu_labs_game_speed_picker"), boxes("menu_labs_game_speed_picker")
    speed = lab_screen.read_selected_picker(image, text, research_id="labs.game-speed")
    assert speed.entry.status == "unavailable" and speed.buy_point is None
    cash = lab_screen.read_selected_picker(image, text, research_id="labs.starting-cash")
    assert cash.entry.status == "available" and cash.buy_point is not None


def test_lower_picker_row_uses_its_visible_white_border() -> None:
    image, text = frame("menu_labs_slot3_available"), boxes("menu_labs_slot3_available")
    for research_id in ("labs.coins-kill-bonus", "labs.coins-wave"):
        selected = lab_screen.read_selected_picker(image, text, research_id=research_id)
        assert selected.entry is not None
        assert (selected.entry.level, selected.entry.cost, selected.entry.status) == (
            1, 40., "available")
        assert selected.buy_point is not None


def test_the_signature_changes_when_the_list_moves() -> None:
    before = page()
    shifted = lab_screen.PickerPage(before.open, before.balance, before.viewport, tuple(
        lab_screen.replace_card_y(card, -200) for card in before.cards))
    assert before.signature() != shifted.signature()


def test_a_page_without_the_title_is_not_open() -> None:
    assert not page("menu_labs_active").open
