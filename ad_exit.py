"""Locate a rewarded ad's exit without depending on the creative's artwork.

Ad SDK controls are often accessible even when their pixels change. Inspect
the foreground ad's accessibility tree first. WebView ads such as Unity's
playables expose no labels while playing, but draw their skip and close
glyphs in solid white over the creative, so their silhouettes are matched
next. Witnessed image templates remain for the remaining ad providers.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

import battle_menu
from ocr import TextBox
from device import Image
from vision import TemplateCache


_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_AD_ACTIVITY = ("adactivity", "rewardedactivity", "interstitialactivity")
# SDK packages whose every Activity is an ad, e.g. Unity's
# com.unity3d.ads.adplayer.FullScreenWebViewDisplay.
_AD_SDK_PACKAGES = ("/com.unity3d.ads.", "/com.unity3d.services.ads.")
_META_AD_ACTIVITY = (
    "com.techtreegames.thetower/com.facebook.ads.audiencenetworkactivity"
)
_PLAY_STORE_OVERLAY = (
    "com.android.vending/com.google.android.finsky.transparentmainactivity.hsdpalias"
)
_CLOSE_LABELS = {"close", "close ad", "close video", "dismiss", "dismiss ad"}
_TOP_LEFT_TEMPLATE = "in_game_ad/end_close_top_left.png"
_PLAY_STORE_CLOSE_TEMPLATE = "in_game_ad/play_store_close.png"
# White skip (⏭) and close (×) glyphs, matched by silhouette in either top
# corner; the close is preferred because it leaves the ad.
_CORNER_GLYPHS = ("in_game_ad/corner_close.png", "in_game_ad/corner_skip.png")
_CORNER_SIZE = 280
_CORNER_THRESHOLD = .9
_GAME_ACTIVITY = "com.techtreegames.thetower/com.unity3d.player.unityplayeractivity"


def _focus_line(window_dump: str) -> str:
    return next((line.lower() for line in window_dump.splitlines()
                 if "mCurrentFocus=" in line), "")


def _ad_has_focus(window_dump: str) -> bool:
    """Require the foreground window to belong to an ad Activity."""
    focus = _focus_line(window_dump)
    return (any(name in focus for name in _AD_ACTIVITY)
            or any(package in focus for package in _AD_SDK_PACKAGES)
            or any(token.rstrip("}") == _META_AD_ACTIVITY for token in focus.split())
            or _PLAY_STORE_OVERLAY in focus)


def _play_store_overlay_has_focus(window_dump: str) -> bool:
    return _PLAY_STORE_OVERLAY in _focus_line(window_dump)


def _window_state(device: Any) -> str:
    read_window = getattr(device, "ad_window_state", None)
    if not callable(read_window):
        shell = getattr(device, "shell", None)
        if not callable(shell):
            return ""
        read_window = lambda: shell("dumpsys window")
    try:
        return read_window()
    except Exception:  # noqa: BLE001 - an ADB failure is not focus evidence
        return ""


def ad_foreground(device: Any) -> bool:
    """Whether Android reports a full-screen ad above the game."""
    return _ad_has_focus(_window_state(device))


def play_store_overlay_foreground(device: Any) -> bool:
    """Whether the rewarded ad opened Google Play's transparent product sheet."""
    return _play_store_overlay_has_focus(_window_state(device))


def game_foreground(device: Any) -> bool:
    """Whether The Tower, rather than an ad or Play, owns the current window."""
    return _GAME_ACTIVITY in _focus_line(_window_state(device))


