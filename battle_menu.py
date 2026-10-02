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
import ocr
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
    if close.status != "located":
        return None
    exit_battle = _locate(screen, templates, "exit_battle")
    if exit_battle.status == "located":
        return close.point
    # The live game's action was renamed from EXIT BATTLE to END ROUND.
    # Require that exact caption in the same menu area alongside the X;
    # a standalone X on an unrelated screen is not an open menu.
    region = Rect(830, 500, 250, 160)
    labels = ocr.read_region(screen, region)
    if any(b.confidence >= .8 and re.fullmatch(r"end\s*round", b.text.strip(), re.I)
           for b in labels):
        return close.point
    return None


def read_menu(screen: Image, templates: TemplateCache) -> dict[Icon, IconReading] | None:
    """The open menu's non-settings icons with their badges, or None when the
    menu is not open (its X and EXIT BATTLE are what prove it is).

    An icon that does not locate cleanly is simply left out: no event is
    running, or Labs / Cards are still locked on a young account. Missing
    means "not due", never a tap - an `ambiguous` or `unusable` locate is
    left out the same way, so only a `located` point can ever be tapped.
    """
    if close_point(screen, templates) is None:
        return None
    readings: dict[Icon, IconReading] = {}
    for icon in ICONS:
        target = _locate(screen, templates, icon)
        if target.status == "located":
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


def daily_ad_tile(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """Only the badged video button in the Store's exact 20-gem FREE tile."""
    button = free_gem_tile(screen, boxes)
    if button is None:
        return None
    if any(
        re.fullmatch(r"x\s*20", b.text.strip(), re.IGNORECASE)
        and abs(_centre(b.rect)[0] - button[0]) < 160
        and 150 < button[1] - _centre(b.rect)[1] < 380
        for b in boxes
    ):
        return button
    # On some Store frames the full-frame OCR misses the small "x 20";
    # its own tile crop reads the amount as a clean "20" instead.
    region = Rect(button[0] - 170, button[1] - 400, 340, 280)
    amounts = [b for b in ocr.read_region(screen, region)
               if re.fullmatch(r"(?:x\s*)?20", b.text.strip(), re.IGNORECASE)
               and b.confidence >= .8]
    return button if len(amounts) == 1 else None


def store_gems(screen: Image, boxes: tuple[TextBox, ...]) -> int | None:
    """Read the Store's gem header, refusing missing or ambiguous balances."""
    if not _is_store_page(boxes):
        return None
    width, height = screen.shape[1], screen.shape[0]
    band = [b for b in boxes if b.confidence >= .9
            and width * .35 < b.rect.x < width * .55
            and b.rect.y < height * .043]
    if not band:
        region = Rect(int(width * .35), 0, int(width * .2), int(height * .043))
        band = [b for b in ocr.read_region(screen, region)
                if b.confidence >= (.8 if len(b.text.strip()) == 1 else .9)]
    values = [v for b in band if (v := ocr.parse_number(b.text)) is not None]
    return values[0] if len(values) == 1 else None


def ad_reward_claim(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """Claim on the full-screen 20 GEMS reward, never a Store offer."""
    if _is_store_page(boxes) or any(is_price(b.text) for b in boxes):
        return None
    width, height = screen.shape[1], screen.shape[0]
    reward = [b for b in boxes if _norm(b.text) == "20 gems"
              and .45 * height < _centre(b.rect)[1] < .7 * height]
    claims = [b for b in boxes if _norm(b.text) == "claim"
              and .7 * height < _centre(b.rect)[1] < .9 * height
              and .25 * width < _centre(b.rect)[0] < .75 * width]
    return _centre(claims[0].rect) if len(reward) == len(claims) == 1 else None


def ad_upsell_close(screen: Image, boxes: tuple[TextBox, ...]) -> tuple[int, int] | None:
    """Close the observed Disable Ads end card, away from its purchase button."""
    texts = [_norm(b.text) for b in boxes]
    if _is_store_page(boxes) or "disable ads" not in texts:
        return None
    if not any(t.startswith("buy for") and is_price(t) for t in texts):
        return None
    width, height = screen.shape[1], screen.shape[0]
    return (int(width * .9), int(height * .055))


def ad_end_card_close(screen: Image, templates: TemplateCache) -> tuple[int, int] | None:
    """Locate a witnessed ad close control wherever the ad placed it."""
    found: list[tuple[int, int]] = []
    for variant in config.BATTLE_MENU_AD_CLOSE_VARIANTS:
        target = _locate(screen, templates, variant)
        if target.status == "ambiguous":
            return None
        if target.status == "located":
            found.append(target.point)
    return found[0] if len(found) == 1 else None
