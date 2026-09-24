"""The green "new" arrow over a bottom tab, read by colour above the tab."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import config
from nav_arrow import arrow_over
from vision import TemplateCache

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def templates() -> TemplateCache:
    return TemplateCache(config.TEMPLATE_DIR)


def frame(name: str) -> np.ndarray:
    image = cv2.imread(str(FIXTURES / name))
    assert image is not None, name
    return image


@pytest.mark.parametrize("name", [
    "menu_main.png",                             # main menu, 1080x2400
    "menu_main_bluestacks_1920.png",             # main menu, 1080x1920
    "menu_workshop.png",                         # the arrow bobs lower here
    "menu_workshop_utility_tutorial_arrow.png",  # next to the Workshop's own arrow
])
def test_an_unvisited_cards_tab_reads_as_arrowed(templates: TemplateCache, name: str) -> None:
    assert arrow_over(frame(name), templates, "CARDS") is True


@pytest.mark.parametrize("name", [
    "menu_cards.png",         # the Cards page's own green glow sits beside the tab
    "menu_cards_stocked.png",
    "menu_labs_active.png",   # a few green pixels from the Labs page above
    "main_menu_resume.png",
    "menu_workshop_utility.png",
])
def test_a_visited_cards_tab_reads_as_clear(templates: TemplateCache, name: str) -> None:
    assert arrow_over(frame(name), templates, "CARDS") is False


def test_the_workshop_arrow_is_not_read_as_the_cards_arrow(templates: TemplateCache) -> None:
    """main_menu.png floats its arrow over the Workshop tab, one column left."""
    screen = frame("main_menu.png")
    assert arrow_over(screen, templates, "WORKSHOP") is True
    assert arrow_over(screen, templates, "CARDS") is not True


def test_no_tab_is_unknown_not_clear(templates: TemplateCache) -> None:
    assert arrow_over(frame("in_run_early.png"), templates, "CARDS") is None