def _play_store_close(screen: Image, templates: TemplateCache) -> tuple[int, int] | None:
    """Locate the X witnessed on the Play product sheet at native resolution."""
    if screen.shape[:2] != (2400, 1080):
        return None
    template = templates.get(_PLAY_STORE_CLOSE_TEMPLATE)
    if template is None:
        return None
    region = screen[480:1560, 790:1080]
    _, score, _, (x, y) = cv2.minMaxLoc(
        cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    if score < .9:
        return None
    return (790 + x + template.shape[1] // 2,
            480 + y + template.shape[0] // 2)


def _white(image: Image) -> Image:
    """Near-white pixels, independent of the creative drawn behind them."""
    low = image.min(axis=2).astype(np.int16)
    high = image.max(axis=2).astype(np.int16)
    return ((low >= 200) & (high - low <= 40)).astype(np.float32)


def _corner_glyph(screen: Image, templates: TemplateCache) -> tuple[int, int] | None:
    """Locate one white close or skip glyph in a top corner of an ad."""
    width = screen.shape[1]
    if screen.shape[0] < _CORNER_SIZE or width < 2 * _CORNER_SIZE:
        return None
    corners = [(x0, _white(screen[:_CORNER_SIZE, x0:x0 + _CORNER_SIZE]))
               for x0 in (0, width - _CORNER_SIZE)]
    for name in _CORNER_GLYPHS:
        template = templates.get(name)
        if template is None:
            continue
        glyph = _white(template)
        points: list[tuple[int, int]] = []
        for x0, corner in corners:
            scores = np.nan_to_num(cv2.matchTemplate(corner, glyph, cv2.TM_CCORR_NORMED))
            for y, x in zip(*np.nonzero(scores >= _CORNER_THRESHOLD)):
                point = (x0 + int(x) + glyph.shape[1] // 2, int(y) + glyph.shape[0] // 2)
                if all(abs(point[0] - px) > 40 or abs(point[1] - py) > 40
                       for px, py in points):
                    points.append(point)
        if len(points) > 1:
            return None
        if points:
            return points[0]
    return None


def return_dialog(screen: Image, boxes: Sequence[TextBox],
                  device: Any) -> tuple[str, tuple[int, int]] | None:
    """Locate only the game dialogs witnessed after closing the Play overlay."""
    if screen.shape[:2] != (2400, 1080) or not game_foreground(device):
        return None
    trusted = [box for box in boxes if box.confidence >= .9]
    names = {re.sub(r"[^a-z0-9]", "", box.text.lower()) for box in trusted}
    if {"welcomeback", "resumepreviousround"} <= names and any(
            name.startswith("resumesremaining") for name in names):
        action, label = "resume", "resume"
    elif ({"cloud", "createaccount", "maybelater"} <= names
          and any(name.startswith("cloudsaveisnow") for name in names)):
        action, label = "maybe_later", "maybelater"
    else:
        return None
    candidates = [box for box in trusted
                  if re.sub(r"[^a-z0-9]", "", box.text.lower()) == label
                  and 200 <= box.rect.x < box.rect.x + box.rect.w <= 880
                  and 1080 <= box.rect.y < box.rect.y + box.rect.h <= 1920
                  and box.rect.h <= 150]
    if len(candidates) != 1:
        return None
    rect = candidates[0].rect
    return action, (rect.x + rect.w // 2, rect.y + rect.h // 2)


def _accessible_close(hierarchy: str, screen: Image) -> tuple[str, tuple[int, int] | None]:
    """Read small labelled close controls at an ad viewport's outer edge."""
    end = hierarchy.find("</hierarchy>")
    if end < 0:
        return ("absent", None)
    try:
        root = ET.fromstring(hierarchy[:end + len("</hierarchy>")])
    except ET.ParseError:
        return ("absent", None)
    nodes = list(root.iter("node"))
    width, height = screen.shape[1], screen.shape[0]
    for node in nodes:
        match = _BOUNDS.fullmatch(node.get("bounds", ""))
        if match and int(match[1]) == int(match[2]) == 0:
            width, height = int(match[3]), int(match[4])
            break
    points: list[tuple[int, int]] = []
    for node in nodes:
        if node.get("clickable") != "true":
            continue
        labels = {" ".join(value.lower().split())
                  for child in node.iter()
                  for value in (child.get("text", ""), child.get("content-desc", ""))}
        resource = node.get("resource-id", "").rsplit("/", 1)[-1].lower()
        if not (labels & _CLOSE_LABELS) and resource not in {"close", "close_button", "ad_close"}:
            continue
        bounds = _BOUNDS.fullmatch(node.get("bounds", ""))
        if bounds is None:
            continue
        x0, y0, x1, y1 = map(int, bounds.groups())
        x, y = (x0 + x1) // 2, (y0 + y1) // 2
        if (not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height)
                or x1 - x0 > width * .22 or y1 - y0 > height * .13
                or not (x < width * .27 or x > width * .73)
                or not (y < height * .2 or y > height * .8)):
            continue
        if all(abs(x - px) > 40 or abs(y - py) > 40 for px, py in points):
            points.append((x, y))
    if len(points) > 1:
        return ("ambiguous", None)
    if points:
        return ("located", points[0])
    return ("absent", None)


def find_close(screen: Image, templates: TemplateCache, device: Any) -> tuple[int, int] | None:
    """Return one witnessed close button, or None when evidence is unclear."""
    if play_store_overlay_foreground(device):
        return _play_store_close(screen, templates)
    read_hierarchy = getattr(device, "ad_accessibility_hierarchy", None)
    if not callable(read_hierarchy):
        shell = getattr(device, "shell", None)
        if callable(shell):
            read_hierarchy = lambda: shell("uiautomator dump /dev/tty")
    ad_focused = ad_foreground(device)
    if callable(read_hierarchy) and ad_focused:
        try:
            status, point = _accessible_close(read_hierarchy(), screen)
            if status == "ambiguous":
                return None
            if point is not None:
                return point
        except Exception:  # noqa: BLE001 - ADB and UI hierarchy failures need visual fallback
            pass
    if ad_focused:
        glyph = _corner_glyph(screen, templates)
        if glyph is not None:
            return glyph
    close = battle_menu.ad_end_card_close(screen, templates)
    if close is not None:
        return close
    template = templates.get(_TOP_LEFT_TEMPLATE)
    if template is None:
        return None
    region = screen[:260, :220]
    if region.shape[0] < template.shape[0] or region.shape[1] < template.shape[1]:
        return None
    _, score, _, (x, y) = cv2.minMaxLoc(
        cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
    if score < .82:
        return None
    return (x + template.shape[1] // 2, y + template.shape[0] // 2)
