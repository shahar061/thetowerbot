"""Readers for the in-battle hamburger menu and the pages it opens.

Pure: nothing here taps. Icons are found by template and their badge is read
from the icon frame's own top-left corner, so a dot on one icon never counts
for its neighbour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

import config
from account_collection import locate_control
from device import Image
from geometry import supported_frame
from milestones_badge import red_pixels
from vision import TemplateCache

Icon = Literal["cart", "missions", "cards", "labs", "event"]
ICONS: tuple[Icon, ...] = ("cart", "missions", "cards", "labs", "event")


@dataclass(frozen=True)
class Badge:
    color: Literal["red", "blue"]


@dataclass(frozen=True)
class IconReading:
    icon: Icon
    point: tuple[int, int]
    badge: Badge | None


@dataclass(frozen=True)
class MenuButton:
    point: tuple[int, int]
    badged: bool


def _locate(screen: Image, templates: TemplateCache, name: str):
    return locate_control(screen, templates.get(config.BATTLE_MENU_TEMPLATES[name]), name)


def blue_pixels(patch: Image) -> int:
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)
    lo, hi = config.BATTLE_MENU_BLUE_HUE
    mask = ((hsv[..., 0] >= lo) & (hsv[..., 0] <= hi)
            & (hsv[..., 1] >= config.BATTLE_MENU_BLUE_MIN_SAT)
            & (hsv[..., 2] >= config.BATTLE_MENU_BLUE_MIN_VAL))
    return int(np.count_nonzero(mask))


def badge_at(screen: Image, top_left: tuple[int, int]) -> Badge | None:
    """The badge on the corner of an icon whose template starts at `top_left`."""
    patch_rect = config.BATTLE_MENU_BADGE_PATCH
    height, width = screen.shape[:2]
    x0, y0 = top_left[0] + patch_rect.x, top_left[1] + patch_rect.y
    patch = screen[max(0, y0):min(height, y0 + patch_rect.h),
                   max(0, x0):min(width, x0 + patch_rect.w)]
    if patch.size == 0:
        return None
    if red_pixels(patch) >= config.BATTLE_MENU_BADGE_MIN_PIXELS:
        return Badge("red")
    if blue_pixels(patch) >= config.BATTLE_MENU_BADGE_MIN_PIXELS:
        return Badge("blue")
    return None


def collapsed(screen: Image, templates: TemplateCache) -> MenuButton | None:
    """The closed menu's hamburger and whether its corner carries a dot."""
    if not supported_frame(screen.shape[1], screen.shape[0]):
        return None
    target = _locate(screen, templates, "hamburger")
    if target.status != "located":
        return None
    return MenuButton(target.point, badge_at(screen, target.rect[:2]) is not None)


def close_point(screen: Image, templates: TemplateCache) -> tuple[int, int] | None:
    """The open menu's X, or None when the menu is not open."""
    if not supported_frame(screen.shape[1], screen.shape[0]):
        return None
    close = _locate(screen, templates, "close")
    exit_battle = _locate(screen, templates, "exit_battle")
    if close.status != "located" or exit_battle.status != "located":
        return None
    return close.point


def read_menu(screen: Image, templates: TemplateCache) -> dict[Icon, IconReading] | None:
    """Every non-settings icon of the open menu with its badge, or None."""
    if close_point(screen, templates) is None:
        return None
    readings: dict[Icon, IconReading] = {}
    for icon in ICONS:
        target = _locate(screen, templates, icon)
        if target.status != "located":
            return None
        readings[icon] = IconReading(icon, target.point, badge_at(screen, target.rect[:2]))
    return readings
