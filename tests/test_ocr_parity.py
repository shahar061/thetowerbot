"""Spec P0a parity: skipping detector upscaling changes no parsed result.

Real engine, like tests/test_ocr.py's read_region tests.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import pytest

import config
import ocr
import pages
import screens
import shopping
import vision
from account_screens import ScreenReadings
from perception import read_cash

FIXTURES = Path(__file__).parent / "fixtures"
EVERY_FRAME = sorted(FIXTURES.glob("*.png")) + sorted((FIXTURES / "account_screens").glob("*.png"))


def _image(path: Path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    assert image is not None, path
    return image


@pytest.mark.parametrize("name", ["in_run_lit", "in_run_defense_1920", "menu_main",
                                  "menu_missions_bluestacks_1920"])
def test_full_frame_boxes_are_identical_without_upscaling(
    name: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "OCR_DET_UPSCALE", False)
    frame = _image(FIXTURES / f"{name}.png")
    assert ocr.read(frame, upscale=False) == ocr.read(frame)


@pytest.mark.parametrize("path", EVERY_FRAME, ids=lambda p: p.name)
def test_the_account_title_reader_is_unchanged_without_upscaling(
    path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = _image(path)
    results = []
    for upscale in (True, False):
        monkeypatch.setattr(config, "OCR_DET_UPSCALE", upscale)
        readings = ScreenReadings()
        held = readings.scan(frame)
        results.append((held, readings.current_evidence()))
    assert results[0] == results[1]


@pytest.mark.parametrize("upscale", [True, False])
def test_crop_readers_that_need_upscaling_keep_it(
    upscale: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review focus 3: these crops are only detected enlarged, so they never opt out."""
    monkeypatch.setattr(config, "OCR_DET_UPSCALE", upscale)
    cache = vision.TemplateCache(config.TEMPLATE_DIR)
    menu = _image(FIXTURES / "menu_main.png")
    page = pages.classify_page(menu, cache)
    assert shopping.header_numbers(menu, page.page, page.top_left) == (78, 0)
    battle = _image(FIXTURES / "in_run_lit.png")
    cash = screens.classify(battle, cache).cash_top_left
    assert cash is not None
    assert read_cash(battle, cash, config.WALLET_FROM_CASH) == 89
