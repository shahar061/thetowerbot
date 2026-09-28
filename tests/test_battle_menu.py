from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import battle_menu
import config
from vision import TemplateCache

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def templates() -> TemplateCache:
    return TemplateCache(Path(__file__).resolve().parent.parent / "templates")


def frame(name: str) -> np.ndarray:
    image = cv2.imread(str(FIX / f"{name}.png"))
    assert image is not None, name
    return image


def test_collapsed_hamburger_is_badged(templates):
    button = battle_menu.collapsed(frame("battle_menu/collapsed_badged"), templates)
    assert button is not None and button.badged
    assert button.point[0] > 950 and button.point[1] < 130


def test_collapsed_hamburger_clear_when_dot_painted_out(templates):
    screen = frame("battle_menu/collapsed_badged").copy()
    button = battle_menu.collapsed(screen, templates)
    x, y = button.point
    # Paint out the badge patch measured from the hamburger's own located
    # rect (clamped at 0), matching what battle_menu.badge_at reads.
    target = battle_menu._locate(screen, templates, "hamburger")
    patch_rect = config.BATTLE_MENU_BADGE_PATCH
    tx, ty = target.rect[:2]
    x0, y0 = max(0, tx + patch_rect.x), max(0, ty + patch_rect.y)
    x1, y1 = x0 + patch_rect.w, y0 + patch_rect.h
    screen[y0:y1, x0:x1] = screen[300, 100]
    assert battle_menu.collapsed(screen, templates).badged is False


def test_collapsed_is_none_when_menu_open(templates):
    assert battle_menu.collapsed(frame("battle_menu/open_badged"), templates) is None


def test_open_menu_badges(templates):
    menu = battle_menu.read_menu(frame("battle_menu/open_badged"), templates)
    assert menu is not None
    assert set(menu) == set(battle_menu.ICONS)
    assert menu["cart"].badge == battle_menu.Badge("red")
    assert menu["event"].badge == battle_menu.Badge("blue")
    for icon in ("missions", "cards", "labs"):
        # Settings' red dot sits beside Missions and must not bleed into it.
        assert menu[icon].badge is None, icon


def test_read_menu_none_when_collapsed(templates):
    assert battle_menu.read_menu(frame("battle_menu/collapsed_badged"), templates) is None


def test_close_point_only_when_open(templates):
    assert battle_menu.close_point(frame("battle_menu/open_badged"), templates) is not None
    assert battle_menu.close_point(frame("battle_menu/collapsed_badged"), templates) is None


@pytest.mark.parametrize("name", ["in_run_early", "in_run_fast", "in_run_defense"])
def test_existing_in_run_frames_never_read_as_open_menu(templates, name):
    assert battle_menu.read_menu(frame(name), templates) is None


def test_unsupported_frame_reads_nothing(templates):
    small = cv2.resize(frame("battle_menu/open_badged"), (540, 1200))
    assert battle_menu.read_menu(small, templates) is None
    assert battle_menu.collapsed(small, templates) is None


import ocr

needs_ocr = pytest.mark.skipif(not ocr.available(), reason="OCR engine not installed")


def boxes(name):
    return ocr.read(frame(name))


@needs_ocr
@pytest.mark.parametrize("name,page", [
    ("battle_menu/event_page", "event"),
    ("battle_menu/event_info_modal", "event_info"),
    ("battle_menu/store_top", "store"),
    ("battle_menu/store_free_tiles", "store"),
])
def test_read_page(name, page):
    reading = battle_menu.read_page(boxes(name))
    assert reading.page == page
    assert reading.return_point is not None and reading.return_point[1] > 2100


@needs_ocr
def test_in_run_frame_is_not_a_page():
    assert battle_menu.read_page(boxes("battle_menu/open_badged")).page == "none"


@needs_ocr
def test_event_modal_close_is_right_of_title():
    screen = frame("battle_menu/event_info_modal")
    point = battle_menu.event_modal_close(screen, boxes("battle_menu/event_info_modal"))
    assert point is not None and 820 < point[0] < 1000 and 400 < point[1] < 580


@needs_ocr
def test_event_page_has_no_ready_claims_and_never_offers_the_boost():
    assert battle_menu.event_claims(boxes("battle_menu/event_page")) == []


def test_event_claims_skip_rows_with_a_price():
    R = config.Rect
    fake = (
        ocr.TextBox("EVENT BOOST + GEMS + RELICS", 0.9, R(30, 900, 700, 60)),
        ocr.TextBox("Claim", 0.9, R(800, 1150, 120, 50)),       # inside boost card
        ocr.TextBox("₪49.90", 0.9, R(800, 1130, 200, 60)),
        ocr.TextBox("Claim", 0.9, R(800, 1800, 120, 50)),       # a mission row
    )
    assert battle_menu.event_claims(fake) == [(860, 1825)]


@needs_ocr
def test_free_gem_tile_found_on_scrolled_store():
    screen = frame("battle_menu/store_free_tiles")
    point = battle_menu.free_gem_tile(screen, boxes("battle_menu/store_free_tiles"))
    assert point is not None
    # Left column, below the FREE caption, never the website tile to its right.
    assert point[0] < 540


@needs_ocr
def test_free_gem_tile_absent_on_store_top():
    assert battle_menu.free_gem_tile(frame("battle_menu/store_top"),
                                     boxes("battle_menu/store_top")) is None


def test_free_gem_tile_rejects_offerwall_and_priced_tiles():
    R = config.Rect
    screen = frame("battle_menu/store_top")
    fake = (
        ocr.TextBox("complete offers for free gems", 0.9, R(60, 1900, 400, 40)),
        ocr.TextBox("FREE", 0.9, R(280, 900, 100, 50)),
        ocr.TextBox("₪17.90", 0.9, R(250, 1040, 160, 50)),
    )
    assert battle_menu.free_gem_tile(screen, fake) is None


@pytest.mark.parametrize("text,price", [
    ("₪49.90", True), ("$4.99", True), ("17,90", True), ("x 20", False), ("Claim", False),
])
def test_is_price(text, price):
    assert battle_menu.is_price(text) is price
