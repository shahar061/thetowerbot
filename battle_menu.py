"""Readers for the in-battle hamburger menu and the pages it opens.

Pure: nothing here taps. Icons are found by template and their badge is read
from the icon frame's own top-left corner, so a dot on one icon never counts
for its neighbour.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

import config
from account_collection import locate_control
from config import Rect
from device import Image
from geometry import supported_frame
from milestones_badge import red_pixels
from ocr import TextBox
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


# --- Page readers (OCR) -----------------------------------------------------
#
# These read the pages the in-battle menu opens: the Event page and its info
# modal, and the Store. All of them carry real-money buy buttons, so a
# reader here must never hand back a price as a tap target - `is_price`
# exists to keep that promise, and `free_gem_tile` leans
# on it rather than trusting position alone.

Page = Literal["event_info", "event", "store", "other", "none"]
# Digit pattern catches "N49.90" too: OCR sometimes reads the shekel glyph
# (₪) as a stray Latin letter, but the "digits + . or , + 2 digits"
# shape survives regardless of what (if anything) precedes it.
_PRICE = re.compile(r"[₪$€£]|\d+[.,]\d{2}\b")
RETURN_TEXT = "tap to return to game"


@dataclass(frozen=True)
class PageReading:
    page: Page
    return_point: tuple[int, int] | None


def _centre(rect: Rect) -> tuple[int, int]:
    return (rect.x + rect.w // 2, rect.y + rect.h // 2)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def is_price(text: str) -> bool:
    return bool(_PRICE.search(text))


def read_page(boxes: tuple[TextBox, ...]) -> PageReading:
    """Which menu destination is on screen, and where its way back is."""
    footer = next((b for b in boxes if RETURN_TEXT in _norm(b.text)), None)
    if footer is None:
        return PageReading("none", None)
    texts = [_norm(b.text) for b in boxes]
    if any("event information" in t for t in texts):
        page: Page = "event_info"
    # OCR sometimes drops the space around the title's dash ("EVENT-STEAMPUNK"),
    # so match with or without one rather than a fixed literal.
    elif any(re.match(r"event\s*-", t) for t in texts):
        page = "event"
    elif any(t.startswith("store") for t in texts):
        page = "store"
    else:
        page = "other"
    return PageReading(page, _centre(footer.rect))


def event_modal_close(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """The bright green X on the EVENT INFORMATION title row."""
    title = next((b for b in boxes if "event information" in _norm(b.text)), None)
    if title is None:
        return None
    r = title.rect
    x0, x1 = r.x + r.w, min(screen.shape[1], r.x + r.w + 260)
    y0, y1 = max(0, r.y - 40), r.y + r.h + 40
    hsv = cv2.cvtColor(screen[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    green = (hsv[..., 0] >= 60) & (hsv[..., 0] <= 95) & (hsv[..., 1] > 120) & (hsv[..., 2] > 150)
    ys, xs = np.nonzero(green)
    if len(xs) < 150:
        return None
    return (x0 + int(np.median(xs)), y0 + int(np.median(ys)))


def _is_store_page(boxes: tuple[TextBox, ...]) -> bool:
    return any(_norm(b.text).startswith("store") for b in boxes)


def _tile_contains(tile: Rect, rect: Rect) -> bool:
    cx, cy = _centre(rect)
    return tile.x <= cx <= tile.x + tile.w and tile.y <= cy <= tile.y + tile.h


def free_gem_tile(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """The ▶ under the Store's exact 'FREE' caption, only on the Store page,
    only if no price shares its tile."""
    if not _is_store_page(boxes):
        return None
    for box in boxes:
        if box.text.strip() != "FREE":
            continue
        cx, cy = _centre(box.rect)
        tile = Rect(cx - 150, cy - 250, 300, 450)
        if any(is_price(b.text) and _tile_contains(tile, b.rect) for b in boxes):
            continue
        button = (cx, cy + 138)
        # The ▶ button carries its own red dot at its top-right corner.
        dot = config.BATTLE_MENU_FREE_DOT_PATCH
        x0, x1 = button[0] + dot.x, button[0] + dot.x + dot.w
        y0, y1 = button[1] + dot.y, button[1] + dot.y + dot.h
        patch = screen[max(0, y0):y1, max(0, x0):x1]
        if patch.size and red_pixels(patch) >= config.BATTLE_MENU_BADGE_MIN_PIXELS:
            return button
    return None
