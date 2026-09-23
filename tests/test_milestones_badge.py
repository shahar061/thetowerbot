"""The MILESTONES badge, read by colour beside the located button."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

import config
from milestones_badge import badge_visible
from vision import TemplateCache

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def templates() -> TemplateCache:
    return TemplateCache(config.TEMPLATE_DIR)


def frame(name: str) -> np.ndarray:
    image = cv2.imread(str(FIXTURES / name))
    assert image is not None, name
    return image


def test_a_badged_main_menu_reads_as_badged(templates: TemplateCache) -> None:
    assert badge_visible(frame("menu_main_bluestacks_1920.png"), templates) is True


@pytest.mark.parametrize("name", ["menu_milestones_entry.png", "main_menu_resume.png"])
def test_an_unbadged_milestones_button_reads_as_clear(templates: TemplateCache,
                                                      name: str) -> None:
    """Both carry MISSIONS/settings/mail badges - red, but nowhere near."""
    assert badge_visible(frame(name), templates) is False


@pytest.mark.parametrize("name", ["menu_main.png", "in_run_early.png"])
def test_no_button_is_unknown_not_clear(templates: TemplateCache, name: str) -> None:
    """A menu before MILESTONES unlocks, or a battle: nothing was seen."""
    assert badge_visible(frame(name), templates) is None


def test_the_badge_is_what_is_read(templates: TemplateCache) -> None:
    """Paint the badge out of the badged capture and the reading flips."""
    image = frame("menu_main_bluestacks_1920.png")
    region = config.MILESTONES_BADGE_REGION
    x, y = 407 + region.dx, 663 + region.dy  # the button's measured top-left
    image[y:y + region.h, x:x + region.w] = image[y - region.h:y, x:x + region.w]
    assert badge_visible(image, templates) is False
