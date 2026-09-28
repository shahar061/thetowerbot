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
